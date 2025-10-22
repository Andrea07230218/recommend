# core/plan_verifier.py
from __future__ import annotations
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, time

from core.gpt_utils import call_llm_json
from core.google_maps import (
    get_place_details,
    is_open_during_slot,
    optimize_visit_order,
    MEAL_SLOT_LABELS,
)
# ---- 本地規則參數 ----
MAX_TRAVEL_RATIO_PER_SLOT = 0.55   # 單一時段內移動時間不應超過該時段長度的 55%
MAX_TRAVEL_MINUTES_HARD = 90       # 或者單段總移動時間不要超過 90 分鐘（兩者取嚴格者）
MIN_MEAL_SLOTS = {"早餐", "中午", "晚上"}  # 一天至少該有的餐段（可依需求改）
DEFAULT_MODEL = "gpt-4o-mini"


def _parse_hhmm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


def _minutes_between(hhmm_start: str, hhmm_end: str) -> int:
    t0 = _parse_hhmm(hhmm_start)
    t1 = _parse_hhmm(hhmm_end)
    dt0 = datetime.combine(datetime.today(), t0)
    dt1 = datetime.combine(datetime.today(), t1)
    return max(0, int((dt1 - dt0).total_seconds() // 60))


def _normalize_name(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum())


def _is_meal_slot(label: str) -> bool:
    return label in MEAL_SLOT_LABELS


# ---------- 本地規則檢查 ----------
def _local_rule_checks(structured_plan: Dict[str, Any], google_rating_min: float, meal_required: bool) -> Dict[str, Any]:
    """
    structured_plan 期待格式：
    {
      "days": [
        {
          "date": "YYYY-MM-DD",
          "slots": [
            {
              "label": "上午",
              "window": ["09:00","12:00"],
              "items": [ { name, place_id, rating, lat, lng, types, slot_type, ... }, ... ]
            }, ...
          ]
        }, ...
      ],
      "location": "台北市"
    }
    """
    issues: List[str] = []
    warnings: List[str] = []
    pass_flags = {
        "meals_ok": True,
        "open_ok": True,
        "dup_ok": True,
        "rating_ok": True,
        "travel_ok": True,
    }

    # A) 每日三餐覆蓋
    if meal_required:
        for di, day in enumerate(structured_plan.get("days", []), start=1):
            labels_present = set()
            for sl in day.get("slots", []):
                if _is_meal_slot(sl.get("label", "")) and sl.get("items"):
                    labels_present.add(sl["label"])
            missing = MIN_MEAL_SLOTS - labels_present
            if missing:
                pass_flags["meals_ok"] = False
                issues.append(f"第 {di} 天缺少餐段：{sorted(list(missing))}")

    # B) 開門重疊（以 Google 詳情再確認，寬鬆重疊即可）
    for di, day in enumerate(structured_plan.get("days", []), start=1):
        date_str = day.get("date")
        for sl in day.get("slots", []):
            win = sl.get("window", [])
            if len(win) != 2:
                continue
            for it in sl.get("items", []):
                pid = it.get("place_id")
                if not pid:
                    continue
                try:
                    det = get_place_details(pid)
                    if not is_open_during_slot(det.get("opening_hours"), tuple(win), date_str=date_str, require_full_cover=False):
                        pass_flags["open_ok"] = False
                        issues.append(f"第 {di} 天 {sl.get('label')}「{it.get('name','') }」疑似不在營業時間內")
                except Exception:
                    # 若 API 失敗，不直接判 fail，但做警告
                    warnings.append(f"第 {di} 天 {sl.get('label')}「{it.get('name','')}」無法確認營業時間")

    # C) 去重（以 place_id 優先，其次名稱）
    seen_pid = set()
    seen_name = set()
    for di, day in enumerate(structured_plan.get("days", []), start=1):
        for sl in day.get("slots", []):
            for it in sl.get("items", []):
                pid = it.get("place_id")
                nm = _normalize_name(it.get("name", ""))
                if pid and pid in seen_pid:
                    pass_flags["dup_ok"] = False
                    issues.append(f"重複地點(place_id)：{it.get('name','')}")
                if nm and nm in seen_name:
                    pass_flags["dup_ok"] = False
                    issues.append(f"重複地點(名稱)：{it.get('name','')}")
                if pid: seen_pid.add(pid)
                if nm:  seen_name.add(nm)

    # D) 評分門檻
    for di, day in enumerate(structured_plan.get("days", []), start=1):
        for sl in day.get("slots", []):
            for it in sl.get("items", []):
                r = it.get("rating")
                if r is None:
                    continue
                try:
                    rv = float(r)
                    if rv < google_rating_min:
                        pass_flags["rating_ok"] = False
                        issues.append(f"評分過低：{it.get('name','')}（{rv} < {google_rating_min}）")
                except Exception:
                    continue

    # E) 單時段移動時間（用 lat/lng + 距離矩陣 → 最近鄰 + 2-opt）
    for di, day in enumerate(structured_plan.get("days", []), start=1):
        for sl in day.get("slots", []):
            items = sl.get("items", [])
            if len(items) < 2:
                continue
            # 準備 lat/lng
            pts = [p for p in items if p.get("lat") is not None and p.get("lng") is not None]
            if len(pts) < 2:
                continue
            try:
                opt = optimize_visit_order(pts, start_idx=0, mode=None)
                travel_mins = int(opt["total_travel_secs"] / 60)
                slot_len = _minutes_between(sl["window"][0], sl["window"][1])
                if travel_mins > MAX_TRAVEL_MINUTES_HARD or travel_mins > int(slot_len * MAX_TRAVEL_RATIO_PER_SLOT):
                    pass_flags["travel_ok"] = False
                    issues.append(f"第 {di} 天 {sl.get('label')} 移動時間偏長（約 {travel_mins} 分）")
                # 把估算寫回去供 LLM 參考
                sl["computed_travel_minutes"] = travel_mins
                sl["computed_travel_mode"] = opt["mode"]
            except Exception:
                warnings.append(f"第 {di} 天 {sl.get('label')} 旅行時間估算失敗")

    all_local_pass = all(pass_flags.values())
    return {
        "pass_flags": pass_flags,
        "passed": all_local_pass,
        "issues": sorted(set(issues)),
        "warnings": sorted(set(warnings)),
    }


# ---------- LLM 評論員 ----------
def _role_prompt(role: str, plan_snippet: str, policy: str, model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """
    要求 JSON 回覆：{ "passed": bool, "reasons": [..], "suggestions": [..] }
    """
    system = f"""你是{role}。請以專業、嚴謹方式審核旅遊行程，並以繁體中文回覆。
回覆格式必須是 JSON，不要加入其它文字。"""
    user = f"""[政策與評分基準]
{policy}

[待審計行程片段]
{plan_snippet}

請輸出：
{{
  "passed": true|false,
  "reasons": [ "最重要的2-5點原因或疑慮" ],
  "suggestions": [ "若未通過，請給3-5點具體改善建議；若通過，給1-3點提升建議" ]
}}"""
    return call_llm_json(system=system, user=user, model=model)


def _join_plan_for_llm(structured_plan: Dict[str, Any]) -> str:
    lines: List[str] = []
    for di, day in enumerate(structured_plan.get("days", []), start=1):
        lines.append(f"第{di}天（{day.get('date','')}）")
        for sl in day.get("slots", []):
            win = sl.get("window", ["??","??"])
            lines.append(f"- {sl.get('label','?')}（{win[0]}–{win[1]}）")
            for it in sl.get("items", []):
                nm = it.get("name","")
                rt = it.get("rating","?")
                ty = ",".join(it.get("types",[])[:3])
                lines.append(f"  • {nm}（評分 {rt}；{ty}）")
            if "computed_travel_minutes" in sl:
                lines.append(f"  • 移動預估：{sl['computed_travel_minutes']} 分（{sl.get('computed_travel_mode','?')}）")
    return "\n".join(lines[:1500])  # 控制長度


def _aggregate_editor(local: Dict[str, Any], rule_j: Dict[str, Any], food_j: Dict[str, Any], logistics_j: Dict[str, Any], model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    system = """你是總編輯，負責統整多位審核員與本地檢查的結果，決定是否「通過上線」。輸出純 JSON。"""
    user = f"""[本地規則檢查]
{local}

[規則審核員]
{rule_j}

[美食審核員]
{food_j}

[物流審核員]
{logistics_j}

請整合以上資訊，輸出：
{{
  "passed": true|false,                  // 只有在「本地規則」通過 且 三位審核員皆 passed=true 才能通過
  "primary_reasons": ["最關鍵的3-6點理由（失敗或風險）"],
  "warnings": ["可接受但應提醒的事項（1-5點）"],
  "improvements": ["若要通過或提升品質的可執行建議（3-8點）"]
}}"""
    return call_llm_json(system=system, user=user, model=model)


def run_plan_verifier(
    structured_plan: Dict[str, Any],
    *,
    google_rating_min: float = 3.8,
    meal_required: bool = True,
    model: str = DEFAULT_MODEL,
) -> Dict[str, Any]:
    """
    回傳：
    {
      "local": {...},
      "rule_checker": {...},
      "food_checker": {...},
      "logistics_checker": {...},
      "aggregate": {...}
    }
    """
    # 1) 先做本地規則（硬性）
    local = _local_rule_checks(structured_plan, google_rating_min, meal_required)

    # 2) 準備 LLM 審核內容（用彙整過的文字 + 本地摘要）
    plan_txt = _join_plan_for_llm(structured_plan)

    rule_policy = """檢查是否合理：不跨縣市、評分是否達標、是否有重複地點、一天至少要有適量行程、描述是否完整且可落地。"""
    food_policy = """檢查餐飲安排：早餐/中午/晚上是否覆蓋、選店是否符合用餐時段與型態（餐廳/咖啡/夜市等）、是否避免全是連鎖、是否考量在地特色。"""
    logistics_policy = """檢查時間與動線：單時段的停留時間與移動時間是否合理、是否有過度奔波、相鄰地點是否同一區域、是否需排隊等風險。"""

    # 3) 三位評論員（可以用不同模型；這裡先統一 DEFAULT_MODEL）
    rule_j = _role_prompt("規則審核員", plan_txt, rule_policy, model)
    food_j = _role_prompt("美食審核員", plan_txt, food_policy, model)
    logistics_j = _role_prompt("物流審核員", plan_txt, logistics_policy, model)

    # 4) 總編輯做最後裁決（本地規則 + 三審皆通過才算通過）
    local_gate = local.get("passed", False)
    three_ok = all(j.get("passed", False) for j in [rule_j, food_j, logistics_j])
    agg_input = {
        "local_passed": local_gate,
        "rule_passed": rule_j.get("passed", False),
        "food_passed": food_j.get("passed", False),
        "logistics_passed": logistics_j.get("passed", False),
        "local_issues": local.get("issues", []),
        "local_warnings": local.get("warnings", []),
    }
    aggregate = _aggregate_editor(agg_input, rule_j, food_j, logistics_j, model)
    aggregate["passed"] = bool(local_gate and three_ok and aggregate.get("passed", False))

    return {
        "local": local,
        "rule_checker": rule_j,
        "food_checker": food_j,
        "logistics_checker": logistics_j,
        "aggregate": aggregate,
    }
