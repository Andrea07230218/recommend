# routes/recommend.py
from datetime import datetime
from datetime import timezone
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from markdown2 import markdown as md
from bson import SON, ObjectId
import traceback
import logging
import os
import httpx

# --- 你的其他 imports (來自原版) ---
from models.schemas import RecommendRequest, RecommendGroupRequest, ReplaceActivityRequest, Alternative
from core.mongo import (
    users_collection,
    get_user,
    save_form,
    form_collection,
)
from core import mongo
from core.langgraph_nodes import (
    extract_profile,
    analyze_preferences,
    generate_daily_slots,
    validate_plan_with_llms,
    assemble_markdown,
    return_plan,
)

# --- 【新增】(來自合併的程式碼) ---
from typing import List, Dict, Any, Optional
import re, json, math
from collections import Counter, defaultdict
from pydantic import BaseModel, Field
from fastapi import Query
# --- 【結束新增 imports】 ---


router = APIRouter()

# --- 【新增】Google Maps API Key ---
#GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", None)
PLACES_API_ENDPOINT = "https://places.googleapis.com/v1/places"


# --- 【新增】(來自合併的程式碼 - 資料庫 collections) ---
try:
    db = mongo.db
    itins_col = db["structured_itineraries"]
    favorites_col = db["user_favorite"]
    pageviews_col = db["user_browse"]
except Exception as e:
    print(f"⚠️ 警告：無法載入 '通用推薦' 所需的 MongoDB collections: {e}")
    itins_col = None
    favorites_col = None
    pageviews_col = None
# --- 【結束新增 collections】 ---


# --- 你的常數和 helper functions (來自原版 - 保持不變) ---
GROUP_FORM_FIELD_ORDER = ["leader_id", "members", "trip_name", "date", "days", "locations", "time_range", "preferences", "exclude", "notes"]
DEFAULTS = {"leader_id": "", "members": [], "trip_name": "", "date": "", "days": 0, "locations": [], "time_range": "", "preferences": [], "exclude": [], "notes": ""}
_BANNED_TERMS = ["手搖飲", "手搖", "飲料店", "茶飲", "珍珠奶茶", "珍奶", "連鎖速食", "速食", "超商", "便利商店", "7-11", "全家", "星巴克", "可不可", "清心", "CoCo", "50嵐", "迷客夏", "麥當勞", "肯德基"]


def _merge_exclude(user_exclude: list[str] | None) -> list[str]:
    base = set(user_exclude or []) | set(_BANNED_TERMS)
    return sorted(list(base))

def to_locations_list(val):
    if not val: return []
    if isinstance(val, list): return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str): return [p.strip() for p in re.split(r"[,、，\s]+", val) if p.strip()]
    return []

def _clean_str_list(value):
    if value is None: return []
    if isinstance(value, str): parts = [p.strip() for p in re.split(r"[,\u3001，/|\s]+", value)]
    elif isinstance(value, list): parts = [str(p).strip() for p in value]
    else: return []
    return [p for p in parts if p]

def _canonize_form_order(d: dict) -> SON:
    ordered = [(k, d.get(k, DEFAULTS.get(k))) for k in GROUP_FORM_FIELD_ORDER]
    return SON(ordered)

def _shape_result_for_frontend(result: dict, trip_name: str = "", trip_id: str | None = None) -> dict:
    itinerary_html = result.get("html") or result.get("itinerary_html") or ""
    itinerary_md = result.get("markdown") or result.get("itinerary_raw") or ""
    if not itinerary_html and itinerary_md: itinerary_html = md(itinerary_md)
    places = result.get("used_places") or result.get("places") or []
    locations = result.get("itinerary_json", {}).get("locations")
    if not isinstance(locations, list): locations = []
    shaped = {"trip_id": trip_id or "", "trip_name": (trip_name or result.get("trip_name") or "").strip(), "html": itinerary_html, "markdown": itinerary_md, "summary": result.get("summary", ""), "days": result.get("days", 1), "used_places": places, "locations_text": "、".join(locations), "error": False, "error_message": ""}
    return shaped


def _shape_personal_form_to_group_format(raw_form: dict, user_id: str) -> dict:
     return {"leader_id": user_id, "members": [{"user_id": user_id}], "trip_name": raw_form.get("trip_name") or raw_form.get("name"), "date": raw_form.get("start_date") or raw_form.get("date"), "days": raw_form.get("days"), "locations": raw_form.get("locations"), "time_range": raw_form.get("activity_start") or raw_form.get("time_range"), "preferences": _clean_str_list(raw_form.get("preferences") or raw_form.get("styles")), "exclude": _clean_str_list(raw_form.get("exclude")), "notes": raw_form.get("notes") or raw_form.get("extraNote")}

