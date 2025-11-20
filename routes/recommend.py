# 檔案： routes/recommend.py
# (✅ 最終修正：將 members 和 chat_id 邏輯直接加入此檔案的 _persist_structured_itinerary 函式)

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from markdown2 import markdown as md
from bson import SON, ObjectId
import traceback
import logging
import os
import httpx

# --- 您的 imports ---
from models.schemas import RecommendRequest, RecommendGroupRequest, ReplaceActivityRequest, Alternative
from core.mongo import (
    users_collection,     # 👈 同步
    get_user,
    save_form,            # 👈 同步
    form_collection,      # 👈 同步
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

from typing import List, Dict, Any, Optional
import re, json, math
from collections import Counter, defaultdict
from pydantic import BaseModel, Field

# 匯入您在 main.py 中定義的 db 物件 (雖然我們在這裡主要使用 core.mongo)
# from main import db as motor_db # 避免混淆，我們只用 core.mongo

router = APIRouter()

# --- Google Maps API ---
PLACES_API_ENDPOINT = "https://places.googleapis.com/v1/places"

# --- 資料庫 collections (同步 PyMongo) ---
# ✅ 【關鍵修正】
# 我們【只】使用從 core.mongo 匯入的【同步】 collections
try:
    db = mongo.db 
    itins_col = db["structured_itineraries"] # <--- 這是行程集合
    # chatroom_collection = db["chatroom"] # <--- 我們會在函式中直接用 db["chatroom"]
    favorites_col = db["user_favorite"]
    pageviews_col = db["user_browse"]
except Exception as e:
    print(f"⚠️ 警告：無法載入 '通用推薦' 所需的 MongoDB collections: {e}")
    itins_col = None
    favorites_col = None
    pageviews_col = None
# --- 結束 collections ---


# --- 常數和同步 Helper functions (保持不變) ---
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

def _shape_personal_form_to_group_format(raw_form: dict, user_id: str) -> dict:
     return {"leader_id": user_id, "members": [{"user_id": user_id}], "trip_name": raw_form.get("trip_name") or raw_form.get("name"), "date": raw_form.get("start_date") or raw_form.get("date"), "days": raw_form.get("days"), "locations": raw_form.get("locations"), "time_range": raw_form.get("activity_start") or raw_form.get("time_range"), "preferences": _clean_str_list(raw_form.get("preferences") or raw_form.get("styles")), "exclude": _clean_str_list(raw_form.get("exclude")), "notes": raw_form.get("notes") or raw_form.get("extraNote")}

def _run_planner(user_id: str, form_payload: dict) -> dict:
    # 假設 langgraph_nodes 裡的函式是同步 (CPU-bound)
    state = {"user_id": user_id, "form": form_payload}; state = extract_profile(state); state = analyze_preferences(state); state = generate_daily_slots(state); state = validate_plan_with_llms(state); state = assemble_markdown(state); return return_plan(state)

# 專案所需的欄位
TRIP_PROJECTION = {
    "_id": 1, "user_id": 1, "created_at": 1, "trip_preference_id": 1,
    "title": 1, "meta": 1, "locations": 1, "start_date": 1, "end_date": 1,
    "activity_start": 1, "activity_end": 1, "avg_age": 1, "transportation": 1,
    "use_gmaps_rating": 1, "preferences": 1, "visibility": 1, "total_budget": 1,
    "days": 1, "nodes": 1, "cover_photo_url": 1,
    
    # ===== ✅ 確保讀取得到我們的新欄位 =====
    "members": 1, "chat_id": 1 
}


# ================== ⬇️ 這裡是*唯一*被修改的函式 ⬇️ ==================
def _persist_structured_itinerary(
    *, user_id: str, form_id: str | None, result: dict, title: str = "",
    fallback_locations: list[str] | None = None,
    original_form: dict = {}
) -> str:
    """
    【修改版】
    將行程規劃結果存入 structured_itineraries (使用 sync)。
    同時建立 chatroom 並雙向綁定 ID。
    """
    if itins_col is None: raise ValueError("structured_itineraries collection is not available.")
    
    # --- 1. (原邏輯) 處理行程 ---
    itin_json = result.get("itinerary_json") or {}; days_list = itin_json.get("days") or []; days_for_db, all_nodes = [], []
    for i, d in enumerate(days_list, start=1):
        slots = d.get("slots", []); slot_nodes = []
        for s in slots: slot_nodes.append({"day": i, "slot": s.get("label"), "start": s.get("window", [None, None])[0], "end": s.get("window", [None, None])[1], "places": s.get("places", [])})
        head_id = None
        if slot_nodes:
            try: from core.itinerary_linked_list import build_linked_list, flatten_linked; head = build_linked_list(slot_nodes); flat = flatten_linked(head); all_nodes.extend(flat.get("nodes", [])); head_id = flat.get("head_id")
            except (ImportError, TypeError): pass
        days_for_db.append({"day": i, "date": d.get("date"), "city": d.get("city"), "head_id": head_id})
    
    # --- 2. (原邏輯) 處理封面圖片 ---
    cover_photo_url = None
    if all_nodes:
        try:
            first_place = all_nodes[0].get("places", [{}])[0]
            if first_place:
                 cover_photo_url = first_place.get("photoUrl")
                 print(f"[DEBUG] 找到封面圖片: {cover_photo_url}")
        except Exception:
            pass 
    
    # --- 3. (原邏輯) 準備行程文件 (doc) ---
    locations = to_locations_list(itin_json.get("locations") or fallback_locations)
    form_data = original_form 
    
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
        "cover_photo_url": cover_photo_url,
        "days": days_for_db,
        "nodes": all_nodes,
        "summary": result.get("summary", ""), "html": result.get("html", ""),
        "used_places": result.get("used_places", []),
        
        # ===== ✅ 4. (新邏輯) 新增 members 欄位 =====
        "members": [user_id]
    }
    
    # ===== ✅ 5. (新邏輯) 執行儲存與雙向綁定 =====
    
    # 5a. 插入行程
    insert_result = itins_col.insert_one(doc)
    trip_id = insert_result.inserted_id
    
    # 5b. 建立 chatroom
    chat_doc = {
        "trip_id": trip_id,              # 將 trip_id 寫入 chatroom
        "created_at": doc["created_at"], # 使用相同的建立時間
        "messages": [],                  
        "members": [user_id]             
    }
    # (我們從 line 42 知道 db = mongo.db)
    chat_result = db["chatroom"].insert_one(chat_doc)
    chat_id = chat_result.inserted_id

    # 5c. 將 chat_id 寫回行程文件
    itins_col.update_one(
        {"_id": trip_id},
        {"$set": {"chat_id": chat_id}}
    )

    # 5d. 回傳 trip_id (與原函式行為一致)
    return str(trip_id)
