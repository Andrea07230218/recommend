# 檔案： core/formatters.py
# (已修正：移除最後一行的 'if v is not None' 過濾器)

from typing import List, Dict, Any, Optional
from collections import defaultdict
from datetime import datetime, timezone
import re

# --- 來自 recommend.py (B) Utilities ---
def pick_itinerary_title(doc: Dict) -> str | None:
    """從 MongoDB 文件中找出最適合的標題"""
    candidates = ["title", "name", "itinerary_name", "plan_name", "trip_title", "trip_name"]
    for k in candidates:
        if v := doc.get(k):
            if isinstance(v, str) and v.strip(): return v.strip()
    if meta := doc.get("meta"):
        if isinstance(meta, dict):
            for k in candidates:
                if v := meta.get(k):
                    if isinstance(v, str) and v.strip(): return v.strip()
    return None

def parse_created_at(doc):
    """安全地解析 created_at 欄位為 ISO 格式字串"""
    ca = doc.get("created_at")
    if isinstance(ca, datetime):
        if ca.tzinfo is None:
            return ca.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return ca.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(ca, dict) and "$date" in ca:
        v = ca["$date"]
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v/1000.0, tz=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        if isinstance(v, str):
            return v
    if isinstance(ca, str):
        return ca
    return None

# --- 【核心】格式化函式 (來自 recommend.py) ---
def format_trip_for_kotlin(doc: Dict[str, Any]) -> Dict[str, Any]:
    """將從 structured_itineraries 讀取的 doc 轉換成 Kotlin Trip data class 預期的格式"""
    
    if not doc:
        print("⚠️ 警告：format_trip_for_kotlin 收到了 None")
        return {}
        
    trip_id = str(doc.get("_id", ""))
    created_by = doc.get("user_id", "")
    name = pick_itinerary_title(doc) or "未命名行程"
    locations_list = doc.get("locations", [])
    locations_str = "、".join(locations_list) if isinstance(locations_list, list) else ""
    total_budget = doc.get("total_budget") # 👈 讀取 null
    start_date = doc.get("start_date")
    end_date = doc.get("end_date")
    activity_start = doc.get("activity_start")
    activity_end = doc.get("activity_end")
    avg_age_raw = doc.get("avg_age")
    avg_age = avg_age_raw if avg_age_raw is not None else "IGNORE"
    transportation = doc.get("transportation", "public")
    transport_prefs = [transportation] if isinstance(transportation, str) else []
    use_gmaps_rating_raw = doc.get("use_gmaps_rating")
    use_gmaps_rating = use_gmaps_rating_raw if use_gmaps_rating_raw is not None else True
    styles_raw = doc.get("preferences")
    styles = styles_raw if isinstance(styles_raw, list) else [] # 👈 讀取 null 變為 []
    visibility_raw = doc.get("visibility")
    visibility = visibility_raw if visibility_raw in ["PUBLIC", "PRIVATE"] else "PRIVATE"
    members = [] # 暫時為空
    
    kotlin_days = []
    nodes = doc.get("nodes", [])
    grouped_nodes: Dict[int, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
    
    if nodes:
        for node in nodes: 
            if not node:
                continue
            
            day_num = node.get("day")
            slot_label = node.get("slot")
            
            places_list = node.get("places", [])
            if not places_list or not isinstance(day_num, int) or not slot_label:
                continue
                
            for place in places_list: 
                if not place or not isinstance(place, dict):
                    continue
                
                activity = {
                    "place_id": place.get("place_id") or place.get("id"),
                    "name": place.get("place_name") or place.get("name"),
                    "category": place.get("category"),
                    "stay_minutes": place.get("stay_minutes"),
                    "rating": place.get("rating"),
                    "reviews": place.get("reviews"),
                    "address": place.get("address"),
                    "map_url": place.get("map_url"),
                    "open_text": place.get("open_text"),
                    "types": place.get("types", []),
                    "lat": place.get("lat"),
                    "lng": place.get("lng"),
                    "from_prev_leg_min": node.get("_from_prev_leg_min")
                }
                activity = {k: v for k, v in activity.items() if v is not None}
                
                if "lat" in activity and "lng" in activity and "place_id" in activity:
                    grouped_nodes[day_num][slot_label].append(activity)

    db_days = doc.get("days", [])
    day_info_map = {d.get("day"): {"date": d.get("date"), "city": d.get("city")} for d in db_days if d and isinstance(d.get("day"), int)}
    
    for day_num, slots_dict in sorted(grouped_nodes.items()):
        kotlin_slots = []
        for slot_label, activities in slots_dict.items():
            window = ["00:00", "23:59"] 
            first_node_in_slot = next((n for n in nodes if n and n.get("day") == day_num and n.get("slot") == slot_label), None)
            if first_node_in_slot:
                start = first_node_in_slot.get("start")
                end = first_node_in_slot.get("end")
                if start and end:
                    window = [start, end]
            kotlin_slots.append({"label": slot_label, "window": window, "places": activities})

        day_info = day_info_map.get(day_num, {})
        kotlin_days.append({
            "date": day_info.get("date", start_date or "未知日期"),
            "city": day_info.get("city"),
            "slots": kotlin_slots
        })

    kotlin_trip = {
        "id": trip_id, "createdBy": created_by, "name": name, "locations": locations_str,
        "totalBudget": total_budget,       # 👈 這裡會是 None
        "startDate": start_date,
        "endDate": end_date,
        "activityStart": activity_start,
        "activityEnd": activity_end,
        "avgAge": avg_age,
        "transportPreferences": transport_prefs, # 👈 這裡會是 []
        "useGmapsRating": use_gmaps_rating,
        "styles": styles,                  # 👈 這裡會是 []
        "visibility": visibility,
        "members": members, 
        "days": kotlin_days
    }
    
    # ✅ 【關鍵修正】
    # 直接回傳完整的字典，包含 None (null) 值
    return kotlin_trip