def _run_planner(user_id: str, form_payload: dict) -> dict:
    state = {"user_id": user_id, "form": form_payload}; state = extract_profile(state); state = analyze_preferences(state); state = generate_daily_slots(state); state = validate_plan_with_llms(state); state = assemble_markdown(state); return return_plan(state)


def _persist_structured_itinerary(
    *, user_id: str, form_id: str | None, result: dict, title: str = "",
    fallback_locations: list[str] | None = None,
) -> str:
    db = mongo.db;
    if itins_col is None: raise ValueError("structured_itineraries collection is not available.")
    itin_json = result.get("itinerary_json") or {}; days_list = itin_json.get("days") or []; days_for_db, all_nodes = [], []
    for i, d in enumerate(days_list, start=1):
        slots = d.get("slots", []); slot_nodes = []
        for s in slots: slot_nodes.append({"day": i, "slot": s.get("label"), "start": s.get("window", [None, None])[0], "end": s.get("window", [None, None])[1], "places": s.get("places", [])})
        head_id = None
        if slot_nodes:
            try: from core.itinerary_linked_list import build_linked_list, flatten_linked; head = build_linked_list(slot_nodes); flat = flatten_linked(head); all_nodes.extend(flat.get("nodes", [])); head_id = flat.get("head_id")
            except (ImportError, TypeError): pass
        days_for_db.append({"day": i, "date": d.get("date"), "city": d.get("city"), "head_id": head_id})
    locations = to_locations_list(itin_json.get("locations") or fallback_locations); form_data = result.get("form", {})
    doc = {
        "user_id": user_id, "form_id": form_id, "created_at": datetime.utcnow(),
        "title": (title or result.get("trip_name") or form_data.get("trip_name") or "未命名行程").strip(),
        "locations": locations,
        "start_date": itin_json.get("start_date") or form_data.get("start_date"),
        "end_date": form_data.get("end_date"),
        "activity_start": form_data.get("activity_start"),
        "activity_end": form_data.get("activity_end"),
        "avg_age": form_data.get("avg_age"),
        "transportation": form_data.get("transportation"),
        "use_gmaps_rating": form_data.get("use_gmaps_rating"),
        "preferences": form_data.get("preferences"),
        "visibility": form_data.get("visibility"),
        "total_budget": form_data.get("total_budget"),
        "days": days_for_db,
        "nodes": all_nodes,
        "summary": result.get("summary", ""), "html": result.get("html", ""),
        "used_places": result.get("used_places", [])
    }
    insert_result = itins_col.insert_one(doc); return str(insert_result.inserted_id)

def _decide_travel_mode(transportation: str | None, transport_text: str | None) -> str:
    t = (transportation or "").strip().lower()
    if t in {"drive", "driving", "car", "汽車"}:
        return "driving"
    if t in {"public", "transit", "bus", "metro", "train", "大眾運輸"}:
        return "transit"
    return "walking"

def _planner_flags() -> dict:
    return {"ban_quick_stops": True, "grid_diversity": True, "dinner_min_reviews": 120, "dinner_min_rating": 4.2}

def parse_created_at(doc):
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