# ================== ⬆️ 這裡是*唯一*被修改的函式 ⬆️ ==================


def _decide_travel_mode(transportation: str | None, transport_text: str | None) -> str:
    t = (transportation or "").strip().lower()
    if t in {"drive", "driving", "car", "汽車"}: return "driving"
    if t in {"public", "transit", "bus", "metro", "train", "大眾運輸"}: return "transit"
    return "walking"

def _planner_flags() -> dict:
    return {"ban_quick_stops": True, "grid_diversity": True, "dinner_min_reviews": 120, "dinner_min_rating": 4.2}


# --- API Route: 產生推薦行程 (修改為 def) ---
@router.post("/", summary="產生推薦行程", description="根據使用者問卷與收藏紀錄，自動產生每日行程")
def recommend_trip(req: RecommendRequest, request: Request): # 👈 ✅ 改回同步 def
    print("\n--- [SYNC] recommend_trip endpoint called ---")
    
    print(f"--- Pydantic model received: {req.model_dump_json(indent=2)[:500]}...")

    print("--- Attempting Pydantic validation and main logic ---")
    try:
        # ✅ 【關鍵修正】移除 'await'
        user = users_collection.find_one({"username": req.user_id})
        if not user:
            print(f"--- User not found: {req.user_id} ---")
            raise HTTPException(status_code=404, detail=f"找不到使用者 {req.user_id}")

        form_data = req.form.copy() if req.form else {}
        if favorites := user.get("favorites"):
            form_data.setdefault("偏好", []).append({"source": "收藏", "類型": favorites})

        locations_arr = to_locations_list(form_data.get("locations"))
        if locations_arr: form_data["locations"] = locations_arr

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
        
        # ✅ 【關鍵修正】移除 'await'
        save_form(req.user_id, _canonize_form_order(group_like_form), form_type="personal", created_at=created_at)
        
        # ✅ 【關鍵修正】移除 'await'
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

        print("--- Calling _run_planner (sync) ---")
        result = _run_planner(user_id=req.user_id, form_payload=form_payload)
        print("--- _run_planner finished ---")

        # ✅ 【關鍵修正】移除 'await'
        # (現在這個函式會正確地建立 chatroom 並加入 members)
        trip_id = _persist_structured_itinerary(
            user_id=req.user_id, 
            form_id=form_id, 
            result=result,
            title=(form_data.get("trip_name") or "未命名行程"),
            fallback_locations=locations_arr,
            original_form=form_payload # 👈 傳入原始表單
        )

        print("--- Reached end of recommend_trip try block, building response ---")

        # ✅ 【關鍵修正】移除 'await'
        full_trip_doc = itins_col.find_one({"_id": ObjectId(trip_id)}, TRIP_PROJECTION)
        if not full_trip_doc:
            raise HTTPException(status_code=500, detail="行程儲存後無法立即讀取")

        android_trip_response = _format_trip_for_kotlin(full_trip_doc)

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
#            ↓↓↓ 【通用推薦 API 及輔助函式 (已改回 sync)】 ↓↓↓
# ======================================================================

