# core/langgraph_nodes.py
from __future__ import annotations
from datetime import datetime, timedelta, time
import re
from typing import Dict, Any, List, Tuple, Optional

from core import mongo
from core.gpt_utils import (
    call_gpt,
    call_llm_json,
    call_multi_checkers,
)
from core.google_maps import (
    geocode_city,
    search_place,
    get_place_details,
    is_open_during_slot,
    format_google_hours,
    clean_place_name,
    search_meal_places,
    optimize_visit_order,
    MEAL_SLOT_LABELS,
    opening_line_for_date,
    search_attraction_candidates,
    annotate_travel_minutes,
    travel_time_minutes,
    build_map_url,
    # build_directions_url,   # 目前未使用
    search_store_candidates,
)
from core.stay_time import estimate_stay_window, balance_durations_in_slot
import markdown2
from core.itinerary_linked_list import ItineraryNode, linked_to_list
from core.fallback import fallback_place_from_backup

from collections import Counter
import json
from bson import ObjectId

# =========================================
# 使用者 id 候選（多欄位兼容）
# =========================================
def get_id_candidates(user_id: str) -> List[Any]:
    cands: List[Any] = []
    if user_id is not None:
        cands.append(user_id)
    try:
        if isinstance(user_id, str) and len(user_id) == 24:
            cands.append(ObjectId(user_id))
    except Exception:
        pass
    try:
        u = mongo.get_user(user_id)
        if u:
            if "_id" in u:
                cands.append(u["_id"])
                cands.append(str(u["_id"]))
                for k in ["id", "user_id", "username"]:
                    if k in u and u[k]:
                        cands.append(u[k])
    except Exception:
        pass
    seen = set()
    uniq: List[Any] = []
    for x in cands:
        key = (type(x).__name__, str(x))
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


# =========================================
# Google types → 中文偏好 tag 對齊
# =========================================
TYPE2TAG = {
    "tourist_attraction": "景點",
    "museum": "博物館",
    "art_gallery": "藝文",
    # "park": "公園",
    "zoo": "親子",
    "aquarium": "親子",
    "shopping_mall": "逛街",
    "department_store": "逛街",
    "night_market": "夜市",
    "restaurant": "美食",
    "cafe": "咖啡",
    "bakery": "甜點",
    "bar": "酒吧",
    "church": "歷史",
    "hindu_temple": "宗教",
    "synagogue": "宗教",
    "mosque": "宗教",
    "amusement_park": "遊樂",
    "beach": "海景",
    "hiking_area": "步道",
}

def map_types_to_tags(types: List[str]) -> List[str]:
    out = []
    for t in types or []:
        if t in TYPE2TAG:
            out.append(TYPE2TAG[t])
    return out + (types or [])

def _ensure_tag_list(val) -> List[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, dict):
        for k in ["tags", "primary", "list", "values"]:
            v = val.get(k)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        return []
    if isinstance(val, str):
        return [p.strip() for p in re.split(r"[,\u3001/;|\s]+", val) if p.strip()]
    return []

# =========================================
# 多城市工具（正規化/備註解析/分配）
# =========================================
CITY_ALIASES = {"台北": "臺北", "台中": "臺中", "台南": "臺南"}

def norm_city(c: str) -> str:
    c = (c or "").strip()
    if not c:
        return c
    c = c.replace("台", "臺").replace(" ", "")
    base = c.replace("市", "").replace("縣", "")
    base = CITY_ALIASES.get(base, base)
    # 外島用「縣」，其餘預設補「市」
    if base in ["澎湖", "金門", "連江"]:
        return base + "縣"
    return base + "市"

def to_locations_list(field) -> List[str]:
    if isinstance(field, list):
        raw = field
    else:
        raw = re.split(r"[，,/\s]+", str(field or ""))
    seen, ordered = set(), []
    for x in raw:
        x = norm_city(x)
        if x and x not in seen:
            seen.add(x)
            ordered.append(x)
    return ordered

DAY_PATTERNS = [
    (1, r"(?:第\s*一\s*天|[Dd]\s*1|Day\s*1)"),
    (2, r"(?:第\s*二\s*天|[Dd]\s*2|Day\s*2)"),
    (3, r"(?:第\s*三\s*天|[Dd]\s*3|Day\s*3)"),
    (4, r"(?:第\s*四\s*天|[Dd]\s*4|Day\s*4)"),
    (5, r"(?:第\s*五\s*天|[Dd]\s*5|Day\s*5)"),
    (6, r"(?:第\s*六\s*天|[Dd]\s*6|Day\s*6)"),
    (7, r"(?:第\s*七\s*天|[Dd]\s*7|Day\s*7)"),
]

def chinese_day_to_int(token: str) -> int:
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    zh = "一二三四五六七八九十"
    val = 0
    for ch in token:
        i = zh.find(ch)
        if i >= 0:
            val = val * 10 + (i + 1)
    return val if val > 0 else 1

def parse_day_city_plan_from_notes(notes: str, max_days: int, known_cities: List[str]) -> Tuple[Dict[int, str], Dict[int, str]]:
    if not notes:
        return {}, {}
    text = notes.replace("台", "臺")
    day_city_plan, stay_city_plan = {}, {}
    # 住宿指定：第N天 住宿在 X
    for _, day_token, city_token in re.findall(
        r"(第\s*([一二三四五六七八九十\d]+)\s*天).*?(?:住宿|住)\s*(?:地點)?\s*在?\s*([^\s，,。]+)",
        text,
    ):
        d = chinese_day_to_int(day_token)
        if 1 <= d <= max_days:
            stay_city_plan[d] = norm_city(city_token)
    # 第N天在 X
    for d, pat in DAY_PATTERNS[:max_days]:
        m = re.search(pat + r".{0,10}在\s*([^\s，,。]+)", text)
        if m:
            day_city_plan[d] = norm_city(m.group(1))
    # 把備註中新城市補進清單
    for _, c in list(day_city_plan.items()) + list(stay_city_plan.items()):
        if c not in known_cities:
            known_cities.append(c)
    return day_city_plan, stay_city_plan