# --- 你的 recommend_trip (來自原版 - 完整保留) ---
@router.post("/", summary="產生推薦行程", description="根據使用者問卷與收藏紀錄，自動產生每日行程")
async def recommend_trip(req: RecommendRequest, request: Request):
    print("\n--- recommend_trip endpoint called ---")
    raw_body = None
    try:
        raw_body = await request.json()
        print(f"--- Raw JSON received: {raw_body}")
    except Exception as json_error:
        print(f"--- FAILED TO PARSE JSON BODY: {json_error} ---")
        raise HTTPException(status_code=400, detail=f"Invalid JSON format: {json_error}")

    print("--- Attempting Pydantic validation and main logic ---")
    try:
        user = users_collection.find_one({"username": req.user_id})
        if not user:
            print(f"--- User not found: {req.user_id} ---")
            raise HTTPException(status_code=404, detail=f"找不到使用者 {req.user_id}")

        form_data = req.form.copy() if req.form else {}

        if favorites := user.get("favorites"):
            form_data.setdefault("偏好", []).append({"source": "收藏", "類型": favorites})

        locations_arr = to_locations_list(form_data.get("locations"))
        if locations_arr:
            form_data["locations"] = locations_arr

        banned_exclude = _merge_exclude(_clean_str_list(form_data.get("exclude")))
        form_data["避開條件"] = banned_exclude

        created_at = datetime.utcnow()
        group_like_form = _shape_personal_form_to_group_format(form_data, req.user_id)

        raw_transportation = form_data.get("transportation")
        group_like_form.update({
            "locations": locations_arr,
            "exclude": banned_exclude,
            "transportation": raw_transportation or None
        })

        save_form(req.user_id, _canonize_form_order(group_like_form), form_type="personal", created_at=created_at)

        form_doc = form_collection.find_one({"user_id": req.user_id, "created_at": created_at, "form_type": "personal"})
        form_id = str(form_doc["_id"]) if form_doc else None

        travel_mode = _decide_travel_mode(raw_transportation, None)

        form_payload = {
            **form_data,
            "form_type": "personal", "created_at": created_at, "trip_preference_id": form_id,
            "planner": _planner_flags(),
            "travel": {"mode": travel_mode, "max_leg_minutes": 20, "search_radius_m": 1200}
        }
        form_payload["旅遊日期"] = form_data.get("start_date")
        form_payload["旅遊天數"] = form_data.get("days")

        print("--- Calling _run_planner ---")
        result = _run_planner(user_id=req.user_id, form_payload=form_payload)
        print("--- _run_planner finished ---")

        trip_id = _persist_structured_itinerary(
            user_id=req.user_id, form_id=form_id, result=result,
            title=(form_data.get("trip_name") or "未命名行程"),
            fallback_locations=locations_arr
        )

        print("--- Reached end of recommend_trip try block, building response ---")

        itin_json = result.get("itinerary_json") or {}
        days_list = itin_json.get("days") or []
        locations_str = "、".join(form_data.get("locations", []))

        android_trip_response = {
            "id": trip_id,
            "createdBy": req.user_id,
            "name": form_data.get("trip_name", "未命名行程"),
            "locations": locations_str,
            "totalBudget": form_data.get("total_budget"),
            "startDate": form_data.get("start_date"),
            "endDate": form_data.get("end_date"),
            "activityStart": form_data.get("activity_start"),
            "activityEnd": form_data.get("activity_end"),
            "avgAge": form_data.get("avg_age", "IGNORE"),
            "transportPreferences": [form_data.get("transportation", "public")],
            "useGmapsRating": form_data.get("use_gmaps_rating", True),
            "styles": form_data.get("preferences", []),
            "visibility": form_data.get("visibility", "PRIVATE"),
            "members": [],
            "days": days_list
        }

        print(f"--- Returning JSON (first 200 chars): {str(android_trip_response)[:200]}...")
        return android_trip_response

    except RequestValidationError as exc:
        print(f"--- Caught RequestValidationError in recommend_trip ---")
        print(f"❌ 422 Validation Error: {exc.errors()}")
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    except HTTPException as http_exc:
        print(f"--- Caught HTTPException in recommend_trip: {http_exc.status_code} ---")
        raise
    except Exception as e:
        print(f"--- Caught generic Exception in recommend_trip ---")
        logging.getLogger("uvicorn.error").exception("Personal recommend failed")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"生成行程時發生錯誤：{str(e)}")


# ======================================================================
#            ↓↓↓ 【合併過來的「通用推薦」API 及輔助函式】 ↓↓↓
# ======================================================================

# --- (B) Utilities (norm_city, split_names, pick_itinerary_title) ---
def norm_city(c: str | None) -> str | None:
    if not c: return None; c = c.strip().replace("台", "臺"); c = re.sub(r"(市|縣)$", "", c); return c

SPLIT_RE = re.compile(r"[，,、/]+")
def split_names(s: str | None) -> list[str]:
    if not isinstance(s, str) or not s.strip() or s == "(空行程)":
        return []
    try:
        return [p.strip() for p in SPLIT_RE.split(s) if p.strip()]
    except Exception:
        return []

def pick_itinerary_title(doc: Dict) -> str | None:
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