# --- (B) Utilities ---
def norm_city(c: str | None) -> str | None:
    if not c: return None; c = c.strip().replace("台", "臺"); c = re.sub(r"(市|縣)$", "", c); return c

# ✅ 【🎉 新增功能】加回被誤刪的函式
SPLIT_RE = re.compile(r"[，,、/]+")
def split_names(s: str | None) -> list[str]:
    if not isinstance(s, str) or not s.strip() or s == "(空行程)":
        return []
    try:
        return [p.strip() for p in SPLIT_RE.split(s) if p.strip()]
    except Exception:
        return []

# ✅ 【🎉 新增功能】加回被誤刪的函式
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

# ✅ 【🎉 新增功能】加回被誤刪的函式
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
    
# --- (B) Features from itinerary ---
def extract_itinerary_features(itin_doc: Dict) -> Dict:
    names = []
    try:
        if nodes := itin_doc.get("nodes"):
            for node in nodes:
                if isinstance(node, dict):
                    node_name = node.get("place_name") or node.get("name")
                    names.extend(split_names(node_name)) # 👈 現在 'split_names' 存在了
    except Exception as e:
        print(f"⚠️ Error extracting names in extract_itinerary_features (ID: {itin_doc.get('_id')}): {e}")

    names = list(dict.fromkeys(n for n in names if n))
    cities = set()
    
    form_id = itin_doc.get("trip_preference_id")
    # ✅ 【關鍵修正】 檢查 'is not None'
    if form_id and form_collection is not None:
        try:
            form = form_collection.find_one({"_id": ObjectId(str(form_id))}) or {}
            if loc := form.get("form", {}).get("location"):
                if c := norm_city(loc): cities.add(c)
        except Exception: pass
    
    official_title = pick_itinerary_title(itin_doc) # 👈 現在 'pick_itinerary_title' 存在了
    fallback_title = " / ".join(names[:2]) if names else "客製行程"
    title = official_title or fallback_title
    return {
        "id": str(itin_doc.get("_id")), "itinerary_name": title, "names": names,
        "cities": sorted(cities), "created_at": parse_created_at(itin_doc), # 👈 現在 'parse_created_at' 存在了
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

# --- (B) Scoring (Sync) ---
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

# --- 輔助函式：轉換資料庫文件為 Kotlin Trip 格式 ---
def _format_trip_for_kotlin(doc: Dict[str, Any]) -> Dict[str, Any]:
    """將從 structured_itineraries 讀取的 doc 轉換成 Kotlin Trip data class 預期的格式"""
    
    if not doc:
        print("⚠️ 警告：format_trip_for_kotlin 收到了 None")
        return {}
        
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
    transport_prefs = [transportation] if isinstance(transportation, str) and transportation else []
    use_gmaps_rating_raw = doc.get("use_gmaps_rating")
    use_gmaps_rating = use_gmaps_rating_raw if use_gmaps_rating_raw is not None else True
    styles_raw = doc.get("preferences")
    styles = styles_raw if isinstance(styles_raw, list) else []
    visibility_raw = doc.get("visibility")
    visibility = visibility_raw if visibility_raw in ["PUBLIC", "PRIVATE"] else "PRIVATE"
    
    # ===== ✅ 讀取 members 欄位 =====
    # (如果 doc 中沒有 "members" 欄位，mongo.get 會回傳 None，所以預設為 [])
    members = doc.get("members", []) 
    
    # ✅ 【🎉 新增功能】讀取封面圖片
    cover_photo_url = doc.get("cover_photo_url")
    
    kotlin_days = []
    nodes = doc.get("nodes", [])
    grouped_nodes: Dict[int, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
    
    if nodes:
        # ✅ 【🎉 新增功能】 - Fallback 邏輯
        if not cover_photo_url:
             try:
                 first_place = nodes[0].get("places", [{}])[0]
                 if first_place:
                     cover_photo_url = first_place.get("photoUrl") or first_place.get("map_url")
             except Exception:
                 pass 

        for node in nodes:
            if not node: continue
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
        "id": trip_id, 
        "createdBy": created_by, 
        "name": name, 
        "locations": locations_str,
        "photoUrl": cover_photo_url,# 👈 ✅ 加上封面圖片 (App 需為 camelCase)
        "totalBudget": total_budget,
        "startDate": start_date,
        "endDate": end_date,
        "activityStart": activity_start,
        "activityEnd": activity_end,
        "avgAge": avg_age,
        "transportPreferences": transport_prefs,
        "useGmapsRating": use_gmaps_rating,
        "styles": styles,
        "visibility": visibility,
        "members": members, # <--- ✅ 加上 members 欄位
        "days": kotlin_days,
        "chatId": str(doc.get("chat_id", "")) # <--- ✅ 加上 chatId 欄位
    }
    # 移除值為 None 或 "IGNORE" 的欄位 (avgAge 除外)
    final_trip = {k: v for k, v in kotlin_trip.items() if v is not None}
    # if avg_age_raw is None:
    #     final_trip.pop("avgAge", None) # 如果 avg_age 是 None，就不要這個欄位
    # else:
    #     final_trip["avgAge"] = avg_age # 確保 "IGNORE" 或真實值被保留
    
    if not final_trip.get("chatId"): # 如果 chat_id 是空字串，移除
        final_trip.pop("chatId", None)
        
    return final_trip


# --- API Route: 取得單一行程詳情 ---
@router.get("/trip/{trip_id}", summary="取得單一行程詳情")
def get_trip_details( # 👈 ✅ 改回同步 def
    request: Request, 
    trip_id: str = Path(...)
):
    print(f"\n--- [SYNC] get_trip_details endpoint called with trip_id: {trip_id} ---")
    
    if itins_col is None: 
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")
        
    try:
        oid = ObjectId(trip_id)
        print(f"--- Attempting to find trip with ObjectId: {oid} ---")
        
        # ✅ 使用我們更新過的 TRIP_PROJECTION
        trip_doc = itins_col.find_one({"_id": oid}, TRIP_PROJECTION) 
        
        if trip_doc:
            print(f"--- Trip found, formatting for Kotlin ---")
            # ✅ _format_trip_for_kotlin 現在會處理 members 和 chat_id
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


# ⛔️ 【關鍵修正】
# 已刪除舊的、有 bug 的 replace_activity_in_trip 函式


# --- API Route for General/Personal Recommendations ---
@router.get(
    "/api/recommendations",
    summary="[通用/個人化] 取得推薦行程列表",
    description="若提供 user_id 則回傳個人化推薦；若無則回傳通用推薦。"
)
def recommendations( # 👈 ✅ 改回同步 def
    request: Request,
    user_id: str | None = Query(default=None),
    top_k: int = Query(3, ge=1, le=10),
    more_k: int = Query(20, ge=0, le=100),
):
    print("\n--- [SYNC] /api/recommendations endpoint called ---") # 👈 加上日誌
    
    if itins_col is None or favorites_col is None or pageviews_col is None: 
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")

    profile = None
    if user_id:
        try: 
            print(f"--- Loading profile for user: {user_id} ---")
            profile = load_user_profile(user_id) # 👈 同步
        except Exception as e: 
            print(f"Error loading profile for {user_id}: {e}")
            user_id = None
            profile = None
    
    all_trips_with_scores = []
    
    print("--- Finding trips in database... ---")
    # ✅ 使用我們更新過的 TRIP_PROJECTION
    for doc in itins_col.find({}, TRIP_PROJECTION): 
        try:
            features = extract_itinerary_features(doc) # 👈 同步
            if user_id and profile: 
                score, reasons = score_itinerary_for_user(features, profile)
            else: 
                score, reasons = score_itinerary_general(features)
            all_trips_with_scores.append({"doc": doc, "score": score, "reasons": reasons})
        except Exception as process_error: 
            print(f"⚠️ 無法處理行程 (ID: {doc.get('_id')}): {process_error}")
            traceback.print_exc()
    
    print(f"--- Found and scored {len(all_trips_with_scores)} trips. Sorting... ---")
    all_trips_with_scores.sort(key=lambda x: x["score"], reverse=True)
    
    formatted_trips = []
    for item in all_trips_with_scores:
        try:
            # ✅ _format_trip_for_kotlin 現在會處理 members 和 chat_id
            kotlin_trip = _format_trip_for_kotlin(item["doc"])
            formatted_trips.append(kotlin_trip)
        except Exception as format_error: 
            print(f"⚠️ 無法格式化行程 (ID: {item['doc'].get('_id')}): {format_error}")
            traceback.print_exc()
    
    general_weights = {"freshness": 1.0, "content_richness": 0.1}
    print("--- Returning formatted trips to client. ---")
    return {
        "user_id": user_id or "general", 
        "generated_at": datetime.utcnow().isoformat() + "Z", 
        "weights": WEIGHTS if user_id else general_weights, 
        "profile": profile, 
        "top3": formatted_trips[:top_k], 
        "more": formatted_trips[top_k : top_k + more_k]
    }


# --- API Route: 取得替代景點 ---
class AlternativesRequest(BaseModel):
    current_place_id: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    weather: Optional[str] = None
    user_id: Optional[str] = None
    radius_meters: int = 5000
    max_results: int = 10

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
        if google_api_key and photo_ref:
            # 建立完整的 Google Photo URL
            photo_url = f"https://places.googleapis.com/v1/{photo_ref}/media?maxWidthPx=400&key={google_api_key}"
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
            "photoUrl": photo_url, # 👈 確保 photoUrl 被傳遞
            "openingHours": [], 
            "openNow": open_now, 
            "openStatusText": None
        }
        return {k: v for k, v in lite.items() if v is not None}
    except Exception as e:
        print(f"Error formatting place {place.get('name')}: {e}")
        return None