def allocate_days_even(days: int, ordered_cities: List[str]) -> List[str]:
    if not ordered_cities:
        return []
    m = len(ordered_cities)
    base = [days // m] * m
    for i in range(days % m):
        base[i] += 1
    out = []
    for c, n in zip(ordered_cities, base):
        out += [c] * n
    return out

def allocate_days_by_density(
    days: int,
    ordered_cities: List[str],
    city_counts: Dict[str, int],
    tie_threshold: float = 0.2,
) -> List[str]:
    if not ordered_cities or days <= 0:
        return []
    counts = [max(0, int(city_counts.get(c, 0))) for c in ordered_cities]
    # 完全沒資料 → 平均
    if sum(counts) == 0:
        return allocate_days_even(days, ordered_cities)
    mx, mn = max(counts), min(counts)
    # 差距不大 → 平均
    if mx == 0 or (mx - mn) / (mx or 1) <= tie_threshold:
        return allocate_days_even(days, ordered_cities)
    # 比例分配（至少各 1 天）
    total = sum(counts)
    base = [max(1, round(days * (c / total))) for c in counts]
    diff = days - sum(base)
    if diff != 0:
        order = sorted(
            range(len(counts)),
            key=lambda k: counts[k],
            reverse=(diff < 0),
        )
        idx = 0
        while diff != 0 and idx < len(order) * 3:
            k = order[idx % len(order)]
            if diff > 0:
                base[k] += 1
                diff -= 1
            else:
                if base[k] > 1:
                    base[k] -= 1
                    diff += 1
            idx += 1
    out = []
    for c, n in zip(ordered_cities, base):
        out += [c] * n
    return out[:days]

def quick_count_candidates(city_name: str, start_date: Optional[str] = None) -> int:
    """輕量估算城市候選量（景點 + 餐飲）。API 失敗時回 0。"""
    try:
        latlng = geocode_city(city_name)
        if not latlng:
            return 0
        center = (latlng[0], latlng[1])
        # 景點：抓少量、放寬條件
        spots = search_attraction_candidates(
            city_name,
            center,
            radius_m=2500,
            limit=18,
            min_rating=4.0,
            min_reviews=20,
            date=start_date or "",
            slot_range=("10:00", "17:00"),
            require_full_cover=False,
            mode="driving",
        ) or []
        # 餐飲：抓中午檔
        foods = search_meal_places(
            city=city_name,
            slot_label="中午",
            date=start_date or str(datetime.today().date()),
            start_hhmm="1200",
            end_hhmm="1400",
            center_latlng=center,
            radius_m=2000,
        ) or []
        return len(spots) + len(foods)
    except Exception:
        return 0


# =========================================
# 從 Mongo 拉瀏覽/收藏 → GPT 偏好分析
# =========================================
def fetch_user_behavior_tags(
    user_id: str, limit_browse: int = 300, limit_fav: int = 200
) -> Dict[str, Any]:
    db = mongo.db
    pageviews_col = db["user_browse"]
    favorites_col = db["user_favorite"]

    id_fields = ["user_id", "userId", "uid", "account_id", "username", "_user_id"]
    id_vals = get_id_candidates(user_id)
    or_clauses = [{fld: {"$in": id_vals}} for fld in id_fields]

    projection = {
        "_id": 0,
        "tags": 1,
        "tag": 1,
        "labels": 1,
        "tag_list": 1,
        "tagString": 1,
        "place_name": 1,
        "name": 1,
        "city": 1,
        "city_name": 1,
        "cityName": 1,
        "timestamp": 1,
        "browse_date": 1,
        "created_at": 1,
        "createdAt": 1,
        "fav_date": 1,
        "favorite_date": 1,
    }

    browse_cursor = (
        pageviews_col.find({"$or": or_clauses}, projection)
        .sort(
            [
                ("browse_date", -1),
                ("timestamp", -1),
                ("created_at", -1),
                ("createdAt", -1),
            ]
        )
        .limit(limit_browse)
    )

    fav_cursor = (
        favorites_col.find({"$or": or_clauses}, projection)
        .sort(
            [
                ("favorite_date", -1),
                ("fav_date", -1),
                ("timestamp", -1),
                ("created_at", -1),
                ("createdAt", -1),
            ]
        )
        .limit(limit_fav)
    )

    tag_counter = Counter()
    city_counter = Counter()
    samples: List[Dict[str, Any]] = []

    def pick_tags(doc: Dict[str, Any]) -> List[str]:
        for key in ["tags", "tag", "labels", "tag_list", "tagString"]:
            if key in doc and doc.get(key) not in (None, "", []):
                return _ensure_tag_list(doc.get(key))
        return []

    def pick_city(doc: Dict[str, Any]) -> str:
        for key in ["city", "city_name", "cityName"]:
            v = (doc.get(key) or "").strip()
            if v:
                return v
        return ""

    def pick_name(doc: Dict[str, Any]) -> str:
        for key in ["place_name", "name"]:
            v = (doc.get(key) or "").strip()
            if v:
                return v
        return ""

    def pick_ts(doc: Dict[str, Any]):
        return (
            doc.get("browse_date")
            or doc.get("favorite_date")
            or doc.get("fav_date")
            or doc.get("timestamp")
            or doc.get("created_at")
            or doc.get("createdAt")
        )

    def ingest(doc, source: str):
        tags = pick_tags(doc)
        place = pick_name(doc)
        city = pick_city(doc)
        ts = pick_ts(doc)

        for t in tags:
            tag_counter[t] += 1
        if city:
            city_counter[city] += 1

        if len(samples) < 40:
            samples.append(
                {
                    "source": source,
                    "place_name": place,
                    "city": city,
                    "tags": tags[:8],
                    "timestamp": ts,
                }
            )

    browse_count = 0
    for b in browse_cursor:
        ingest(b, "browse")
        browse_count += 1

    fav_count = 0
    for f in fav_cursor:
        ingest(f, "favorite")
        fav_count += 1

    try:
        print(
            f"[偏好分析] 抓取到瀏覽 {browse_count} 筆、收藏 {fav_count} 筆（user_id={user_id}；候選IDs={list(map(str,id_vals))}）"
        )
    except Exception:
        pass

    return {
        "total_browse": pageviews_col.count_documents({"$or": or_clauses}),
        "total_favorite": favorites_col.count_documents({"$or": or_clauses}),
        "tag_counts": dict(tag_counter.most_common(200)),
        "city_counts": dict(city_counter.most_common(50)),
        "samples": samples,
    }

def build_preference_prompt(user_id: str, bundle: Dict[str, Any]) -> str:
    tag_counts = bundle.get("tag_counts", {})
    city_counts = bundle.get("city_counts", {})
    samples = bundle.get("samples", [])
    total_browse = bundle.get("total_browse", 0)
    total_fav = bundle.get("total_favorite", 0)

    top_tags_preview = dict(list(tag_counts.items())[:40])

    lines = []
    lines.append("你是一位旅遊偏好分析師，請根據使用者的歷史『瀏覽與收藏』資料，推論其旅遊偏好。")
    lines.append("請用中文回答，並嚴格輸出指定 JSON 格式。")
    lines.append("")
    lines.append(f"【資料總覽】瀏覽 {total_browse} 筆、收藏 {total_fav} 筆")
    lines.append(f"【Top Tags(截取)】{top_tags_preview}")
    lines.append(f"【城市偏好(計數)】{city_counts}")
    lines.append("【代表樣本(節錄)】最多 40 筆，含來源(browse/favorite)、地點、城市與 tags：")
    for s in samples:
        lines.append(
            f"- ({s.get('source')}) {s.get('place_name','')} / {s.get('city','')} / tags={s.get('tags',[])}"
        )

    lines.append(
        """
請輸出以下 JSON（不要多餘解釋）：
{
  "top_themes": [
    {"tag": "<字串>", "score": 0.0, "evidence": ["從哪些標籤/樣本推得(簡短)"] }
  ],
  "style": "用 1~2 句形容此人旅行風格",
  "avoid": ["可能不喜歡/較少出現的主題（若無可空陣列）"],
  "city_bias": [{"city": "<城市>", "score": 0.0}],
  "confidence": 0.0,
  "summary_zh": "用 2~4 句中文總結此人的旅遊偏好重點"
}
規則：
- score 代表偏好強度或傾向機率，總和不需為 1。
- top_themes 建議 5~8 個主題（以 tag 為主），evidence 簡短列關鍵字即可。
- city_bias 只列 3~6 個最有代表性的城市。
- 嚴格輸出合法 JSON。若資料量明顯不足，請仍輸出完整 JSON，並將 confidence 降低，summary 說明不足原因。
"""
    )
    return "\n".join(lines)

def analyze_user_preferences_with_gpt(user_id: str) -> Dict[str, Any]:
    bundle = fetch_user_behavior_tags(user_id=user_id)
    if not bundle.get("tag_counts"):
        print(f"[偏好分析] 使用者 {user_id} 沒有瀏覽或收藏資料")
        return {
            "top_themes": [],
            "style": "",
            "avoid": [],
            "city_bias": [],
            "confidence": 0.2,
            "summary_zh": "沒有找到瀏覽或收藏資料，無法判定偏好。",
        }

    prompt = build_preference_prompt(user_id, bundle)
    print("========== 偏好分析 Prompt ==========")
    print(prompt)
    print("==================================")

    try:
        res = call_llm_json(
            system="你是旅遊偏好分析師，務必以 JSON 回覆，並使用繁體中文。",
            user=prompt,
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=800,
        )
        print("========== GPT 原始回應(JSON) ==========")
        try:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        except Exception:
            print(str(res))
        print("================================")

        res["_source_stats"] = {
            "tag_counts": bundle.get("tag_counts"),
            "city_counts": bundle.get("city_counts"),
        }
        print("✅ 偏好分析 JSON 解析成功")
        return res

    except Exception as e:
        print("⚠️ GPT 呼叫失敗（analyze_user_preferences_with_gpt）：", str(e))
        import traceback

        traceback.print_exc()
        return {
            "top_themes": [],
            "style": "",
            "avoid": [],
            "city_bias": [],
            "confidence": 0.2,
            "summary_zh": "偏好分析呼叫失敗，已使用保底結果。",
        }

def score_by_behavior(
    place_tags: List[str],
    place_city: Optional[str],
    behavior_profile: Dict[str, Any],
    alpha: float = 0.5,
    beta: float = 0.3,
    gamma: float = -0.2,
) -> float:
    """
    根據 GPT 偏好分析結果幫景點加/扣分。
    - alpha: 主題分數權重
    - beta : 城市傾向權重
    - gamma: 避免主題扣分（每命中一類扣一次）
    """
    if not behavior_profile:
        return 0.0

    tags = set([t.strip() for t in place_tags or [] if t and str(t).strip()])
    city = (place_city or "").strip()

    theme_score = 0.0
    for t in behavior_profile.get("top_themes", []):
        tag = (t.get("tag") or "").strip()
        s = float(t.get("score") or 0.0)
        if tag and tag in tags:
            theme_score += s

    city_score = 0.0
    for c in behavior_profile.get("city_bias", []):
        if (c.get("city") or "").strip() == city:
            city_score += float(c.get("score") or 0.0)

    avoid_hits = 0
    avoid_list = set(
        [a.strip() for a in behavior_profile.get("avoid", []) if a and str(a).strip()]
    )
    for a in avoid_list:
        if a in tags:
            avoid_hits += 1

    return alpha * theme_score + beta * city_score + gamma * avoid_hits


# =========================================
# Node 1：合併問卷與收藏偏好 + 多城市解析
# =========================================
def extract_profile(state: Dict[str, Any]) -> Dict[str, Any]:
    user_id = state["user_id"]
    form = state["form"]

    def normalize_transport(val: Optional[str]) -> str:
        if not val:
            return "transit"
        v = str(val).strip().lower()
        alias = {
            "transit": "transit", "public": "transit", "bus": "transit", "metro": "transit",
            "train": "transit", "大眾運輸": "transit", "公共運輸": "transit", "捷運": "transit", "電車": "transit",
            "car": "driving", "drive": "driving", "driving": "driving", "汽車": "driving", "開車": "driving", "自駕": "driving",
            "walking": "walking", "walk": "walking", "步行": "walking",
            "bicycling": "bicycling", "bike": "bicycling", "單車": "bicycling", "腳踏車": "bicycling", "自行車": "bicycling",
        }
        return alias.get(v, "transit")

    # 在 extract_profile 取得 form 後加：
    if isinstance(form, dict) and "transport" in form:
        form["transport"] = normalize_transport(form.get("transport"))

    # base profile
    if user_id == "group":
        merged_pref = form.get("偏好", ["未知偏好"])
        profile = {
            "favorites": [],
            "form": form,
            "偏好分析": merged_pref[0] if isinstance(merged_pref, list) else merged_pref,
        }
    elif "偏好分析" in form or "favorites" in form:
        profile = {
            "favorites": form.get("favorites", []),
            "form": form,
            "偏好分析": form.get("偏好分析", "尚未填寫"),
        }
    else:
        user = mongo.get_user(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found.")
        profile = {
            "favorites": user.get("favorites", []),
            "form": form,
            "偏好分析": user.get("偏好摘要", "尚未填寫"),
        }

    # 個人化偏好（行為 → GPT）
    behavior_profile = {}
    if user_id != "group":
        try:
            behavior_profile = analyze_user_preferences_with_gpt(user_id)
            state["behavior_profile"] = behavior_profile

            bp_summary = (behavior_profile or {}).get("summary_zh")
            if bp_summary:
                profile["偏好分析"] = bp_summary

            print("🔍 偏好分析完成：", profile.get("偏好分析", "（無）"))
            print(
                "   top_themes =",
                [(t.get("tag"), t.get("score")) for t in behavior_profile.get("top_themes", [])][:8],
            )
            print("   city_bias  =", behavior_profile.get("city_bias", [])[:5])

        except Exception as e:
            print("❌ 偏好分析流程例外（extract_profile）：", e)
            import traceback

            traceback.print_exc()
            state["behavior_profile"] = {
                "top_themes": [],
                "style": "",
                "avoid": [],
                "city_bias": [],
                "confidence": 0.0,
                "summary_zh": "偏好分析失敗（例外）。",
            }

    # ====== 多城市：解析/分配 ======
    fdict = profile.get("form", {}) if isinstance(profile.get("form", {}), dict) else {}
    days = int(fdict.get("旅遊天數") or fdict.get("days") or 1)

    raw_locations = (
        fdict.get("locations") or fdict.get("地點") or fdict.get("location") or ""
    )
    ordered_cities = to_locations_list(raw_locations) or ["高雄市"]

    notes = (fdict.get("備註") or fdict.get("notes") or "").strip()
    day_city_plan = fdict.get("day_city_plan") or {}
    stay_city_plan = fdict.get("stay_city_plan") or {}
    if not day_city_plan and notes:
        dcp, scp = parse_day_city_plan_from_notes(
            notes, days, ordered_cities.copy()
        )
        day_city_plan, stay_city_plan = dcp, scp
        fdict["locations"] = ordered_cities  # 補回正規欄位

    # 先吃備註，再按候選豐富度比例分配（連續分段），不足則平均
    per_day_city: Dict[int, str] = {}
    for d in range(1, days + 1):
        if d in day_city_plan:
            per_day_city[d] = norm_city(day_city_plan[d])

    remaining = [d for d in range(1, days + 1) if d not in per_day_city]
    if remaining:
        city_counts = {
            c: quick_count_candidates(c, start_date=str(fdict.get("旅遊日期") or ""))
            for c in ordered_cities
        }
        seq = allocate_days_by_density(len(remaining), ordered_cities, city_counts)
        for d, c in zip(remaining, seq):
            per_day_city[d] = c

    for d in range(1, days + 1):
        per_day_city.setdefault(d, ordered_cities[(d - 1) % len(ordered_cities)])

    # 寫回 state/profile
    profile["days"] = days
    profile["locations"] = ordered_cities
    state["per_day_city"] = per_day_city
    state["stay_city_plan"] = stay_city_plan
    state["profile"] = profile

    return {
        **state,
        "profile": profile,
        "form_type": fdict.get(
            "form_type", "group" if user_id == "group" else "personal"
        ),
        "created_at": fdict.get("created_at"),
        "trip_preference_id": fdict.get("trip_preference_id"),
    }


# =========================================
# Node 2：分析使用者喜好（兼容舊流程）
# =========================================
def analyze_preferences(state: Dict[str, Any]) -> Dict[str, Any]:
    profile = state.get("profile", {})
    form = profile.get("form", {})
    favorites = profile.get("favorites", [])
    exclude = form.get("避開條件", [])
    notes = form.get("備註", "").strip()

    # 若已產生 behavior_profile，直接採用其摘要
    bp = state.get("behavior_profile")
    if bp and bp.get("summary_zh"):
        profile["偏好分析"] = bp["summary_zh"]
        state["profile"] = profile
        return state

    if not favorites:
        summary = "使用者尚未收藏地點。"
    else:
        fav_text = "\n".join(
            [f"{f['name']} - {', '.join(f.get('tags', []))}" for f in favorites]
        )
        prompt = f"""以下是使用者收藏的地點與標籤：

{fav_text}

請分析這位使用者的旅遊偏好風格（以關鍵詞為主），例如：歷史、文青、美食，不超過 50 字。"""
        if exclude:
            prompt += f"\n請避開以下類型的地點：{', '.join(exclude)}。"
        if notes:
            prompt += f"\n使用者額外備註需求如下，請一併納入考量：{notes}"

        summary = call_gpt(prompt)

    profile["偏好分析"] = summary
    state["profile"] = profile
    return state


# =========================================
# Slot 與時間工具
# =========================================
def _t(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def _fmt(t: time) -> str:
    return t.strftime("%H:%M")

def _add_minutes(hhmm: str, minutes: int) -> str:
    base = datetime.combine(datetime.today(), _t(hhmm))
    return (base + timedelta(minutes=minutes)).time().strftime("%H:%M")

def derive_slots_from_time_range(
    time_range: Optional[str], days: int, start_date: Optional[str]
) -> List[Dict[str, Any]]:
    """
    根據「活動時間」推導每日時段（不再單獨抽出下午茶）。
    固定切為：早餐(07–09)、上午(09–12)、中午(12–14)、下午(14–17)、晚上(17–活動結束)。
    之後的排程會在「下午」時段內視情況安插咖啡/點心，但不再建立獨立『下午茶』與『晚餐前』區塊，避免重疊。
    """
    if not time_range or "-" not in time_range:
        time_range = "09:00-21:00"
    start_s, end_s = time_range.split("-")
    start, end = _t(start_s.strip()), _t(end_s.strip())

    def clip(a, b):
        st = max(a, start)
        ed = min(b, end)
        if st >= ed:
            return None
        return (st.strftime("%H:%M"), ed.strftime("%H:%M"))

    base = [
        ("早餐", clip(_t("07:00"), _t("09:00"))),
        ("上午", clip(_t("09:00"), _t("12:00"))),
        ("中午", clip(_t("12:00"), _t("14:00"))),
        ("下午", clip(_t("14:00"), _t("17:00"))),
        ("晚上", clip(_t("17:00"), end)),
    ]
    slots = [(lab, w) for (lab, w) in base if w]

    if start_date:
        try:
            d0 = datetime.fromisoformat(start_date).date()
        except Exception:
            d0 = datetime.today().date()
    else:
        d0 = datetime.today().date()

    days_slots: List[Dict[str, Any]] = []
    for i in range(days):
        di = (d0 + timedelta(days=i)).isoformat()
        days_slots.append({
            "date": di,
            "slots": [{"label": lab, "window": w} for lab, w in slots]
        })
    return days_slots

def parse_time_str(tstr: str) -> time:
    return datetime.strptime(tstr, "%H:%M").time()

def estimate_visit_duration(place_types: List[str], rating_count: int) -> int:
    if not rating_count:
        return 90
    if any(t in place_types for t in ["restaurant", "food", "cafe", "meal", "night_market", "bakery"]):
        return 60
    if any(t in place_types for t in ["museum", "art_gallery", "church", "park"]):
        return 120 if rating_count > 1000 else 90
    if "shopping_mall" in place_types or "night_club" in place_types:
        return 90
    return 60 if rating_count < 300 else 90

# ✅ 修改：降低評論數門檻
def _min_reviews_for_slot(slot_label: str) -> int:
    """依時段調整評論數門檻（讓早餐/下午小店不至於被掃掉）"""
    return 10  # 統一降低到 10

def _addr_is_bad(addr: Optional[str]) -> bool:
    if not addr:
        return True
    s = str(addr)
    return ("Unnamed Road" in s) or (s.strip() == "-") or (s.strip().lower() == "none")

def _norm_addr(*vals) -> str:
    for v in vals:
        if v and not _addr_is_bad(v):
            return v
    return "未提供"


# 取代/覆蓋原本的 _map_url：就算沒有經緯度，也能保底用 place_id 生出連結
def _map_url(pid: str, det: Dict[str, Any], loc: Dict[str, Any], fallback_url: Optional[str]) -> str:
    lat = (loc or {}).get("lat")
    lng = (loc or {}).get("lng")
    if det.get("url"):
        return det["url"]
    if fallback_url:
        return fallback_url
    if lat is not None and lng is not None:
        return build_map_url(pid, lat, lng)
    # 保底：僅以 place_id 產出 Google Maps 連結（不需要座標）
    if pid:
        return f"https://www.google.com/maps/place/?q=place_id:{pid}"
    return ""

# 新增：產生「上一點 → 下一點」的 Google 導航連結
def _directions_url(origin: Optional[Dict[str, float]], dest: Optional[Dict[str, float]], mode: str) -> str:
    try:
        if (not origin) or (not dest):
            return ""
        olat, olng = float(origin["lat"]), float(origin["lng"])
        dlat, dlng = float(dest["lat"]), float(dest["lng"])
        tmode = "transit" if mode == "transit" else ("walking" if mode == "walking" else ("bicycling" if mode == "bicycling" else "driving"))
        return f"https://www.google.com/maps/dir/?api=1&origin={olat},{olng}&destination={dlat},{dlng}&travelmode={tmode}"
    except Exception:
        return ""

# 新增：模式中文顯示
def _mode_zh(mode: str) -> str:
    return {"driving":"開車","walking":"步行","bicycling":"單車","transit":"大眾運輸"}.get(mode, "交通")

# ✅ 修改：改為關閉快停靠禁用（或改為更寬鬆的判斷）
BAN_QUICK_STOPS = False  # 改為 False

# 新增：判斷是否為「快停靠」類型（手搖飲、速食、超商等）
def _is_quick_stop(p: Dict[str, Any]) -> bool:
    name = (p.get("name") or "").lower()
    # 品牌關鍵字（含中英混寫）
    kw = [
        "50嵐","50lan",
        "coco","都可",
        "清心","清心福全",
        "迷客夏","milksha",
        "星巴克","starbucks",
        "再睡5分鐘","康青龍",
        "可不可","kebuke",
        "一芳","yifang",
        "珍煮丹","tigersugar","老虎堂","tiger sugar",
        "鹿角巷","the alley",
        "comebuy","chatime","日出茶太",
        "麻古","macu",
        "麥當勞","mcdonald",
        "kfc","肯德基",
        "7-eleven","7-11","7 eleven",
        "全家","familymart",
        "茶湯會",
        "珍奶","手搖","飲料","豆花","冰品"
    ]
    if any(k.lower() in name for k in kw):
        return True
    types = set(p.get("types", []) or [])
    quick_types = {"convenience_store","fast_food"}
    return len(quick_types & types) > 0


# ✅ 修改：放寬距離與搜尋半徑
def _cfg(state_form: Dict[str, Any]) -> Dict[str, Any]:
    trav = state_form.get("travel", {}) if isinstance(state_form, dict) else {}
    # 交通模式優先取 travel.mode，其次 form['transport'] / form['交通方式']
    raw_mode = (trav.get("mode")
                or state_form.get("transport")
                or state_form.get("交通方式")
                or "driving")
    m = str(raw_mode).lower()
    if m in ["car", "drive", "driving", "汽車", "開車", "自駕"]:
        m = "driving"
    elif m in ["transit", "public", "bus", "metro", "train", "大眾運輸", "公共運輸", "捷運", "電車"]:
        m = "transit"
    elif m in ["walking", "walk", "步行"]:
        m = "walking"
    elif m in ["bicycling", "bike", "單車", "腳踏車", "自行車"]:
        m = "bicycling"
    else:
        m = "driving"

    return {
        "mode": m,
        "max_leg": int(trav.get("max_leg_minutes", 50)),  # ✅ 從 20 改為 50
        "search_radius": int(trav.get("search_radius_m", 3000)),  # ✅ 從 1200 改為 3000
        # ➕ 新增：步調與角色
        "pace": (state_form.get("pace") or trav.get("pace") or "normal"),
        "persona": state_form.get("persona") or trav.get("persona") or [],
    }

def is_open_covering_duration(
    hours_obj: Optional[Dict[str, Any]],
    date_str: str,
    start_hhmm: str,
    stay_minutes: int,
) -> bool:
    """檢查從 start_hhmm 起，是否連續營業至少 stay_minutes。"""
    try:
        s = _t(start_hhmm)
        e = _t(_add_minutes(start_hhmm, max(1, stay_minutes)))
        return is_open_during_slot(
            hours_obj,
            (s.strftime("%H:%M"), e.strftime("%H:%M")),
            date_str=date_str,
            require_full_cover=True,
        )
    except Exception:
        return True  # 沒資料就放行

# =========================================
# Node 3：生成每日時段行程（多城市）
# =========================================
def generate_daily_slots(state: Dict[str, Any]) -> Dict[str, Any]:
    f = state["profile"]["form"]

    # --- cost guard: in-memory caches & knobs ---
    DETAIL_TOPK_CHECK = 8        # 每批候選最多打幾次 get_place_details（命中即停）
    GPT_NAME_TOPK = 6            # LLM 名單最多驗證幾個
    DM_TOPN = 20                 # 丟距離矩陣前，只對前 N 個打
    DM_LAX_FACTOR = 1.6          # 累加在 Haversine 粗估上的寬鬆倍數

    _detail_cache, _geocode_cache, _travel_cache = {}, {}, {}

    # ✅ 修改：放寬類型限制與門檻
    ALLOWED_TYPES = {
        "tourist_attraction", "museum", "park", "art_gallery", "landmark",
        "zoo", "aquarium", "buddhist_temple", "church", "hindu_temple",
        "amusement_park", "beach", "hiking_area"
    }
    AVOID_TYPES = {
        "convenience_store", "gas_station", "atm", "parking", "car_repair", "train_station"
    }
    # ✅ 基礎門檻降低
    MIN_RATING_FALLBACK = 3.8  # 從 3.8 降為 3.5
    MIN_REVIEWS_FALLBACK = 50  # 從 100 降為 20

    # ✅ 修改：類型檢查改為黑名單制
    def _place_types_ok(types: list[str]) -> bool:
        ts = set(types or [])
        # 只要不在避開類型中就接受
        if ts & AVOID_TYPES:
            return False
        return True

    def _is_shopping_like(types: list[str]) -> bool:
        ts = set(types or [])
        return bool(ts & {"shopping_mall", "night_market", "department_store"})

    def _friendly_open_text(opening_hours: dict | None, types: list[str]) -> str:
        if opening_hours:
            try:
                txt = format_google_hours(opening_hours)
                if txt:
                    return (txt.splitlines()[0] if "\n" in txt else txt)
            except Exception:
                pass
        s = set(types or [])
        if "park" in s:
            return "全天開放（戶外公園）"
        if "museum" in s or "art_gallery" in s:
            return "依館方公告（常見 09:00–17:00；週一多休）"
        if "landmark" in s or "tourist_attraction" in s:
            return "依現場公告，建議出發前確認"
        return "營業時間不明，建議出發前確認"

    def geocode_city_cached(city: str):
        if city not in _geocode_cache:
            _geocode_cache[city] = geocode_city(city)
        return _geocode_cache[city]

    def get_place_details_cached(pid: str) -> dict:
        if not pid:
            return {}
        if pid not in _detail_cache:
            _detail_cache[pid] = get_place_details(pid) or {}
        return _detail_cache[pid]

    def travel_time_cached(a: dict, b: dict, mode: str) -> int:
        # 正規化 mode，避免傳入奇怪字串導致 API 無法解析
        m = str(mode).lower() if mode else "driving"
        if m in ["car", "drive", "driving"]:
            m = "driving"
        elif m in ["transit", "public", "bus", "metro", "捷運", "大眾運輸"]:
            m = "transit"
        elif m in ["walk", "walking", "步行"]:
            m = "walking"
        elif m in ["bicycling", "bike", "單車", "腳踏車", "自行車"]:
            m = "bicycling"
        else:
            m = "driving"

        key = (round(a["lat"], 4), round(a["lng"], 4),
               round(b["lat"], 4), round(b["lng"], 4), m)

        if key not in _travel_cache:
            _travel_cache[key] = travel_time_minutes(a, b, m)

        return _travel_cache[key]

    def _approx_travel_minutes(anchor: Dict[str, float], loc: Dict[str, float], mode: str) -> int:
        """不用打距離矩陣，先用 Haversine 直線距離粗估分鐘數（便宜）。"""
        try:
            from math import radians, sin, cos, asin, sqrt
            lat1, lon1 = float(anchor["lat"]), float(anchor["lng"])
            lat2, lon2 = float(loc.get("lat")), float(loc.get("lng"))
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
            km = 6371.0 * 2.0 * asin(sqrt(a))
            # 依模式給個保守速度：開車 25km/h、走路 4.5、單車 12、大眾運輸 20
            if mode == "walking":
                speed = 4.5
            elif mode == "bicycling":
                speed = 12
            elif mode == "transit":
                speed = 20
            else:
                speed = 25
            return int(km / max(1e-6, speed) * 60) + 5  # +5 分鐘緩衝
        except Exception:
            return 999

    # ✅ 修改：城市檢查改為選用（暫時關閉）
    def _addr_in_city(addr: str, city: str) -> bool:
        """很輕量的城市比對，避免跨縣市（台/臺 同字視為相同）。"""
        if not addr or not city:
            return True  # ✅ 改為預設通過
        a = str(addr).replace("台", "臺")
        c = norm_city(city).replace("市", "").replace("縣", "")
        return c in a

    if isinstance(f, dict):
        days = int(f.get("旅遊天數", 1))
        locations_arr = to_locations_list(
            f.get("locations") or f.get("地點") or f.get("location") or ""
        )
        locations_text = "、".join(locations_arr)

        time_range = f.get("活動時間", "09:00-21:00")
        start_date = f.get("旅遊日期", "")
        form_prefs = f.get("preferences", [])
        avoid = "、".join(f.get("避開條件", [])) if f.get("避開條件") else "無"
        google_rating_min = float(f.get("google評分", 3.5))  # ✅ 改為 3.5
        meal_required = f.get("三餐安排", "是") == "是"
        budget = f.get("預算", 1000)
        transport = f.get("交通方式", "")  # 僅用於文案；實際模式走 cfg['mode']
        stay = f.get("住宿類型", "")
        people_info = f.get("人數與身分", "")
        cfg = _cfg(f)
    else:
        days = int(f.旅遊天數)
        locations_arr = to_locations_list(
            getattr(f, "locations", None) or getattr(f, "地點", None) or getattr(f, "location", None) or ""
        )
        locations_text = "、".join(locations_arr)

        time_range = getattr(f, "活動時間", "09:00-21:00")
        start_date = getattr(f, "旅遊日期", "")
        form_prefs = f.preferences
        avoid = "、".join(f.避開條件) if f.避開條件 else "無"
        google_rating_min = float(f.google評分)
        meal_required = f.三餐安排 == "是"
        budget = f.預算
        transport = f.交通方式
        stay = f.住宿類型
        people_info = f.人數與身分
        cfg = {"mode": "driving", "max_leg": 50, "search_radius": 3000}  # ✅ 修改預設值

    pref = state["profile"].get("偏好分析", "")
    if form_prefs:
        pref += "（問卷偏好：" + "、".join(form_prefs) + "）"

    # 推導每日 slots
    days_slots = derive_slots_from_time_range(time_range, days, start_date)

    # 每日城市（由 extract_profile 計算）
    per_day_city: Dict[int, str] = state.get("per_day_city") or {}
    if not per_day_city:
        cities = locations_arr or ["高雄市"]
        per_day_city = {i + 1: cities[i % len(cities)] for i in range(days)}
        state["per_day_city"] = per_day_city

    used_place_ids: set = set()
    used_place_names: set = set()
    used_day_place_ids: List[set] = [set() for _ in range(days)]

    daily_md: Dict[str, str] = {}
    daily_struct: List[Dict[str, Any]] = []
    linked_head = None
    linked_prev = None

    first_hop_limit = cfg["max_leg"] + 10  # ✅ 第一站再放寬一點

    # 非餐段目標點數（讓行程更扎實）
    TARGET_PER_SLOT = {"上午": 2, "下午": 2}

    for d_idx, day in enumerate(days_slots):
        prev_slot_last_ll = None  # 記錄上一段最後一點座標
        d_no = d_idx + 1
        # 每日城市
        day_city = per_day_city.get(d_no) or ((locations_arr or [""])[0])
        md = f"## 第{d_no}天 · {day_city}\n"
        day_date = day["date"]
        day_struct = {"date": day_date, "city": day_city, "slots": []}

        # === 短期修正：每日商圈/夜市上限 1（避免重複性高） ===
        shopping_like_used = 0

        # 當日錨點：該市中心
        city_center = geocode_city_cached(day_city)
        cur_anchor = {"lat": city_center[0], "lng": city_center[1]} if city_center else None

        for si, slot in enumerate(day["slots"]):
            label = slot["label"]
            win = slot["window"]
            slot_is_meal = label in MEAL_SLOT_LABELS
            slot_md = f"### {label}（{win[0]}–{win[1]}）\n"

            pre_slot_anchor = cur_anchor.copy() if cur_anchor else None  # 目前未顯示第一段導航，可視需求使用
            selected_places: List[Dict[str, Any]] = []

            # ---------- 餐段 ----------
            if slot_is_meal and meal_required:
                radius_seq = [
                    max(600, cfg["search_radius"] // 2),
                    cfg["search_radius"],
                    int(cfg["search_radius"] * 1.5),
                    cfg["search_radius"] * 2,
                ]
                chosen = None
                limit_minutes = first_hop_limit if si == 0 else cfg["max_leg"]
                stay_min = 60

                for r in radius_seq:
                    picks = search_meal_places(
                        city=day_city,
                        slot_label=label,
                        date=day_date,
                        start_hhmm=win[0].replace(":", ""),
                        end_hhmm=win[1].replace(":", ""),
                        center_latlng=(cur_anchor["lat"], cur_anchor["lng"])
                        if cur_anchor
                        else None,
                        radius_m=r,
                    )
                    if not picks:
                        continue

                    print(f"[DEBUG] 餐廳候選總數：{len(picks)}")
                    
                    # --- 便宜粗篩（不打矩陣/不打 details） ---
                    pre = []
                    for p in picks:
                        # ➤ 禁用連鎖手搖/速食/超商
                        if BAN_QUICK_STOPS and _is_quick_stop(p):
                            print(f"  ❌ 快停靠：{p.get('name')}")
                            continue
                        pid = p.get("place_id")
                        if not pid or pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                            print(f"  ❌ 重複：{p.get('name')}")
                            continue
                        rating = float(p.get("rating") or 0.0)
                        reviews = int(p.get("user_ratings_total") or 0)
                        if reviews < _min_reviews_for_slot(label) or rating < google_rating_min:
                            print(f"  ❌ 評論/評分不足({reviews}/{rating})：{p.get('name')}")
                            continue
                        pre.append(p)
                    
                    print(f"[DEBUG] 通過粗篩：{len(pre)} 個")
                    
                    if not pre:
                        continue

                    # 有錨點 → 只對前 DM_TOPN 打距離矩陣
                    if cur_anchor:
                        pre.sort(key=lambda c: c.get("_approx_minutes", 999))
                        pre = pre[:DM_TOPN]
                        picks = annotate_travel_minutes(pre, cur_anchor, cfg["mode"])
                    else:
                        picks = pre

                    # 依「偏好 → 距離 → 評分 → 評論數」排序
                    bp = state.get("behavior_profile", {})
                    def _behav(m):
                        tags = map_types_to_tags(m.get("types", []))
                        return score_by_behavior(tags, day_city, bp, alpha=0.6, beta=0.2, gamma=-0.15)

                    sorted_picks = sorted(
                        picks,
                        key=lambda c: (
                            -_behav(c),
                            c.get("travel_minutes_from_anchor", 999),
                            -(float(c.get("rating", 0) or 0)),
                            -(int(c.get("user_ratings_total", 0) or 0)),
                        ),
                    )

                    # 只對 Top-K 打 details，命中即停
                    for c in sorted_picks[:min(8, len(sorted_picks))]:  # ✅ 從 3 改為 8
                        pid = c["place_id"]
                        det = get_place_details_cached(pid)
                        if not det:
                            print(f"  ❌ details 失敗：{c.get('name')}")
                            continue
                        # ➤ 再次保險：禁用快停靠
                        if BAN_QUICK_STOPS and _is_quick_stop({"name": det.get("name") or c.get("name",""),
                                                            "types": det.get("types") or c.get("types", [])}):
                            print(f"  ❌ 快停靠（details）：{det.get('name')}")
                            continue
                        addr = det.get("formatted_address") or c.get("formatted_address")
                        if _addr_is_bad(addr):
                            print(f"  ❌ 地址爛：{det.get('name')} | {addr}")
                            continue
                        # ✅ 註解掉地址城市檢查
                        # if not _addr_in_city(addr, day_city):
                        #     print(f"  ❌ 地址城市不符：{det.get('name')} | {addr}")
                        #     continue
                        c["_det"] = det
                        chosen = c
                        print(f"  ✅ 選中餐廳：{det.get('name')}")
                        break

                if chosen:
                    det = chosen.get("_det") or get_place_details_cached(chosen["place_id"]) or {}
                    loc = (det.get("geometry") or {}).get("location") or (chosen.get("location") or {})
                    try:
                        open_line = opening_line_for_date(det.get("opening_hours"), day_date)
                    except Exception:
                        open_line = _friendly_open_text(det.get("opening_hours"), det.get("types") or chosen.get("types", []))

                    duration = stay_min
                    item = {
                        "place_id": chosen["place_id"],
                        "name": det.get("name") or chosen.get("name"),
                        "category": "meal",
                        "stay_minutes": duration,
                        "rating": float(det.get("rating", 0) or chosen.get("rating", 0) or 0),
                        "reviews": int(
                            det.get("user_ratings_total", 0)
                            or chosen.get("user_ratings_total", 0)
                            or 0
                        ),
                        "address": _norm_addr(det.get("formatted_address"), chosen.get("formatted_address")),
                        "map_url": _map_url(chosen["place_id"], det, loc, chosen.get("google_maps_url")),
                        "open_text": open_line,
                        "types": det.get("types") or chosen.get("types", []),
                        "lat": loc.get("lat"),
                        "lng": loc.get("lng"),
                        "source": "gm_meal",
                    }
                    print(f"🍽️ 選到 {label}：{item['name']} | {item['address']}")
                    selected_places.append(item)

                    used_place_ids.add(chosen["place_id"])
                    used_place_names.add(item["name"])
                    used_day_place_ids[d_idx].add(chosen["place_id"])
                    cur_anchor = {"lat": item["lat"], "lng": item["lng"]}

                    # 晚上：飯後若還有時間，補一個夜間活動（夜市 / 散步 / 夜景咖啡廳 隨機選一）
                    if label == "晚上":
                        try:
                            start_t, end_t = map(parse_time_str, win)
                            slot_len = (
                                datetime.combine(datetime.today(), end_t)
                                - datetime.combine(datetime.today(), start_t)
                            ).seconds // 60
                            extra = slot_len - duration - 10

                            if extra >= 60:
                                import random
                                NIGHT_OPTIONS = [
                                    {"keyword": "夜市", "types": ["night_market", "shopping_mall"]},
                                    {"keyword": "散步", "types": ["tourist_attraction", "park"]},
                                    {"keyword": "夜景", "types": ["cafe", "restaurant", "tourist_attraction"]},
                                ]
                                opt = random.choice(NIGHT_OPTIONS)

                                night = search_store_candidates(
                                    keyword=opt["keyword"],
                                    city=day_city,
                                    center_latlng=(loc.get("lat"), loc.get("lng")),
                                    radius_m=max(800, int(cfg["search_radius"] * 1.2)),
                                    types=opt["types"],
                                    limit=6,
                                    min_rating=4.0,
                                    min_reviews=30,
                                    date=day_date,
                                    slot_range=(win[0], win[1]),
                                    require_full_cover=False,
                                    mode=cfg["mode"],
                                ) or []

                                night = annotate_travel_minutes(
                                    night, {"lat": loc.get("lat"), "lng": loc.get("lng")}, cfg["mode"]
                                ) if night else []

                                night.sort(
                                    key=lambda c: (
                                        c.get("travel_minutes_from_anchor", 999),
                                        -(float(c.get("rating", 0) or 0)),
                                    )
                                )

                                for c in night:
                                    pid2 = c.get("place_id")
                                    if not pid2 or pid2 in used_place_ids:
                                        continue
                                    det2 = get_place_details_cached(pid2) or {}
                                    t2 = det2.get("types") or c.get("types", [])
                                    # 夜市/購物類地點若超過上限就跳過
                                    if _is_shopping_like(t2) and shopping_like_used >= 1:
                                        continue
                                    loc2 = (det2.get("geometry") or {}).get("location") or (c.get("location") or {})
                                    addr2 = det2.get("formatted_address") or c.get("formatted_address")
                                    if _addr_is_bad(addr2):
                                        continue
                                    # ✅ 城市檢查已改為選用
                                    if not _addr_in_city(addr2, day_city):
                                        continue

                                    item2 = {
                                        "place_id": pid2,
                                        "name": det2.get("name") or c.get("name"),
                                        "category": "stroll",
                                        "stay_minutes": min(60, extra),
                                        "rating": float(det2.get("rating", 0) or c.get("rating", 0) or 0),
                                        "reviews": int(
                                            det2.get("user_ratings_total", 0) or c.get("user_ratings_total", 0) or 0
                                        ),
                                        "address": _norm_addr(det2.get("formatted_address"), c.get("formatted_address")),
                                        "map_url": _map_url(pid2, det2, loc2, c.get("google_maps_url")),
                                        "open_text": _friendly_open_text(det2.get("opening_hours"), t2),
                                        "types": t2,
                                        "lat": loc2.get("lat"),
                                        "lng": loc2.get("lng"),
                                        "source": "gm_night",
                                    }

                                    if _is_shopping_like(item2.get("types", [])):
                                        shopping_like_used += 1
                                    selected_places.append(item2)
                                    used_place_ids.add(pid2)
                                    used_place_names.add(item2["name"])
                                    used_day_place_ids[d_idx].add(pid2)
                                    break
                        except Exception:
                            pass

                else:
                    # fallback
                    fallback = fallback_place_from_backup(
                        city=day_city,
                        slot_label=label,
                        slot_window=win,
                        date=day_date,
                        used_place_ids=used_place_ids,
                    )
                    # ➤ 禁用快停靠（保險）
                    if fallback and BAN_QUICK_STOPS and _is_quick_stop({"name": fallback.get("name",""),
                                                                        "types": fallback.get("types", [])}):
                        fallback = None

                    if fallback:
                        loc = (fallback.get("geometry") or {}).get("location") or {}
                        item = {
                            "place_id": fallback["place_id"],
                            "name": fallback["name"],
                            "category": "meal",
                            "stay_minutes": stay_min,
                            "rating": max(float(fallback.get("rating", 0) or 0), MIN_RATING_FALLBACK),
                            "reviews": int(fallback.get("reviews", 0) or 0),
                            "address": fallback["address"],
                            "map_url": build_map_url(fallback["place_id"], loc.get("lat"), loc.get("lng")),
                            "open_text": "（自動補齊推薦）",
                            "types": fallback.get("types", []),
                            "lat": loc.get("lat"),
                            "lng": loc.get("lng"),
                            "source": "gm_fallback",
                        }
                        selected_places.append(item)
                        used_place_ids.add(item["place_id"])
                        used_place_names.add(item["name"])
                        used_day_place_ids[d_idx].add(item["place_id"])
                        cur_anchor = {"lat": item["lat"], "lng": item["lng"]}
                        print(f"🛠️ 餐廳 fallback 成功：{item['name']}")
                    else:
                        slot_md += "- ⚠️ 未能找到合適餐廳（含 fallback 皆失敗）\n"

            # ---------- 非餐段 ----------
            if not slot_is_meal:
                start_t, end_t = map(parse_time_str, win)
                max_slot_minutes = (
                    datetime.combine(datetime.today(), end_t)
                    - datetime.combine(datetime.today(), start_t)
                ).seconds // 60
                remaining_minutes = max_slot_minutes
                chosen_any = False
                limit_minutes = first_hop_limit if si == 0 else cfg["max_leg"]
                target_n = TARGET_PER_SLOT.get(label, 1)

                # === 下午段優先塞 1 間咖啡（可塞才塞） ===========================
                CAFE_MIN = 45
                CAFE_MAX = 90
                CAFE_BUF = 10

                def _fits_slot(rem_min: int, need_min: int, buf: int = CAFE_BUF) -> bool:
                    return (rem_min - (need_min + buf)) >= 0

                if label == "下午" and remaining_minutes >= CAFE_MIN:
                    try:
                        radius_seq = [
                            max(600, cfg["search_radius"] // 2),
                            cfg["search_radius"],
                            int(cfg["search_radius"] * 1.5),
                            cfg["search_radius"] * 2,
                        ]
                        cafe_item = None
                        leg_for_cafe = 0

                        for r in radius_seq:
                            picks = search_meal_places(
                                city=day_city,
                                slot_label="下午茶",
                                date=day_date,
                                start_hhmm=win[0].replace(":", ""),
                                end_hhmm=win[1].replace(":", ""),
                                center_latlng=(cur_anchor["lat"], cur_anchor["lng"]) if cur_anchor else None,
                                radius_m=r,
                            )
                            if not picks:
                                continue

                            # --- 便宜粗篩，先看評分/評論與直線距離，僅對 TopN 才打矩陣 ---
                            pre = []
                            min_reviews = _min_reviews_for_slot("下午茶")
                            for p in picks:
                                # ➤ 禁用快停靠
                                if BAN_QUICK_STOPS and _is_quick_stop(p):
                                    continue
                                pid = p.get("place_id")
                                if not pid or pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                                    continue
                                rating = float(p.get("rating") or 0.0)
                                reviews = int(p.get("user_ratings_total") or 0)
                                if reviews < min_reviews or rating < 4.0:
                                    continue
                                if cur_anchor and p.get("location"):
                                    approx_min = _approx_travel_minutes(cur_anchor, p["location"], cfg["mode"])
                                    if approx_min > int(limit_minutes * DM_LAX_FACTOR):
                                        continue
                                    p["_approx_minutes"] = approx_min
                                pre.append(p)
                            if not pre:
                                continue
                            if cur_anchor:
                                pre.sort(key=lambda c: c.get("_approx_minutes", 999))
                                pre = pre[:DM_TOPN]
                                picks = annotate_travel_minutes(pre, cur_anchor, cfg["mode"])
                            else:
                                picks = pre
                            # ----------------------------------------------------------------

                            bp = state.get("behavior_profile", {})
                            def _behav(c):
                                tags = map_types_to_tags(c.get("types", []))
                                return score_by_behavior(tags, day_city, bp, alpha=0.6, beta=0.2, gamma=-0.15)

                            sorted_picks = sorted(
                                picks,
                                key=lambda c: (
                                    -_behav(c),
                                    c.get("travel_minutes_from_anchor", 0 if not cur_anchor else 999),
                                    -(float(c.get("rating") or 0)),
                                    -(int(c.get("user_ratings_total") or 0)),
                                ),
                            )

                            for c in sorted_picks:
                                pid = c.get("place_id")
                                if not pid or pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                                    continue

                                det = get_place_details_cached(pid) or {}
                                # ➤ 再擋一次（保險）
                                if BAN_QUICK_STOPS and _is_quick_stop({"name": det.get("name") or c.get("name",""),
                                                                       "types": det.get("types") or c.get("types", [])}):
                                    continue
                                loc = (det.get("geometry") or {}).get("location") or {}
                                if not loc:
                                    continue

                                reviews = int(det.get("user_ratings_total", 0) or 0)
                                rating = float(det.get("rating", 0) or 0)
                                if reviews < _min_reviews_for_slot("下午茶"):
                                    continue
                                if rating < 4.0:
                                    continue

                                if cur_anchor:
                                    leg = int(c.get("travel_minutes_from_anchor", 999))
                                    if leg > limit_minutes:
                                        continue
                                else:
                                    leg = 0

                                base = 60
                                dur = min(CAFE_MAX, max(CAFE_MIN, base))
                                if not _fits_slot(remaining_minutes, leg + dur, CAFE_BUF):
                                    dur = CAFE_MIN
                                    if not _fits_slot(remaining_minutes, leg + dur, CAFE_BUF):
                                        continue

                                try:
                                    open_line = opening_line_for_date(det.get("opening_hours"), day_date)
                                except Exception:
                                    open_line = _friendly_open_text(det.get("opening_hours"), det.get("types", []))

                                url = build_map_url(pid, loc.get("lat"), loc.get("lng"))
                                cafe_item = {
                                    "place_id": pid,
                                    "name": det.get("name") or c.get("name"),
                                    "category": "meal",
                                    "stay_minutes": dur,
                                    "rating": rating,
                                    "reviews": reviews,
                                    "address": _norm_addr(det.get("formatted_address"), c.get("formatted_address")),
                                    "map_url": url,
                                    "open_text": open_line,
                                    "types": det.get("types") or c.get("types", []),
                                    "lat": loc.get("lat"),
                                    "lng": loc.get("lng"),
                                    "source": "gm_cafe",
                                }
                                leg_for_cafe = leg
                                break
                            if cafe_item:
                                break

                        if cafe_item:
                            selected_places.append(cafe_item)
                            used_place_ids.add(cafe_item["place_id"])
                            used_place_names.add(cafe_item["name"])
                            used_day_place_ids[d_idx].add(cafe_item["place_id"])
                            remaining_minutes -= (leg_for_cafe + cafe_item["stay_minutes"] + CAFE_BUF)
                            if remaining_minutes < 0:
                                remaining_minutes = 0
                            cur_anchor = {"lat": cafe_item["lat"], "lng": cafe_item["lng"]}
                            # 咖啡後若還有 >=40 分鐘，就允許再補 1 個景點
                            allow_more_after_cafe = (remaining_minutes >= 40)
                            chosen_any = not allow_more_after_cafe
                    except Exception as _e:
                        print("⚠️ 下午咖啡優先流程例外：", _e)
                # === 下午段優先塞咖啡（END） ======================================

                # 一般觀光段：LLM 名單 → 驗證 + 距離門檻；失敗用 Nearby 補
                if (not chosen_any) or (label == "下午" and 'allow_more_after_cafe' in locals() and allow_more_after_cafe):
                    avoid_text = "、".join(used_place_names) if used_place_names else "無"

                    # ✅ 修改：Prompt 改為更寬鬆
                    prompt_lines = [
                        f"你是一位專業的中文旅遊行程規劃助手，請針對這個時段，在「{day_city}」市推薦 8 個值得造訪的地點。",
                        "【規則】",
                        "1) 必須是真實景點且可在 Google Maps 驗證。",
                        "2) 優先推薦：博物館、公園、藝術館、觀光景點、地標建築。",
                        "3) 避免推薦：便利商店、加油站、停車場。",
                        "4) 若找不到特定名稱，請回傳同城市內的其他景點，不得留空。",
                        "5) 僅回傳 markdown list，每行只有地點名稱，不加描述。",
                        "",
                        "【避免重複】",
                        f"- 當日已選：{avoid_text}",
                        "",
                        "【時間條件】",
                        f"- 需能在 {win[0]}–{win[1]} 合理造訪或入場。",
                    ]
                    names_md = call_gpt("\n".join(prompt_lines))
                    candidate_names = [
                        clean_place_name(m.strip())
                        for m in re.findall(r"-\s*(.+)", names_md)
                    ][:GPT_NAME_TOPK]  # 控制成本：只驗證前 K 個

                    # LLM 候選失靈 → 用附近 attraction 候選補
                    fallback_batches: List[Dict[str, Any]] = []
                    if not candidate_names and cur_anchor:
                        for r in [
                            max(800, cfg["search_radius"] // 2),
                            cfg["search_radius"],
                            int(cfg["search_radius"] * 1.6),
                        ]:
                            part = search_attraction_candidates(
                                day_city,
                                (cur_anchor["lat"], cur_anchor["lng"]),
                                radius_m=r,
                                limit=12,
                                min_rating=max(google_rating_min, MIN_RATING_FALLBACK),
                                min_reviews=40,
                                date=day_date,
                                slot_range=(win[0], win[1]),
                                require_full_cover=False,
                                mode=cfg["mode"],
                            )
                            if part:
                                fallback_batches = part
                                break

                    limit_minutes = first_hop_limit if si == 0 else cfg["max_leg"]

                    # 1) 先嘗試：LLM 名單逐一驗證 + 距離門檻（扣除移動時間）
                    for raw_name in candidate_names:
                        print(f"[景點驗證] 開始檢查：{raw_name}")
                        gplace = search_place(raw_name, day_city) or (
                            search_place(raw_name, locations_arr[0]) if locations_arr else None
                        )
                        if not gplace:
                            print(f"  ❌ search_place 失敗：{raw_name}")
                            continue
                        
                        pid = gplace.get("place_id")
                        if not pid:
                            print(f"  ❌ 無 place_id：{raw_name}")
                            continue
                        if pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                            print(f"  ❌ 已使用過：{raw_name} (pid={pid[:20]}...)")
                            continue

                        det = get_place_details_cached(pid) or {}
                        if not det:
                            print(f"  ❌ get_place_details 失敗：{raw_name}")
                            continue
                        
                        types = det.get("types", [])
                        print(f"  📍 類型：{types[:5]}")
                        
                        # ✅ 類型白名單檢查（已改為黑名單制）
                        if not _place_types_ok(types):
                            print(f"  ❌ 類型不符（在黑名單中）：{raw_name}")
                            continue
                        
                        # 商圈/夜市類型一天只允許 1 次
                        if _is_shopping_like(types) and shopping_like_used >= 1:
                            print(f"  ❌ 商圈/夜市已達上限：{raw_name}")
                            continue
                        
                        # ➤ 禁用快停靠
                        if BAN_QUICK_STOPS and _is_quick_stop({"name": det.get("name") or gplace.get("name",""),
                                                            "types": types}):
                            print(f"  ❌ 快停靠：{raw_name}")
                            continue

                        rating_count = det.get("user_ratings_total", gplace.get("user_ratings_total", 0))
                        rating_val = float(det.get("rating", 0.0) or 0.0)
                        print(f"  ⭐ 評分/評論：{rating_val}/{rating_count}")
                        
                        # ✅ 門檻已降低
                        if (rating_val < google_rating_min) and (rating_val < MIN_RATING_FALLBACK) and (rating_count < MIN_REVIEWS_FALLBACK):
                            print(f"  ❌ 評分/評論過低：{rating_val} < {google_rating_min}/{MIN_RATING_FALLBACK}, {rating_count} < {MIN_REVIEWS_FALLBACK}")
                            continue
                        
                        min_reviews = _min_reviews_for_slot(label)
                        if rating_count < min_reviews:
                            print(f"  ❌ 評論數不足：{rating_count} < {min_reviews}")
                            continue

                        loc = (det.get("geometry") or {}).get("location") or {}
                        if not loc or loc.get("lat") is None or loc.get("lng") is None:
                            print(f"  ❌ 無座標：{raw_name}")
                            continue
                        
                        if cur_anchor and loc:
                            try:
                                leg = travel_time_cached(
                                    cur_anchor, 
                                    {"lat": float(loc.get("lat")), "lng": float(loc.get("lng"))}, 
                                    cfg["mode"]
                                )
                                print(f"  🚗 移動時間：{leg} 分鐘 (上限={limit_minutes})")
                                # ✅ 暫時註解距離檢查，看看是不是這裡卡住
                                # if leg > limit_minutes:
                                #     print(f"  ❌ 距離過遠：{leg} > {limit_minutes}")
                                #     continue
                            except Exception as e:
                                print(f"  ⚠️ 距離計算失敗：{e}")
                                leg = 0
                        else:
                            leg = 0
                            print(f"  ℹ️ 無錨點，跳過距離檢查")

                        addr = det.get("formatted_address", "")
                        if _addr_is_bad(addr):
                            print(f"  ❌ 地址無效：{addr}")
                            continue
                        
                        # ✅ 完全註解掉城市檢查
                        # if not _addr_in_city(addr, day_city):
                        #     print(f"  ❌ 地址城市不符：{addr} vs {day_city}")
                        #     continue
                        print(f"  📍 地址：{addr[:50]}...")

                        dur = estimate_visit_duration(types, rating_count)
                        total_need = leg + dur + 5
                        print(f"  ⏱️ 需時：移動{leg}+停留{dur}+緩衝5={total_need} 分鐘 (剩餘={remaining_minutes})")
                        
                        # ✅ 暫時註解時間檢查，看看能不能先產出行程
                        # if remaining_minutes - total_need < 0:
                        #     print(f"  ❌ 時間不足：需要{total_need}但只剩{remaining_minutes}")
                        #     continue

                        try:
                            open_line = opening_line_for_date(det.get("opening_hours"), day_date)
                        except Exception:
                            open_line = _friendly_open_text(det.get("opening_hours"), types)

                        url = _map_url(pid, det, loc, None)
                        item = {
                            "place_id": pid,
                            "name": det.get("name", raw_name),
                            "category": "attraction",
                            "stay_minutes": dur,
                            "rating": rating_val,
                            "reviews": int(det.get("user_ratings_total", 0) or 0),
                            "address": _norm_addr(det.get("formatted_address")),
                            "map_url": url,
                            "open_text": open_line,
                            "types": types,
                            "lat": loc.get("lat"),
                            "lng": loc.get("lng"),
                            "source": "gpt+gm",
                            "raw_name": raw_name,
                        }
                        item["_behavior_score"] = score_by_behavior(
                            map_types_to_tags(item.get("types", [])),
                            day_city,
                            state.get("behavior_profile", {}),
                            alpha=0.6,
                            beta=0.25,
                            gamma=-0.15,
                        )
                        
                        print(f"  ✅ 成功加入：{item['name']}")
                        selected_places.append(item)
                        selected_places.sort(key=lambda x: -float(x.get("_behavior_score", 0.0)))

                        remaining_minutes -= total_need
                        used_place_ids.add(pid)
                        used_place_names.add(item["name"])
                        used_day_place_ids[d_idx].add(pid)
                        cur_anchor = {"lat": item["lat"], "lng": item["lng"]}
                        
                        if _is_shopping_like(types):
                            shopping_like_used += 1
                        
                        if remaining_minutes <= 30 or len(selected_places) >= target_n:
                            print(f"  ℹ️ 停止：剩餘時間{remaining_minutes}或已達目標數{len(selected_places)}/{target_n}")
                            break

                    # 2) 再嘗試：Nearby attraction 候選（距離友善）
                    if (len(selected_places) == 0) and fallback_batches:
                        fb = (
                            annotate_travel_minutes(fallback_batches, cur_anchor, cfg["mode"])
                            if cur_anchor
                            else fallback_batches
                        )
                        bp = state.get("behavior_profile", {})
                        def _behav_cand(c):
                            return score_by_behavior(
                                map_types_to_tags(c.get("types", [])),
                                day_city,
                                bp,
                                alpha=0.6,
                                beta=0.25,
                                gamma=-0.15,
                            )
                        fb.sort(
                            key=lambda c: (
                                -_behav_cand(c),
                                c.get("travel_minutes_from_anchor", 999),
                                -(float(c.get("rating", 0) or 0)),
                                -(int(c.get("user_ratings_total", 0) or 0)),
                            )
                        )
                        cand = None
                        for c in fb:
                            # ✅ 類型白名單；且商圈/夜市上限
                            if not _place_types_ok(c.get("types", [])):
                                continue
                            if _is_shopping_like(c.get("types", [])) and shopping_like_used >= 1:
                                continue
                            # ➤ 禁用快停靠
                            if BAN_QUICK_STOPS and _is_quick_stop(c):
                                continue
                            pid = c.get("place_id")
                            if not pid or pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                                continue
                            if int(c.get("travel_minutes_from_anchor", 999)) > limit_minutes:
                                continue
                            if _addr_is_bad(c.get("formatted_address")):
                                continue
                            # ✅ 城市檢查已改為選用
                            if not _addr_in_city(c.get("formatted_address"), day_city):
                                continue
                            cand = c
                            break
                        if cand:
                            det = get_place_details_cached(cand["place_id"]) or {}
                            types2 = det.get("types") or cand.get("types", [])
                            loc = (det.get("geometry") or {}).get("location") or (cand.get("location") or {})
                            try:
                                open_line = opening_line_for_date(det.get("opening_hours"), day_date)
                            except Exception:
                                open_line = _friendly_open_text(det.get("opening_hours"), types2)
                            dur = estimate_visit_duration(
                                types2,
                                int(det.get("user_ratings_total", 0) or cand.get("user_ratings_total", 0) or 0),
                            )
                            leg = int(cand.get("travel_minutes_from_anchor", 0))
                            need = leg + dur + 5
                            if remaining_minutes - need >= 0:
                                url = _map_url(cand["place_id"], det, loc, cand.get("google_maps_url"))
                                item = {
                                    "place_id": cand["place_id"],
                                    "name": det.get("name") or cand.get("name"),
                                    "category": "attraction",
                                    "stay_minutes": dur,
                                    "rating": float(det.get("rating", 0) or cand.get("rating", 0) or 0),
                                    "reviews": int(det.get("user_ratings_total", 0) or cand.get("user_ratings_total", 0) or 0),
                                    "address": _norm_addr(det.get("formatted_address"), cand.get("formatted_address")),
                                    "map_url": url,
                                    "open_text": open_line,
                                    "types": types2,
                                    "lat": loc.get("lat"),
                                    "lng": loc.get("lng"),
                                    "source": "gm_nearby",
                                }
                                item["_behavior_score"] = score_by_behavior(
                                    map_types_to_tags(item.get("types", [])),
                                    day_city,
                                    state.get("behavior_profile", {}),
                                    alpha=0.6,
                                    beta=0.25,
                                    gamma=-0.15,
                                )
                                selected_places.append(item)
                                used_place_ids.add(cand["place_id"])
                                used_place_names.add(item["name"])
                                used_day_place_ids[d_idx].add(cand["place_id"])
                                cur_anchor = {"lat": item["lat"], "lng": item["lng"]}
                                remaining_minutes -= need
                                if _is_shopping_like(types2):
                                    shopping_like_used += 1

                    # 3) 若低於目標數，嘗試 nearby 再補（命中 target_n 或時間不足就停）
                    if len(selected_places) < target_n and remaining_minutes >= 45 and cur_anchor:
                        more = search_attraction_candidates(
                            day_city,
                            (cur_anchor["lat"], cur_anchor["lng"]),
                            radius_m=int(cfg["search_radius"] * 1.2),
                            limit=12,
                            min_rating=max(google_rating_min, 4.0),
                            min_reviews=30,
                            date=day_date,
                            slot_range=(win[0], win[1]),
                            require_full_cover=False,
                            mode=cfg["mode"],
                        ) or []

                        more = annotate_travel_minutes(more, cur_anchor, cfg["mode"]) if more else []
                        bp = state.get("behavior_profile", {})
                        def _behav_more(c):
                            return score_by_behavior(map_types_to_tags(c.get("types", [])), day_city, bp, alpha=0.6, beta=0.25, gamma=-0.15)

                        more.sort(key=lambda c: (
                            -_behav_more(c),
                            c.get("travel_minutes_from_anchor", 999),
                            -(float(c.get("rating", 0) or 0)),
                            -(int(c.get("user_ratings_total", 0) or 0)),
                        ))

                        for c in more:
                            # ✅ 類型白名單；且商圈/夜市上限
                            if not _place_types_ok(c.get("types", [])):
                                continue
                            if _is_shopping_like(c.get("types", [])) and shopping_like_used >= 1:
                                continue
                            # ➤ 禁用快停靠
                            if BAN_QUICK_STOPS and _is_quick_stop(c):
                                continue
                            pid = c.get("place_id")
                            if (not pid) or pid in used_place_ids or pid in used_day_place_ids[d_idx]:
                                continue
                            if int(c.get("travel_minutes_from_anchor", 999)) > limit_minutes:
                                continue
                            if _addr_is_bad(c.get("formatted_address")):
                                continue
                            # ✅ 城市檢查已改為選用
                            if not _addr_in_city(c.get("formatted_address"), day_city):
                                continue

                            det = get_place_details_cached(pid) or {}
                            types3 = det.get("types") or c.get("types", [])
                            loc = (det.get("geometry") or {}).get("location") or (c.get("location") or {})
                            dur = estimate_visit_duration(
                                types3,
                                int(det.get("user_ratings_total", 0) or c.get("user_ratings_total", 0) or 0)
                            )

                            leg = int(c.get("travel_minutes_from_anchor", 0))
                            need = leg + dur + 5
                            if remaining_minutes - need < 0:
                                continue

                            try:
                                open_line = opening_line_for_date(det.get("opening_hours"), day_date)
                            except Exception:
                                open_line = _friendly_open_text(det.get("opening_hours"), types3)

                            item = {
                                "place_id": pid,
                                "name": det.get("name") or c.get("name"),
                                "category": "attraction",
                                "stay_minutes": dur,
                                "rating": float(det.get("rating", 0) or c.get("rating", 0) or 0),
                                "reviews": int(det.get("user_ratings_total", 0) or c.get("user_ratings_total", 0) or 0),
                                "address": _norm_addr(det.get("formatted_address"), c.get("formatted_address")),
                                "map_url": _map_url(pid, det, loc, c.get("google_maps_url")),
                                "open_text": open_line,
                                "types": types3,
                                "lat": loc.get("lat"),
                                "lng": loc.get("lng"),
                                "source": "gm_nearby_fill",
                            }
                            selected_places.append(item)
                            used_place_ids.add(pid); used_place_names.add(item["name"]); used_day_place_ids[d_idx].add(pid)
                            cur_anchor = {"lat": item["lat"], "lng": item["lng"]}
                            remaining_minutes -= need
                            if _is_shopping_like(types3):
                                shopping_like_used += 1
                            if len(selected_places) >= target_n or remaining_minutes <= 30:
                                break

                if not selected_places:
                    slot_md += "- ⚠️ 無法找到符合條件的地點（LLM/Google 均未命中或距離過遠）\n"

            # ---------- 路徑最佳化（每個時段內） ----------
            if len(selected_places) >= 3:
                opt = optimize_visit_order(selected_places, start_idx=0, mode=None)
                order = opt["order"]
                total_mins = int(opt["total_travel_secs"] / 60)
                selected_places = [selected_places[i] for i in order]
                mode_str = opt.get('mode') or cfg.get('mode') or '-'
                slot_md += f"  - 🚶 預估移動：{total_mins} 分鐘（{mode_str}）\n"
            else:
                total_mins = 0  # 供下方配平緩衝使用

            # ---------- 停留時間配平 ----------
            try:
                from datetime import datetime as _dt
                _s = _dt.strptime(win[0], "%H:%M"); _e = _dt.strptime(win[1], "%H:%M")
                slot_minutes = int((_e - _s).total_seconds() // 60)
                travel_buffer = int(total_mins)

                user_ctx = {'pace': cfg.get('pace', 'normal'), 'persona': cfg.get('persona', [])}
                slot_ctx = {'slot_name': label, 'start': win[0], 'end': win[1],
                            'date': day_date, 'travel_buffer_min': travel_buffer}

                windows = []
                for p in selected_places:
                    w = estimate_stay_window(p, slot_ctx, user_ctx)

                    # 仍保留快停靠縮短（理論上已被全面禁用，這段只作保險）
                    if _is_quick_stop(p):
                        w = {'min': 10, 'base': 20, 'max': 30}

                    weight = (p.get('reviews') or p.get('user_ratings_total') or 0)
                    weight = max(1.0, float(weight))
                    if p.get('rating'):
                        weight *= (0.8 + 0.2 * float(p['rating']) / 5.0)

                    if _is_quick_stop(p):
                        weight = max(1.0, weight * 0.25)

                    windows.append({'min': w['min'], 'base': w['base'], 'max': w['max'], 'weight': weight})
                    print(f"[StayTime] {p.get('name')} | {w} | weight={weight}")

                final_stays = balance_durations_in_slot(windows, slot_minutes, travel_buffer)
                print(f"[StayTime] slot={label} {win[0]}-{win[1]} total={slot_minutes}m buffer={travel_buffer}m -> stays={final_stays}")

                for p, m in zip(selected_places, final_stays):
                    p['stay_minutes'] = int(m)
            except Exception as _e:
                print(f"[StayTime] 配平失敗：{_e}")
                pass

            # ---------- linked list ----------
            node = ItineraryNode(
                day=d_no,
                slot_name=label,
                start_time=win[0],
                end_time=win[1],
                places=selected_places,
            )
            if d_idx == 0 and len(day["slots"]) > 0 and label == day["slots"][0]["label"]:
                linked_head = node
            else:
                if "linked_prev" in locals() and linked_prev is not None:
                    linked_prev.next = node
            linked_prev = node

            # ---------- 逐段移動（上一個 → 這一個）的分鐘數與導航連結 ----------
            try:
                for i in range(len(selected_places) - 1):
                    a = selected_places[i]
                    b = selected_places[i + 1]
                    a_loc = {"lat": a.get("lat"), "lng": a.get("lng")}
                    b_loc = {"lat": b.get("lat"), "lng": b.get("lng")}

                    try:
                        leg_min = travel_time_cached(a_loc, b_loc, cfg["mode"])
                    except Exception:
                        leg_min = _approx_travel_minutes(a_loc, b_loc, cfg["mode"])

                    # 把「上一個 → 這一個」的資訊寫在『目的地 b』身上
                    b["_from_prev_leg_min"] = int(leg_min)
                    b["_from_prev_nav_url"] = _directions_url(a_loc, b_loc, cfg["mode"])
            except Exception as _e:
                print(f"[Legs] 計算段間移動失敗：{_e}")

            # --- 跨時段：上一段最後一點 → 本段第一點（每天第一段不顯示） ---
            try:
                if selected_places and si > 0:
                    first_ll = {"lat": selected_places[0]["lat"], "lng": selected_places[0]["lng"]}
                    origin_ll = prev_slot_last_ll  # 只用上一段最後一點

                    if origin_ll and origin_ll.get("lat") is not None and origin_ll.get("lng") is not None:
                        try:
                            cross_min = travel_time_cached(origin_ll, first_ll, cfg["mode"])
                        except Exception:
                            cross_min = _approx_travel_minutes(origin_ll, first_ll, cfg["mode"])
                        selected_places[0]["_from_prev_leg_min"] = int(cross_min)
                        selected_places[0]["_from_prev_nav_url"] = _directions_url(origin_ll, first_ll, cfg["mode"])
            except Exception as _e:
                print(f"[Legs] 跨時段移動失敗：{_e}")

            # ---------- 累積 JSON 結構 ----------
            day_struct["slots"].append({"label": label, "window": [win[0], win[1]], "places": selected_places})

            # ---------- Markdown 渲染 ----------
            for i, p in enumerate(selected_places):
                icon = (
                    "🍽️" if p.get("category") == "meal"
                    else ("🛍️" if p.get("category") == "stroll" else "🏠")
                )
                slot_md += f"- {icon} **{p['name']}**（預估停留 {p['stay_minutes']} 分鐘）\n"
                slot_md += f"  - ⭐ 評分：{p.get('rating','-')}\n"
                slot_md += f"  - 📍 地址：{p.get('address','-')}\n"
                slot_md += f"  - 🕒 營業時間：{p.get('open_text','未提供')}\n"
                slot_md += f"  - 🔗 [地圖連結]({p.get('map_url','')})\n"

                # 從上一個景點 → 此景點（若沒有 _from_prev_* 就不顯示；A 不會有、B~ 會有）
                if p.get("_from_prev_leg_min") is not None:
                    mode_zh = _mode_zh(cfg["mode"])
                    mins = int(p["_from_prev_leg_min"])
                    nav = p.get("_from_prev_nav_url") or ""
                    if nav:
                        slot_md += f"  - 🚕 從上一點移動：約 {mins} 分鐘（{mode_zh}）｜[Google 導航]({nav})\n"
                    else:
                        slot_md += f"  - 🚕 從上一點移動：約 {mins} 分鐘（{mode_zh}）\n"

            if selected_places:
                prev_slot_last_ll = {"lat": selected_places[-1]["lat"], "lng": selected_places[-1]["lng"]}

            md += slot_md + "\n"

        daily_md[f"Day{d_no}"] = md
        daily_struct.append(day_struct)

    # 保存到 state
    state["daily_slots"] = daily_md
    state["itinerary_struct"] = {
        "locations_text": locations_text,
        "locations": locations_arr,
        "per_day_city": state.get("per_day_city", {}),
        "start_date": start_date,
        "days": daily_struct,
    }
    state["itinerary_json"] = state["itinerary_struct"]
    state["used_places"] = list(used_place_names)
    state["linked_itinerary"] = linked_to_list(linked_head)
    state["head"] = linked_head
    state["days"] = len(days_slots)
    state["date"] = start_date
    state["summary"] = state["profile"].get("偏好分析", "")
    return state



# =========================================
# Node 3.5：LLM 多模型驗證
# =========================================
def validate_plan_with_llms(state: Dict[str, Any]) -> Dict[str, Any]:
    plan_json_str = json_dumps_safe(state.get("itinerary_struct", {}))
    result = call_multi_checkers(plan_json_str, summary=state.get("summary", ""))
    state["plan_validation"] = result
    return state

def json_dumps_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


# =========================================
# Node 4：產出 markdown 與 HTML 行程
# =========================================
def assemble_markdown(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = "\n\n".join(state.get("daily_slots", {}).values())
    state["itinerary_raw"] = raw
    state["itinerary_html"] = markdown2.markdown(raw)
    state["markdown"] = raw
    state["html"] = state["itinerary_html"]
    return state


# =========================================
# Node 5：回傳行程資訊
# =========================================
def return_plan(state: Dict[str, Any]) -> Dict[str, Any]:
    ij = state.get("itinerary_json", {}) or {}
    return {
        "used_places": state.get("used_places", []),
        "locations": ij.get("locations", []),           # ✅ 回傳陣列
        "locations_text": ij.get("locations_text", ""), # ✅ 若前端要顯示字串
        "days": state.get("days", 1),
        "summary": state.get("summary", ""),
        "markdown": state.get("markdown", ""),
        "html": state.get("html", ""),
        "itinerary_json": ij,
        "linked_list_head": state.get("head"),
        "plan_validation": state.get("plan_validation"),
        "behavior_profile": state.get("behavior_profile", {}),
    }