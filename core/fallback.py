# core/fallback.py
from __future__ import annotations
from typing import Optional, Dict, Any, List

from core.google_maps import search_place, get_place_details, is_open_during_slot
from core.place_filters import BAN_QUICK_STOPS, is_quick_stop

# 依照時段準備一些中文關鍵字，盡量挑到能吃或能坐的店
# ※ 已移除「手搖飲」關鍵字，避免命中被禁用類型
_SLOT_KEYWORDS = {
    "早餐":   ["早餐", "早午餐", "豆漿", "美而美", "早點"],
    "上午":   ["咖啡", "甜點", "小吃", "早午餐"],
    "中午":   ["午餐", "餐廳", "小吃", "便當", "熱炒"],
    "下午":   ["咖啡", "下午茶", "甜點", "冰品"],
    "下午茶": ["下午茶", "咖啡", "甜點", "茶館"],
    "晚餐前": ["咖啡", "甜點", "小吃"],  # ← 移除「手搖飲」
    "晚上":   ["晚餐", "餐廳", "夜市", "熱炒", "居酒屋"],
}

def _first_non_empty(*vals) -> str:
    for v in vals:
        if v:
            s = str(v).strip()
            if s:
                return s
    return ""

def _shape_detail_for_fallback(det: Dict[str, Any]) -> Dict[str, Any]:
    """
    轉成 generate_daily_slots 期望的 fallback 結構。
    需要 keys:
      - place_id, name, rating, reviews, address, types, geometry.location
    """
    geom = (det.get("geometry") or {})
    loc = geom.get("location") or {}
    return {
        "place_id": det.get("place_id"),
        "name": det.get("name"),
        "rating": float(det.get("rating") or 0.0),
        "reviews": int(det.get("user_ratings_total") or 0),
        "address": _first_non_empty(det.get("formatted_address")),
        "types": det.get("types", []) or [],
        "geometry": {"location": {"lat": loc.get("lat"), "lng": loc.get("lng")}},
    }

def _quality_ok(slot_label: str, rating: float, reviews: int) -> bool:
    """保底也做基本品質把關（與主流程門檻一致概念，略微寬鬆）。"""
    if slot_label == "晚上":
        return rating >= 4.1 and reviews >= 100
    elif slot_label == "中午":
        return rating >= 3.9 and reviews >= 50
    elif slot_label == "早餐":
        return rating >= 3.8 and reviews >= 25
    else:  # 下午 / 上午 / 下午茶 / 晚餐前
        return rating >= 3.9 and reviews >= 35

def fallback_place_from_backup(
    *,
    city: str,
    slot_label: str,
    slot_window: Optional[List[str]] = None,  # e.g. ["12:00","14:00"]
    date: Optional[str] = None,
    used_place_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """
    當主流程找不到餐廳/店家時，以簡單關鍵字在該城市抓一個可用的備援地點。
    - 與 core.google_maps.search_place 的介面保持一致： search_place(name, city)
    - 回傳結構需可被 generate_daily_slots 直接使用（見 _shape_detail_for_fallback）
    """
    used_place_ids = used_place_ids or set()
    city = (city or "").strip()
    if not city:
        return None

    keywords = _SLOT_KEYWORDS.get(slot_label, []) or ["餐廳", "咖啡"]

    for kw in keywords:
        try:
            # ✅ 介面對齊：不要用 query=，而是 (name_keyword, city)
            gplace = search_place(kw, city)
            if not gplace:
                continue

            pid = gplace.get("place_id")
            if not pid or pid in used_place_ids:
                continue

            det = get_place_details(pid) or {}
            if not det:
                continue

            # 嚴格避開連鎖手搖/速食/超商
            if BAN_QUICK_STOPS and is_quick_stop(det.get("name", ""), det.get("types", [])):
                continue

            # 營業時段基本檢查（若有提供 opening_hours）
            if slot_window and date:
                try:
                    ok = is_open_during_slot(
                        det.get("opening_hours"),
                        (slot_window[0], slot_window[1]),
                        date_str=date,
                        require_full_cover=False,
                    )
                    if not ok:
                        continue
                except Exception:
                    # 沒有營業資訊就寬鬆放行
                    pass

            shaped = _shape_detail_for_fallback(det)

            # 基本完整性：要有名字＆座標＆地址
            if not shaped.get("name"):
                continue
            loc = ((shaped.get("geometry") or {}).get("location") or {})
            if loc.get("lat") is None or loc.get("lng") is None:
                continue
            if not shaped.get("address"):
                continue

            # 品質把關
            if not _quality_ok(slot_label, shaped.get("rating", 0.0), shaped.get("reviews", 0)):
                continue

            return shaped

        except Exception:
            # 單一關鍵字失敗就換下一個，不讓整段炸掉
            continue

    return None