# --- (B) Features from itinerary ---
def extract_itinerary_features(itin_doc: Dict) -> Dict:
    names = []
    try:
        if nodes := itin_doc.get("nodes"):
            for node in nodes:
                if isinstance(node, dict):
                    node_name = node.get("place_name") or node.get("name")
                    names.extend(split_names(node_name))
        elif days := itin_doc.get("days"):
            for day in days:
                if isinstance(day, dict):
                    if attractions := day.get("attractions"):
                         if isinstance(attractions, list):
                            for att in attractions:
                                if isinstance(att, dict):
                                    name = att.get("name")
                                    names.extend(split_names(name))
                    elif slots := day.get("slots"):
                        if isinstance(slots, list):
                            for slot in slots:
                                if isinstance(slot, dict):
                                    places = slot.get("places", [])
                                    if isinstance(places, list):
                                        for place in places:
                                            if isinstance(place, dict):
                                                name = place.get("name")
                                                names.extend(split_names(name))
    except Exception as e:
        print(f"⚠️ Error extracting names in extract_itinerary_features (ID: {itin_doc.get('_id')}): {e}")
        traceback.print_exc()

    names = list(dict.fromkeys(n for n in names if n))
    cities = set()
    for n in names:
        for city_kw in ["臺南", "高雄", "宜蘭", "花蓮", "臺東", "澎湖"]:
            if city_kw in n: cities.add(city_kw)
    form_id = itin_doc.get("trip_preference_id")
    if form_id:
        try:
            form = form_collection.find_one({"_id": ObjectId(str(form_id))}) or {}
            if loc := form.get("form", {}).get("location"):
                if c := norm_city(loc): cities.add(c)
        except Exception: pass
    official_title = pick_itinerary_title(itin_doc)
    fallback_title = " / ".join(names[:2]) if names else "客製行程"
    title = official_title or fallback_title
    return {
        "id": str(itin_doc.get("_id")), "itinerary_name": title, "names": names,
        "cities": sorted(cities), "created_at": parse_created_at(itin_doc),
    }

# --- (B) User profile ---
def load_user_profile(user_id: str) -> Dict:
    try: user_obj_id = ObjectId(user_id); or_clause = [{"user_id": user_obj_id}, {"user_id": user_id}]
    except Exception: or_clause = [{"user_id": user_id}]
    favs = list(favorites_col.find({"$or": or_clause})) if favorites_col is not None else []
    pvs = list(pageviews_col.find({"$or": or_clause})) if pageviews_col is not None else []
    saved_names, tag_ct, city_ct, browse_ct = [], Counter(), Counter(), {}
    for f in favs:
        if name := (f.get("place_name") or "").strip(): saved_names.append(name)
        if c := norm_city(f.get("city")): city_ct[c] += 1
        for zh in f.get("tags", []): tag_ct[zh] += 1
    for v in pvs:
        if n := (v.get("place_name") or "").strip():
            cnt = int(v.get("count", 1) or 1)
            browse_ct[n] = browse_ct.get(n, 0) + max(cnt, 1)
    return {
        "saved_attractions": list(dict.fromkeys(saved_names)),
        "liked_tags": [k for k,_ in tag_ct.most_common()],
        "liked_locations": [k for k,_ in city_ct.most_common()],
        "browse_counts": browse_ct,
    }

# --- (B) Scoring ---
WEIGHTS = {"location":2.0, "saved":5.0, "browse":1.5, "fresh":0.5}

def freshness_score(created_iso: str | None) -> float:
    if not created_iso: return 0.0
    try:
        created = datetime.fromisoformat(created_iso.replace("Z","+00:00"))
        days = (datetime.utcnow().replace(tzinfo=timezone.utc) - created).days
        return 1.0 / (1.0 + max(days, 0) / 30.0)
    except Exception: return 0.0

def score_itinerary_general(it: Dict) -> (float, List[str]):
    s, reasons = 0.0, []
    if f := freshness_score(it.get("created_at")):
        if f > 0: pts = 1.0 * f; s += pts; reasons.append(f"新發佈 +{pts:.2f}")
    if name_count := len(it.get("names", [])):
        if name_count > 0: pts = 0.1 * name_count; s += pts; reasons.append(f"內容豐富 +{pts:.1f}")
    return s, reasons

def score_itinerary_for_user(it: Dict, prof: Dict):
    s, reasons = 0.0, []
    it_names, it_cities = set(it["names"]), set(it["cities"])
    if loc_overlap := it_cities & set(prof.get("liked_locations", [])):
        pts = WEIGHTS["location"] * len(loc_overlap); s += pts; reasons.append(f"地點匹配 {sorted(loc_overlap)} +{pts:.1f}")
    if saved_ov := it_names & set(prof.get("saved_attractions", [])):
        pts = WEIGHTS["saved"] * len(saved_ov); s += pts; reasons.append(f"包含收藏 {sorted(saved_ov)[:3]} +{pts:.1f}")
    bc = prof.get("browse_counts", {})
    bpts = sum(WEIGHTS["browse"] * math.log(1+c, 2) for n,c in bc.items() if n in it_names)
    if bpts > 0: s += bpts; reasons.append(f"瀏覽加權 +{bpts:.1f}")
    if f := freshness_score(it.get("created_at")):
        if f > 0: pts = WEIGHTS["fresh"] * f; s += pts; reasons.append(f"新鮮度 +{pts:.2f}")
    return s, reasons

