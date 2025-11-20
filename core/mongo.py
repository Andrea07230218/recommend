# core/mongo.py
from pymongo import MongoClient
from datetime import datetime, timedelta
from markdown2 import markdown as md
import uuid
from bson import ObjectId
from collections import Counter, defaultdict
from typing import Dict, Any, List, Optional, Sequence # 確保引入所有需要的型別

# ✅ 連線到本地 MongoDB / Atlas
mongo_client = MongoClient("mongodb+srv://anne:1218@cluster0.g54wj9s.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

# ✅ 指定資料庫與集合
db = mongo_client["tripDemo-shan"]
users_collection = db["users"]
form_collection = db["forms"]
itineraries_collection = db["itineraries"]
chatroom_collection = db["chatroom"]

# === 可選：建立常用索引 (省略內容) =========================
def ensure_indexes() -> None:
    """
    為瀏覽/收藏與行程相關集合建立常用索引，提升查詢效能。
    """
    db["user_browse"].create_index([("user_id", 1), ("created_at", -1)])
    db["user_browse"].create_index([("user_id", 1), ("city", 1)])
    db["user_favorite"].create_index([("user_id", 1), ("place_name", 1)])
    db["forms"].create_index([("user_id", 1), ("created_at", -1)])
    db["itineraries"].create_index([("user_id", 1), ("created_at", -1)])
    db["structured_itineraries"].create_index([("user_id", 1), ("created_at", -1)])
    db["forms"].create_index([("transportation", 1), ("created_at", -1)])
    db["chatroom"].create_index([("trip_id", 1)])

# ============================== 新增功能 =====================================

def get_member_object_ids(db, member_usernames: List[str]) -> Dict[str, str]:
    """
    功能：根據 Username 列表，批量查找並回傳其對應的 MongoDB _id 字串。
    這是為了在偏好分析時，能用 Username 查到行為數據表裡儲存的 ObjectId。
    回傳：{"happy@gmail.com": "691727af55bf73270e731232", ...}
    """
    if not member_usernames:
        return {}
        
    # 查找 users collection
    user_docs = db["users"].find(
        # 使用 $in 批量查找所有提供的 username
        {"username": {"$in": list(set(member_usernames))}},
        {"_id": 1, "username": 1} # 只抓取 _id 和 username
    )
    
    result = {}
    for user_doc in user_docs:
        # 將 ObjectId 轉換成字串，並以 Username 為鍵儲存
        result[user_doc["username"]] = str(user_doc["_id"]) 
        
    return result

# ============================== 既有功能 =====================================

# ✅ 查詢使用者
def get_user(username: str):
    return users_collection.find_one({'username': username})

# ✅ 儲存個人或團體填寫的表單
def save_form(user_id, form_data, form_type="personal", created_at=None):
    if not created_at:
        created_at = datetime.utcnow()

    if "transportation" not in form_data:
        form_data["transportation"] = derive_transportation(form_data)
    else:
        form_data["transportation"] = derive_transportation(form_data)

    doc = {
        "user_id": user_id,
        "form_type": form_type,
        **form_data,
        "created_at": created_at
    }
    db["forms"].insert_one(doc)
    return created_at



def derive_transportation(form_data: Dict[str, Any]) -> str:
    v = str(form_data.get("transportation", "")).strip().lower()
    if v in {"drive", "driving", "car"}:
        return "drive"
    if v in {"public", "transit", "bus", "metro", "train"}:
        return "public"
    text = str(form_data.get("交通方式", "")).strip().lower()
    if any(k in text for k in ["汽車", "開車", "自駕", "車", "driving", "car", "drive"]):
        return "drive"
    if any(k in text for k in ["大眾", "捷運", "公車", "地鐵", "火車", "客運", "transit", "public", "bus", "metro", "train"]):
        return "public"
    return "public"


def save_itinerary(user_id, itinerary, form_type="personal", created_at=None):
    if not created_at:
        created_at = datetime.utcnow()
    
    doc = {
        "user_id": user_id,
        "form_type": form_type,
        "created_at": created_at,
        "itinerary": itinerary
    }
    db["itineraries"].insert_one(doc)

# ================== ⬇️ 這裡是修改後的第一個函式 ⬇️ ==================
def itinerary_linkedlist_to_day_structure(
    user_id, head, form_type="personal", trip_preference_id=None, created_at=None,
    # ===== ✅ NEW：新增參數以接收您要的額外欄位 =====
    form_data: Dict[str, Any] = None,
    summary: str = "",
    html: str = "",
    nodes: List[Dict[str, Any]] = None
):
    """
    【修改版】
    將 linked list 轉換為行程文件。
    現在會*同時*合併 form_data、summary、html 和 nodes。
    """
    if not created_at:
        created_at = datetime.utcnow()

    # ... (您原有的 while 迴圈邏輯，完全不變) ...
    day_map: Dict[int, Dict[str, Any]] = {}
    current = head
    prev_node = None
    while current:
        node_id = str(uuid.uuid4())[:8]
        node_data = {
            "_id": node_id,
            "name": ", ".join([p["name"] for p in current.places]) if getattr(current, "places", None) else "(空行程)",
            "start_time": getattr(current, "start_time", None),
            "end_time": getattr(current, "end_time", None),
            "transport": "",
            "note": "",
            "weather_checked": False,
            "alternative_recommended": False,
            "replaced_by": None,
            "next_id": None
        }
        day = getattr(current, "day", None)
        if day not in day_map:
            day_map[day] = {
                "day": day,
                "head": node_id,
                "attractions": []
            }
        if prev_node:
            prev_node["next_id"] = node_id
        day_map[day]["attractions"].append(node_data)
        prev_node = node_data
        current = getattr(current, "next", None)

    # ===== ✅ NEW：建立包含所有欄位的 final_doc =====
    final_doc = {
        # --- 基本資料 ---
        "user_id": user_id,
        "form_type": form_type,
        "trip_preference_id": trip_preference_id,
        "created_at": created_at,
        
        # --- 原始表單資料 (平鋪展開) ---
        **(form_data or {}),  # <--- 關鍵！這會把 title, locations, start_date 等加進來
        
        # --- AI 生成內容 ---
        "summary": summary,
        "html": html,
        "nodes": nodes or [], # "nodes" 欄位 (如截圖所示)
        
        # --- 結構化行程 ---
        "days": list(day_map.values()),
        
        # --- 我們新增的欄位 ---
        "members": [user_id]
    }

    return final_doc

# ================== ⬇️ 這裡是修改後的第二個函式 ⬇️ ==================
def save_structured_linked_itinerary(
    user_id, head, form_type="personal", trip_preference_id=None, created_at=None,
    # ===== ✅ NEW：新增參數以接收您要的額外欄位 =====
    form_data: Dict[str, Any] = None,
    summary: str = "",
    html: str = "",
    nodes: List[Dict[str, Any]] = None
):
    """
    【修改版】
    儲存結構化的行程，並建立/關聯一個對應的聊天室。
    現在會將 form_data, summary, html, nodes 都傳遞下去儲存。
    """
    
    # 1. 準備行程文件
    doc_created_at = created_at or datetime.utcnow()
    doc = itinerary_linkedlist_to_day_structure(
        user_id=user_id,
        head=head,
        form_type=form_type,
        trip_preference_id=trip_preference_id,
        created_at=doc_created_at,
        
        # ===== ✅ NEW：將所有資料傳遞給 helper 函式 =====
        form_data=form_data,
        summary=summary,
        html=html,
        nodes=nodes
    )
    
    # 2. 插入行程 (使用您圖片中的 "structured_itineraries")
    itinerary_result = db["structured_itineraries"].insert_one(doc)
    
    # 3. 取得 trip_id
    trip_id = itinerary_result.inserted_id

    # 4. 建立 chatroom 文件
    chat_doc = {
        "trip_id": trip_id,
        "created_at": doc_created_at,
        "messages": [],
        "members": [user_id]
    }
    chat_result = chatroom_collection.insert_one(chat_doc)
    
    # 5. 取得 chat_id
    chat_id = chat_result.inserted_id

    # 6. 將 chat_id 寫回 "structured_itineraries" 文件中
    db["structured_itineraries"].update_one(
        {"_id": trip_id},
        {"$set": {"chat_id": chat_id}}
    )

    # 7. 回傳 trip_id
    return trip_id

# ====================== (以下函式皆維持原樣) =====================

def get_user_favorites(user_id: str) -> List[Dict[str, Any]]:
    return list(db["user_favorite"].find(
        {"user_id": user_id},
        {"_id": 0, "place_id": 1, "place_name": 1, "city": 1, "tags": 1, "type": 1}
    ))

def get_user_browse_summary(user_id: str, lookback_days: int = 180) -> Dict[str, Any]:
    since = datetime.utcnow() - timedelta(days=lookback_days)
    cursor = db["user_browse"].find(
        {"user_id": user_id, "created_at": {"$gte": since}},
        {"_id": 0, "place_id": 1, "place_name": 1, "city": 1, "tags": 1, "type": 1}
    )
    places: List[Dict[str, Any]] = []
    cities = Counter()
    tags = Counter()
    types = Counter()
    names = Counter()
    for doc in cursor:
        places.append(doc)
        if doc.get("city"): cities[doc["city"]] += 1
        if doc.get("tags"):
            for t in doc["tags"]: tags[t] += 1
        if doc.get("type"): types[doc["type"]] += 1
        if doc.get("place_name"): names[doc["place_name"]] += 1
    return {
        "raw": places, "cities": cities, "tags": tags, "types": types, "names": names
    }

def build_behavior_profile(favs: List[Dict[str, Any]], browse_sum: Dict[str, Any]) -> Dict[str, Any]:
    fav_cities = Counter([f.get("city") for f in favs if f.get("city")])
    fav_tags   = Counter([t for f in favs for t in (f.get("tags") or [])])
    fav_types  = Counter([f.get("type") for f in favs if f.get("type")])
    fav_names  = Counter([f.get("place_name") for f in favs if f.get("place_name")])

    def _merge_counter(c1: Counter, c2: Counter, w1: float = 2.0, w2: float = 1.0) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for k, v in c1.items(): out[k] += w1 * v
        for k, v in c2.items(): out[k] += w2 * v
        return dict(out)

    cities_score = _merge_counter(fav_cities, browse_sum.get("cities", Counter()))
    tags_score   = _merge_counter(fav_tags,   browse_sum.get("tags", Counter()))
    types_score  = _merge_counter(fav_types,  browse_sum.get("types", Counter()))
    names_score  = _merge_counter(fav_names,  browse_sum.get("names", Counter()))

    def _normalize(d: Dict[str, float]) -> Dict[str, float]:
        if not d: return {}
        m = max(d.values())
        if m <= 0: return {k: 0.0 for k in d}
        return {k: round(v / m, 4) for k, v in d.items()}

    return {
        "cities_pref": _normalize(cities_score),
        "tags_pref":   _normalize(tags_score),
        "types_pref":  _normalize(types_score),
        "names_pref":  _normalize(names_score),
        "favs": favs
    }

from pymongo.errors import ServerSelectionTimeoutError

def test_connection():
    try:
        mongo_client.admin.command("ping")
        print("✅ MongoDB 連線成功")
        dbs = mongo_client.list_database_names()
        print("📂 資料庫清單:", dbs)
        cols = db.list_collection_names()
        print(f"📑 tripDemo-shan 集合:", cols)
    except ServerSelectionTimeoutError as e:
        print("❌ MongoDB 連線失敗 (伺服器選擇逾時):", e)
    except Exception as e:
        print("❌ 測試過程發生錯誤:", e)