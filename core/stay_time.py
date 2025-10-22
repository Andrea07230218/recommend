# core/stay_time.py
from __future__ import annotations
from typing import Dict, List, Any, Tuple
import math
from datetime import datetime
from core.place_filters import is_quick_stop

def _minutes_between(hhmm_start: str, hhmm_end: str) -> int:
    s = datetime.strptime(hhmm_start, "%H:%M")
    e = datetime.strptime(hhmm_end, "%H:%M")
    return int((e - s).total_seconds() // 60)

# ====== 觀光類型的基準時窗（分鐘）: (min, max) ======
BASE_BY_TYPE: Dict[str, Tuple[int, int]] = {
    "museum": (60, 150),
    "art_gallery": (45, 120),
    "park": (30, 150),
    "zoo": (90, 180),
    "shopping_mall": (45, 120),
    "temple": (40, 90),
    "church": (40, 90),
    "viewpoint": (30, 90),
    "beach": (60, 180),
    "aquarium": (60, 150),
    "amusement_park": (90, 210),
    "historical_landmark": (60, 150),
}

# ====== 餐飲類型的基準時窗（分鐘）: (min, max) ======
# 以「中文時段」為主，並提供英文別名對應（見 _norm_slot）
FOOD_SLOT_WINDOW_ZH: Dict[str, Tuple[int, int]] = {
    "早餐": (30, 60),
    "中午": (45, 90),
    "下午": (30, 60),
    "晚上": (75, 120),  # 晚餐上調，強化用餐體驗
}

# 英文/常見別名 → 中文時段的映射
_SLOT_ALIASES_TO_ZH: Dict[str, str] = {
    # 英文
    "breakfast": "早餐",
    "morning": "上午",
    "noon": "中午",
    "lunch": "中午",
    "afternoon": "下午",
    "evening": "晚上",
    "dinner": "晚上",
    # 中文容錯（小寫後比對）
    "早餐": "早餐",
    "上午": "上午",
    "中午": "中午",
    "下午": "下午",
    "晚上": "晚上",
}

PACE_MULT = {
    "fast": 0.9,
    "normal": 1.0,
    "slow": 1.15,
}

def _norm_slot(slot_name: str | None) -> str:
    """
    將 slot 名稱標準化為中文（早餐/上午/中午/下午/晚上）。
    不認得時，回傳空字串。
    """
    if not slot_name:
        return ""
    key = str(slot_name).strip().lower()
    # 英文優先
    if key in _SLOT_ALIASES_TO_ZH:
        return _SLOT_ALIASES_TO_ZH[key]
    # 原字串可能就是中文，但被 lower() 過；再給一次機會
    raw = str(slot_name).strip()
    return _SLOT_ALIASES_TO_ZH.get(raw, "")

def _type_bucket(types: List[str]) -> str | None:
    if not types:
        return None
    tset = set(types)
    # 精準命中
    for key in BASE_BY_TYPE.keys():
        if key in tset:
            return key
    # 寬鬆回退
    if "park" in tset:
        return "park"
    if "tourist_attraction" in tset:
        return "viewpoint"
    return None

def _popularity_weight(rating: float | None, reviews: int | None) -> float:
    r = float(rating or 0.0)
    n = max(0, int(reviews or 0))
    # 熱度：log(評論數) * （評分0.8~1.0權重）
    return math.log1p(n) * (0.8 + 0.2 * (min(max(r, 0), 5) / 5.0))

def estimate_stay_window(place: Dict[str, Any],
                         slot_ctx: Dict[str, Any],
                         user_ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    回傳 {min, base, max, reason}
    - 餐飲：依中文時段（或英文別名）決定區間；晚餐上調到 75–120。
    - 快停靠（連鎖手搖/速食/超商）：強制 10–30（雙保險）。
    - 觀光：依類型表給範圍，並用評價/步調微調 base。
    """
    types = place.get("types") or []
    rating = float(place.get("rating") or 0.0)
    reviews = int(place.get("user_ratings_total") or place.get("reviews") or 0)
    price_level = place.get("price_level")
    slot_zh = _norm_slot(slot_ctx.get("slot_name"))
    pace = user_ctx.get("pace", "normal")

    # 🔒 快停靠：就算前面已禁用，仍當作安全閥
    if is_quick_stop(place.get("name", ""), types):
        return {"min": 10, "base": 20, "max": 30,
                "reason": "quick_stop"}

    # ====== 基準時窗 ======
    if place.get("category") == "meal":
        # 餐段：依中文時段使用食物窗口；不識別時落中間 (45,90)
        min_b, max_b = FOOD_SLOT_WINDOW_ZH.get(slot_zh, (45, 90))
        # 晚餐明確強化
        if slot_zh == "晚上":
            min_b, max_b = FOOD_SLOT_WINDOW_ZH["晚上"]
    else:
        bucket = _type_bucket(types) or "viewpoint"
        min_b, max_b = BASE_BY_TYPE.get(bucket, (45, 120))

    # 基準 base = 區間中位
    base = (min_b + max_b) / 2.0

    # 熱門度調整
    pop = _popularity_weight(rating, reviews)      # ~ 0..6+
    pop_norm = min(pop / 6.0, 1.0)                # 0..1
    base = base * (0.9 + 0.2 * pop_norm)          # 0.9x ~ 1.1x

    # 餐廳的價位與等位時間
    if place.get("category") == "meal":
        if isinstance(price_level, int) and price_level >= 3:
            base += 10
        if reviews >= 1500:
            base += 10

    # 使用者步調
    base *= PACE_MULT.get(pace, 1.0)

    # 裁切到 min/max
    base = max(min_b, min(int(round(base)), max_b))

    return {
        "min": min_b,
        "base": int(base),
        "max": max_b,
        "reason": f"slot={slot_zh or '-'} types={types[:3]} rating={rating} reviews={reviews} pace={pace}"
    }

def balance_durations_in_slot(items: List[Dict[str, Any]],
                              slot_minutes: int,
                              travel_buffer: int = 0) -> List[int]:
    """
    依「可用時間 = slot_minutes - travel_buffer」配平 items 的時長（分鐘）。
    items 需含 {min, base, max, weight}；本函式只調整數值，不改順序。
    - 若總和 > 可用：按 1/weight 比例縮（熱門縮得少）。
    - 若總和 < 可用：按 weight 比例加（熱門加得多）。
    """
    avail = max(0, int(slot_minutes) - int(travel_buffer))
    n = len(items)
    if n == 0:
        return []

    # 先用 base 做起點（但仍要夾在 min/max 之間）
    mins = [max(it["min"], min(int(it["base"]), it["max"])) for it in items]
    total = sum(mins)
    if total == avail:
        return mins

    weights = [max(0.1, float(it.get("weight", 1.0))) for it in items]  # 防 0
    if total > avail and total > 0:
        # 需要縮減：熱門者縮得較少 -> 按 1/weight 縮
        over = total - avail
        invw = [1.0 / w for w in weights]
        inv_sum = sum(invw) or 1.0
        for i in range(n):
            cut = over * (invw[i] / inv_sum)
            mins[i] = max(items[i]["min"], int(round(mins[i] - cut)))
        # 二次微調避免仍超額
        total2 = sum(mins)
        if total2 > avail:
            diff = total2 - avail
            while diff > 0:
                changed = False
                for i in range(n):
                    if mins[i] > items[i]["min"]:
                        mins[i] -= 1
                        diff -= 1
                        changed = True
                        if diff == 0:
                            break
                if not changed:
                    break
    elif total < avail:
        # 需要增加：熱門者加得較多 -> 按 weight 加
        under = avail - total
        wsum = sum(weights) or 1.0
        for i in range(n):
            add = under * (weights[i] / wsum)
            mins[i] = min(items[i]["max"], int(round(mins[i] + add)))
        # 二次微調避免不足
        total2 = sum(mins)
        if total2 < avail:
            diff = avail - total2
            while diff > 0:
                changed = False
                for i in range(n):
                    if mins[i] < items[i]["max"]:
                        mins[i] += 1
                        diff -= 1
                        changed = True
                        if diff == 0:
                            break
                if not changed:
                    break
    return mins