# --- 【新增】輔助函式：轉換資料庫文件為 Kotlin Trip 格式 ---
def _format_trip_for_kotlin(doc: Dict[str, Any]) -> Dict[str, Any]:
    """將從 structured_itineraries 讀取的 doc 轉換成 Kotlin Trip data class 預期的格式"""
    trip_id = str(doc.get("_id", ""))
    created_by = doc.get("user_id", "")
    name = pick_itinerary_title(doc) or "未命名行程"
    locations_list = doc.get("locations", [])
    locations_str = "、".join(locations_list) if isinstance(locations_list, list) else ""
    total_budget = doc.get("total_budget")
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
    styles = styles_raw if isinstance(styles_raw, list) else []
    visibility_raw = doc.get("visibility")
    visibility = visibility_raw if visibility_raw in ["PUBLIC", "PRIVATE"] else "PRIVATE"
    members = []
    
    kotlin_days = []
    nodes = doc.get("nodes", [])
    grouped_nodes: Dict[int, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
    
    if nodes:
        for node in nodes:
            if node:
                day_num = node.get("day")
                slot_label = node.get("slot")
                if isinstance(day_num, int) and slot_label:
                    activity = {
                        "id": node.get("place_id"),
                        "name": node.get("place_name") or node.get("name"),
                        "category": node.get("category"),
                        "stayMinutes": node.get("stay_minutes"),
                        "rating": node.get("rating"),
                        "reviews": node.get("reviews"),
                        "address": node.get("address"),
                        "mapUrl": node.get("map_url"),
                        "openText": node.get("open_text"),
                        "types": node.get("types", []),
                        "lat": node.get("lat"),
                        "lng": node.get("lng"),
                        "fromPrevLegMin": node.get("_from_prev_leg_min")
                    }
                    activity = {k: v for k, v in activity.items() if v is not None}
                    if "lat" in activity and "lng" in activity:
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
        "totalBudget": total_budget, "startDate": start_date, "endDate": end_date,
        "activityStart": activity_start, "activityEnd": activity_end, "avgAge": avg_age,
        "transportPreferences": transport_prefs, "useGmapsRating": use_gmaps_rating,
        "styles": styles, "visibility": visibility,
        "members": members, "days": kotlin_days
    }
    return kotlin_trip


# --- 【新增】API Route: 取得單一行程詳情 ---
@router.get("/trip/{trip_id}", summary="取得單一行程詳情")
def get_trip_details(trip_id: str):
    print(f"\n--- get_trip_details endpoint called with trip_id: {trip_id} ---")
    if itins_col is None: 
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")
    try:
        oid = ObjectId(trip_id)
        print(f"--- Attempting to find trip with ObjectId: {oid} ---")
        projection = {
            "_id": 1, "user_id": 1, "created_at": 1, "trip_preference_id": 1,
            "title": 1, "meta": 1, "locations": 1, "start_date": 1, "end_date": 1,
            "activity_start": 1, "activity_end": 1, "avg_age": 1, "transportation": 1,
            "use_gmaps_rating": 1, "preferences": 1, "visibility": 1, "total_budget": 1,
            "days": 1, "nodes": 1
        }
        trip_doc = itins_col.find_one({"_id": oid}, projection)
        if trip_doc:
            print(f"--- Trip found, formatting for Kotlin ---")
            kotlin_trip = _format_trip_for_kotlin(trip_doc)
            print(f"--- Returning formatted trip (first 200 chars): {str(kotlin_trip)[:200]}...")
            return kotlin_trip
        else:
            print(f"--- Trip not found in DB ---")
            raise HTTPException(status_code=404, detail=f"找不到行程 ID: {trip_id}")
    except Exception as e:
        print(f"--- Error in get_trip_details for ID {trip_id} ---")
        logging.getLogger("uvicorn.error").exception("Get trip detail failed")
        traceback.print_exc()
        if "is not a valid ObjectId" in str(e):
            raise HTTPException(status_code=400, detail=f"無效的行程 ID 格式: {trip_id}")
        else:
            raise HTTPException(status_code=500, detail=f"取得行程詳情時發生錯誤: {str(e)}")