@router.post("/alternatives", summary="取得替代景點建議")
async def get_alternatives( # 👈 保持 async，因為它使用 httpx
    request: Request,
    payload: AlternativesRequest
):
    print(f"\n--- [ASYNC] get_alternatives endpoint called with: {payload} ---")
    
    google_api_key = request.app.state.google_api_key
    
    if not google_api_key:
        raise HTTPException(
            status_code=500, 
            detail="Google Maps API Key 未設定"
        )

    lat = payload.lat
    lng = payload.lng
    
    if not lat or not lng:
        print(f"--- Lat/Lng not provided, fetching details for place ID: {payload.current_place_id} ---")
        try:
            async with httpx.AsyncClient() as client:
                details_url = f"{PLACES_API_ENDPOINT}/{payload.current_place_id}"
                headers = {
                    "X-Goog-Api-Key": google_api_key,
                    "X-Goog-FieldMask": "location"
                }
                response = await client.get(details_url, headers=headers)
                response.raise_for_status()
                details = response.json()
                location = details.get("location")
                if location and isinstance(location.get("latitude"), (int, float)) and isinstance(location.get("longitude"), (int, float)):
                    lat = location["latitude"]
                    lng = location["longitude"]
                    print(f"--- Got location from details: lat={lat}, lng={lng} ---")
                else:
                    raise HTTPException(status_code=404, detail="找不到指定景點的座標")
                    
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=e.response.status_code, 
                detail=f"查詢景點詳情失敗: {e.response.text}"
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"查詢景點詳情時發生錯誤: {str(e)}")

    if not lat or not lng:
        raise HTTPException(status_code=400, detail="無法取得目前景點座標")

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
    
    try:
        async with httpx.AsyncClient() as client:
            nearby_url = f"{PLACES_API_ENDPOINT}:searchNearby"
            headers = {
                "X-Goog-Api-Key": google_api_key,
                # ✅ 【🎉 新增功能】
                # 確保 'photos' 欄位在 FieldMask 中
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
                "maxResultCount": min(payload.max_results, 20)
            }
            
            response = await client.post(nearby_url, headers=headers, json=search_payload)
            response.raise_for_status()
            results = response.json()
            
            print(f"--- Nearby Search returned {len(results.get('places', []))} places ---")
            
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
        raise HTTPException(
            status_code=e.response.status_code, 
            detail=f"搜尋附近景點失敗: {e.response.text}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜尋替代景點時發生錯誤: {str(e)}")

    print(f"[SUCCESS] Returning {len(alternatives)} alternatives")
    return alternatives