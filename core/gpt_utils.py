# core/gpt_utils.py
from __future__ import annotations
import os
import re
import json
from typing import Any, Dict, List

from pathlib import Path
from dotenv import load_dotenv, find_dotenv

# 優先載入專案根目錄的 .env
ROOT = Path(__file__).resolve().parents[1]
candidates = [ROOT/".env", ROOT/".env.local", Path.cwd()/".env"]
for p in candidates:
    if p.exists():
        load_dotenv(p, override=False)
fd = find_dotenv(usecwd=True)
if fd:
    load_dotenv(fd, override=False)

from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    tried = [str(p) for p in candidates] + ([fd] if fd else [])
    raise RuntimeError(f"缺少 OPENAI_API_KEY，請在 .env 或環境變數設定；已嘗試載入：{tried}")
client = OpenAI(api_key=OPENAI_API_KEY)

# === 預設模型（可依環境調整） ===
DEFAULT_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
DEFAULT_JSON_MODEL = os.getenv("OPENAI_JSON_MODEL", "gpt-4o-mini")


# ---------------------------
# 兼容舊介面
# ---------------------------
def call_gpt(prompt: str, model: str = "gpt-3.5-turbo", temperature: float = 0.7) -> str:
    return call_gpt_text(
        system="你是旅遊規劃助理。請一律使用繁體中文回覆。",
        user=prompt,
        model=model if model else DEFAULT_TEXT_MODEL,
        temperature=temperature,
    )


def call_gpt_text(
    system: str,
    user: str,
    model: str = DEFAULT_TEXT_MODEL,
    temperature: float = 0.6,
    max_tokens: int = 3000,
    max_retries: int = 2,
) -> str:
    last_err: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print("❌ GPT 錯誤(call_gpt_text)：", e)
            last_err = e
    raise RuntimeError(last_err)


def call_llm_json(
    system: str,
    user: str,
    model: str = DEFAULT_JSON_MODEL,
    temperature: float = 0.6,
    max_tokens: int = 4000,
    max_retries: int = 2,
) -> Any:
    """
    使用 response_format=json_object。注意：messages 內容必須包含 'json' 字樣。
    """
    # 保證 system/user 至少一邊含有 "json"
    if "json" not in system.lower():
        system = system + "（請以 JSON 格式回覆）"
    if "json" not in user.lower():
        user = user + "\n\n（以上為 JSON 任務，請回傳 JSON）"

    last_err: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            if not content:
                raise RuntimeError("empty content")
            return json.loads(content)
        except Exception as e:
            print("❌ GPT 錯誤(call_llm_json)：", e)
            last_err = e
    raise RuntimeError(last_err)


# ---------------------------
# 多模型檢查（輸入改為 JSON）
# ---------------------------
CHECKER_MODELS = ["gpt-4o-mini", "gpt-4o-mini", "gpt-4o-mini"]
AGG_MODEL = "gpt-4o-mini"

# 注意：此模板會用 .format(plan=...)，因此示範 JSON 的大括號必須用 {{ }}
CHECKER_USER_TMPL = """你是旅遊品質檢查員，請用 JSON 回覆。
輸入是一份「行程 JSON」，請檢查：餐段是否齊全、營業時間是否合理、移動距離是否過遠、是否有重複地點。
請提出 1-3 條改善建議（短句）。
行程內容如下（JSON）：
{plan}

若可行請回：
{{
  "passed": true,
  "notes": ["...","..."]
}}
否則：
{{
  "passed": false,
  "issues": ["...","..."],
  "suggestions": ["..."]
}}"""

AGG_USER_TMPL = """下面是多個檢查器的 JSON 結果，請彙總為單一 JSON：
{reports}
若有任何一個 passed=false，就回：
{{"passed": false, "reasons": ["..."]}}
否則：
{{"passed": true, "highlights": ["..."]}}"""


def call_multi_checkers(plan_json_str: str, summary: str = "") -> Dict[str, Any]:
    reports: List[Any] = []
    for m in CHECKER_MODELS:
        try:
            r = call_llm_json(
                system="你是旅遊品質檢查員，輸出必須是 JSON；務必使用繁體中文。",
                user=CHECKER_USER_TMPL.format(plan=plan_json_str),
                model=m,
                temperature=0.2,
            )
            reports.append(r)
        except Exception as e:
            reports.append({"error": f"checker_failed:{m}", "notes": str(e)})

    reports_json = json.dumps(reports, ensure_ascii=False, indent=2)
    agg = call_llm_json(
        system="你是旅遊品質總審查員，輸出必須是 JSON；務必使用繁體中文。",
        user=AGG_USER_TMPL.format(reports=reports_json),
        model=AGG_MODEL,
        temperature=0.2,
    )
    return {"checkers": reports, "aggregate": agg}


# ---------------------------
# 在候選清單中挑選（嚴禁創造新店）
# ---------------------------
def select_from_candidates(slot_context: Dict[str, Any],
                           candidates: List[Dict[str, Any]],
                           k: int = 1,
                           model: str = DEFAULT_JSON_MODEL,
                           temperature: float = 0.2) -> List[Dict[str, Any]]:
    """
    讓 GPT 在候選清單中挑選 k 個店家。輸入 candidates 建議包含：
      place_id / name / rating / user_ratings_total / travel_minutes_from_anchor / types / formatted_address
    若呼叫失敗，改用 rule-based（距離→評分→評論數）回傳前 k 名。
    回傳格式：[{"place_id": "...", "reason": "..."}]
    """
    if not candidates:
        return []

    try:
        slim = [
            {
                "place_id": c.get("place_id"),
                "name": c.get("name"),
                "rating": c.get("rating"),
                "reviews": c.get("user_ratings_total"),
                "minutes": c.get("travel_minutes_from_anchor"),
                "types": c.get("types", []),
                "address": c.get("formatted_address", ""),
            } for c in candidates
        ]

        sys_prompt = (
            "你是旅遊行程規劃助手。你只能從提供的候選清單中挑選店家，不得創造清單外店家。"
            "優先選擇移動時間較短的選項；若差距不大，再看評分與評論數。請用繁體中文。"
            "請以 JSON 格式回覆結果。"
        )
        user_prompt = json.dumps({
            "slot_context": slot_context,
            "candidates": slim,
            "instructions": {
                "k": k,
                "selection_rules": [
                    "只能選擇候選清單中的 place_id",
                    "優先 minutes 較短；同分看 rating 與 reviews",
                    "回傳 JSON 陣列：[{place_id, reason}]，reason 40字以內"
                ]
            }
        }, ensure_ascii=False) + "\n（以上為 JSON 資料，請回傳 JSON）"

        res = call_llm_json(system=sys_prompt, user=user_prompt, model=model, temperature=temperature)
        picks = res if isinstance(res, list) else res.get("picks") or res.get("result") or []
        if not isinstance(picks, list):
            raise RuntimeError("bad_format")

        out: List[Dict[str, Any]] = []
        for p in picks:
            if isinstance(p, dict) and p.get("place_id"):
                out.append({"place_id": p["place_id"], "reason": (p.get("reason","")[:60])})
            if len(out) >= k:
                break
        if out:
            return out
        raise RuntimeError("empty_selection")

    except Exception:
        pass

    cands = [c for c in candidates if c.get("place_id")]
    if not cands:
        return []
    cands.sort(key=lambda c: (
        c.get("travel_minutes_from_anchor", 999),
        -float(c.get("rating", 0) or 0),
        -int(c.get("user_ratings_total", 0) or 0)
    ))
    return [{"place_id": c.get("place_id"), "reason": "距離較近且評價較高"} for c in cands[:k]]