@router.post(
    "/trips/{trip_id}/replace",
    summary="更換行程中的一個景點",
    description="將行程中的一個舊景點替換為一個新的景點"
)
def replace_activity_in_trip(trip_id: str, req: ReplaceActivityRequest):
    print(f"\n--- replace_activity_in_trip endpoint called for trip: {trip_id} ---")
    if itins_col is None:
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")

    try:
        oid = ObjectId(trip_id)
        
        projection = {
            "_id": 1, "user_id": 1, "created_at": 1, "trip_preference_id": 1,
            "title": 1, "meta": 1, "locations": 1, "start_date": 1, "end_date": 1,
            "activity_start": 1, "activity_end": 1, "avg_age": 1, "transportation": 1,
            "use_gmaps_rating": 1, "preferences": 1, "visibility": 1, "total_budget": 1,
            "days": 1, "nodes": 1
        }
        trip_doc = itins_col.find_one({"_id": oid}, projection)
        
        if not trip_doc:
            print(f"--- Trip not found: {trip_id} ---")
            raise HTTPException(status_code=404, detail=f"找不到行程 ID: {trip_id}")

        new_data = req.new_activity_data
        
        new_node_data = {
            "place_id": new_data.placeId,
            "place_name": new_data.name,
            "name": new_data.name,
            "address": new_data.address,
            "rating": new_data.rating,
            "reviews": new_data.userRatingsTotal,
            "lat": new_data.lat,
            "lng": new_data.lng,
            "openText": new_data.openStatusText,
        }
        new_node_data = {k: v for k, v in new_node_data.items() if v is not None}

        updated_nodes = []
        found = False

        for node in trip_doc.get("nodes", []):
            current_node_pid = node.get("place_id") or node.get("id")
            
            if current_node_pid == req.old_activity_id:
                found = True
                print(f"--- Found old activity: {node.get('name')}. Replacing... ---")
                
                old_node_info = node.copy()
                old_node_info.update(new_node_data)
                old_node_info["place_id"] = new_data.placeId
                old_node_info["name"] = new_data.name
                old_node_info["place_name"] = new_data.name
                
                updated_nodes.append(old_node_info)
            else:
                updated_nodes.append(node)

        if not found:
            print(f"--- Old activity ID not found in nodes: {req.old_activity_id} ---")
            raise HTTPException(status_code=404, detail=f"在行程中找不到舊景點 ID: {req.old_activity_id}")

        print(f"--- Updating database for trip {trip_id} with new nodes... ---")
        itins_col.update_one(
            {"_id": oid},
            {"$set": {"nodes": updated_nodes}}
        )

        print(f"--- Update complete. Fetching updated document... ---")
        updated_doc = itins_col.find_one({"_id": oid}, projection)
        if not updated_doc:
             raise HTTPException(status_code=500, detail="更新後讀取行程失敗")
             
        print(f"--- Returning formatted trip to client. ---")
        return _format_trip_for_kotlin(updated_doc)

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"--- Error in replace_activity_in_trip for ID {trip_id} ---")
        logging.getLogger("uvicorn.error").exception("Replace activity failed")
        traceback.print_exc()
        if "is not a valid ObjectId" in str(e):
            raise HTTPException(status_code=400, detail=f"無效的行程 ID 格式: {trip_id}")
        else:
            raise HTTPException(status_code=500, detail=f"更換景點時發生錯誤: {str(e)}")


# --- (B) API Route for General/Personal Recommendations ---
@router.get(
    "/api/recommendations",
    summary="[通用/個人化] 取得推薦行程列表",
    description="若提供 user_id 則回傳個人化推薦；若無則回傳通用推薦。"
)
def recommendations(
    user_id: str | None = Query(default=None),
    top_k: int = Query(3, ge=1, le=10),
    more_k: int = Query(20, ge=0, le=100),
):
    profile = None
    if user_id:
        try: 
            profile = load_user_profile(user_id)
        except Exception as e: 
            print(f"Error loading profile for {user_id}: {e}")
            user_id = None
            profile = None
    
    projection = {
        "_id": 1, "user_id": 1, "created_at": 1, "trip_preference_id": 1, 
        "title": 1, "meta": 1, "locations": 1, "start_date": 1, "end_date": 1, 
        "activity_start": 1, "activity_end": 1, "avg_age": 1, "transportation": 1, 
        "use_gmaps_rating": 1, "preferences": 1, "visibility": 1, "total_budget": 1, 
        "days": 1, "nodes": 1
    }
    
    if itins_col is None: 
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")
    
    all_trips_with_scores = []
    for doc in itins_col.find({}, projection):
        try:
            features = extract_itinerary_features(doc)
            if user_id and profile: 
                score, reasons = score_itinerary_for_user(features, profile)
            else: 
                score, reasons = score_itinerary_general(features)
            all_trips_with_scores.append({"doc": doc, "score": score, "reasons": reasons})
        except Exception as process_error: 
            print(f"⚠️ 無法處理行程 (ID: {doc.get('_id')}): {process_error}")
            traceback.print_exc()
    
    all_trips_with_scores.sort(key=lambda x: x["score"], reverse=True)
    
    formatted_trips = []
    for item in all_trips_with_scores:
        try:
            kotlin_trip = _format_trip_for_kotlin(item["doc"])
            formatted_trips.append(kotlin_trip)
        except Exception as format_error: 
            print(f"⚠️ 無法格式化行程 (ID: {item['doc'].get('_id')}): {format_error}")
            traceback.print_exc()
    
    general_weights = {"freshness": 1.0, "content_richness": 0.1}
    return {
        "user_id": user_id or "general", 
        "generated_at": datetime.utcnow().isoformat() + "Z", 
        "weights": WEIGHTS if user_id else general_weights, 
        "profile": profile, 
        "top3": formatted_trips[:top_k], 
        "more": formatted_trips[top_k : top_k + more_k]
    }


# --- 【新增】API Route: 取得替代景點 ---
class AlternativesRequest(BaseModel):
    current_place_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    weather: Optional[str] = None
    user_id: Optional[str] = None
    radius_meters: int = 5000
    max_results: int = 10

# 在 get_alternatives 函式之前加入這個輔助函式
def _format_place_for_kotlin_lite(place: Dict[str, Any], google_api_key: str = None) -> Optional[Dict[str, Any]]:
    """
    將 Google Places API 的回應轉換成 Kotlin Alternative 格式
    """
    try:
        place_id_full = place.get("name")
        if not place_id_full or not place_id_full.startswith("places/"):
            return None
        place_id = place_id_full.split('/')[-1]

        location = place.get("location")
        if not location or not isinstance(location.get("latitude"), (int, float)) or not isinstance(location.get("longitude"), (int, float)):
             return None

        display_name_info = place.get("displayName")
        name = display_name_info.get("text") if isinstance(display_name_info, dict) else place.get("name")
        if not name:
            return None

        photo_ref = place.get("photos", [{}])[0].get("name")
        # 如果有 API Key 就加上，沒有就不加
        if google_api_key and photo_ref:
            photo_url = f"https://places.googleapis.com/v1/{photo_ref}/media?maxWidthPx=400&key={google_api_key}"
        elif photo_ref:
            photo_url = f"https://places.googleapis.com/v1/{photo_ref}/media?maxWidthPx=400"
        else:
            photo_url = None

        rating = place.get("rating")
        user_rating_count = place.get("userRatingCount")
        address = place.get("shortFormattedAddress") or place.get("formattedAddress")
        open_now = place.get("regularOpeningHours", {}).get("openNow")

        lite = {
            "placeId": place_id, 
            "name": name, 
            "lat": location["latitude"], 
            "lng": location["longitude"],
            "address": address, 
            "rating": rating, 
            "userRatingsTotal": user_rating_count,
            "photoUrl": photo_url, 
            "openingHours": [], 
            "openNow": open_now, 
            "openStatusText": None
        }
        return {k: v for k, v in lite.items() if v is not None}
    except Exception as e:
        print(f"Error formatting place {place.get('name')}: {e}")
        return None


@router.post("/alternatives", summary="取得替代景點建議")
async def get_alternatives(
    request: Request,  # 👈 必須有這個參數
    payload: AlternativesRequest
):
    print(f"\n--- get_alternatives endpoint called with: {payload} ---")
    
    # 👇 從 app.state 讀取（不是用全域變數）
    google_api_key = request.app.state.google_api_key
    
    print(f"[DEBUG] Checking GOOGLE_MAPS_API_KEY...")
    print(f"[DEBUG] GOOGLE_MAPS_API_KEY exists: {google_api_key is not None}")
    
    if not google_api_key:
        raise HTTPException(
            status_code=500, 
            detail="Google Maps API Key 未設定"
        )

    lat = payload.lat
    lng = payload.lng
    
    # === 檢查 2: 座標 ===
    print(f"[DEBUG] Initial coordinates - lat: {lat}, lng: {lng}")
    
    if not lat or not lng:
        print(f"--- Lat/Lng not provided, fetching details for place ID: {payload.current_place_id} ---")
        try:
            async with httpx.AsyncClient() as client:
                details_url = f"{PLACES_API_ENDPOINT}/{payload.current_place_id}"
                print(f"[DEBUG] Fetching from: {details_url}")
                
                headers = {
                    "X-Goog-Api-Key": google_api_key,
                    "X-Goog-FieldMask": "location"
                }
                print(f"[DEBUG] Headers: X-Goog-Api-Key: {google_api_key[:20]}..., FieldMask: location")
                
                response = await client.get(details_url, headers=headers)
                print(f"[DEBUG] Place Details response status: {response.status_code}")
                
                response.raise_for_status()
                details = response.json()
                print(f"[DEBUG] Place Details response: {details}")
                
                location = details.get("location")
                if location and isinstance(location.get("latitude"), (int, float)) and isinstance(location.get("longitude"), (int, float)):
                    lat = location["latitude"]
                    lng = location["longitude"]
                    print(f"--- Got location from details: lat={lat}, lng={lng} ---")
                else:
                    print(f"[ERROR] Invalid location data: {location}")
                    raise HTTPException(status_code=404, detail="找不到指定景點的座標")
                    
        except httpx.HTTPStatusError as e:
            print(f"[ERROR] Google Places API error (details): {e.response.status_code}")
            print(f"[ERROR] Response text: {e.response.text}")
            traceback.print_exc()
            raise HTTPException(
                status_code=e.response.status_code, 
                detail=f"查詢景點詳情失敗: {e.response.text}"
            )
        except Exception as e:
            print(f"[ERROR] Unexpected error fetching place details: {e}")
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"查詢景點詳情時發生錯誤: {str(e)}")

    if not lat or not lng:
        print("[ERROR] Still no coordinates after fetch!")
        raise HTTPException(status_code=400, detail="無法取得目前景點座標")

    # === 檢查 3: 決定搜尋類型 ===
    included_types = ["tourist_attraction"]
    rank_preference = "POPULARITY"
    
    if payload.weather == "rainy":
        print("--- Weather is rainy, prioritizing indoor types ---")
        included_types = [
            "museum", "art_gallery", "shopping_mall", "cafe", 
            "restaurant", "movie_theater", "aquarium", "library"
        ]
        rank_preference = "DISTANCE"

    print(f"--- Calling Nearby Search: lat={lat}, lng={lng}, radius={payload.radius_meters}, types={included_types} ---")
    alternatives = []
    
    # === 檢查 4: 附近搜尋 ===
    try:
        async with httpx.AsyncClient() as client:
            nearby_url = f"{PLACES_API_ENDPOINT}:searchNearby"
            print(f"[DEBUG] Nearby Search URL: {nearby_url}")
            
            headers = {
                "X-Goog-Api-Key": google_api_key,
                "X-Goog-FieldMask": "places.name,places.displayName,places.location,places.shortFormattedAddress,places.formattedAddress,places.rating,places.userRatingCount,places.photos,places.regularOpeningHours"
            }
            
            search_payload = {
                "includedTypes": included_types,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": payload.radius_meters
                    }
                },
                "languageCode": "zh-TW",
                "regionCode": "TW",
                "rankPreference": rank_preference,
                "maxResultCount": payload.max_results + 5
            }
            print(f"[DEBUG] Search payload: {search_payload}")
            
            response = await client.post(nearby_url, headers=headers, json=search_payload)
            print(f"[DEBUG] Nearby Search response status: {response.status_code}")
            
            response.raise_for_status()
            results = response.json()
            
            print(f"--- Nearby Search returned {len(results.get('places', []))} places ---")
            print(f"[DEBUG] First place (if exists): {results.get('places', [{}])[0] if results.get('places') else 'No places'}")
            
            for place in results.get("places", []):
                place_id_full = place.get("name")
                if not place_id_full:
                    continue
                    
                place_id = place_id_full.split('/')[-1]
                if place_id == payload.current_place_id:
                    print(f"--- Skipping original place: {place.get('displayName', {}).get('text')} ---")
                    continue
                
                formatted = _format_place_for_kotlin_lite(place, google_api_key)
                if formatted:
                    alternatives.append(formatted)
                else:
                    print(f"[WARN] Could not format place: {place.get('name')}")
            
            alternatives = alternatives[:payload.max_results]
            print(f"--- Formatted {len(alternatives)} alternatives ---")
            
    except httpx.HTTPStatusError as e:
        print(f"[ERROR] Google Places API error (nearby): {e.response.status_code}")
        print(f"[ERROR] Response text: {e.response.text}")
        traceback.print_exc()
        raise HTTPException(
            status_code=e.response.status_code, 
            detail=f"搜尋附近景點失敗: {e.response.text}"
        )
    except Exception as e:
        print(f"[ERROR] Unexpected error during Nearby Search: {e}")
        print(f"[ERROR] Exception type: {type(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"搜尋替代景點時發生錯誤: {str(e)}")

    print(f"[SUCCESS] Returning {len(alternatives)} alternatives")
    return alternatives