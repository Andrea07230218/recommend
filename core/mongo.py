from pymongo import MongoClient
from datetime import datetime, timedelta  # ✅ 擴充：支援 lookback_days
from markdown2 import markdown as md
import uuid
from bson import ObjectId
from collections import Counter, defaultdict  # ✅ 新增
from typing import Dict, Any, List            # ✅ 新增

# ✅ 連線到本地 MongoDB / Atlas
mongo_client = MongoClient("mongodb+srv://anne:1218@cluster0.g54wj9s.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")

# ✅ 指定資料庫與集合
db = mongo_client["tripDemo-shan"]
users_collection = db["users"]
form_collection = db["forms"]             # ✅ 新增集合（沿用你的既有命名）
itineraries_collection = db["itineraries"]

# === 可選：建立常用索引，第一次部署或上線前呼叫一次即可 =========================
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


# ============================== 既有功能 =====================================

# ✅ 查詢使用者
def get_user(username: str):
    return users_collection.find_one({'username': username})

# ✅ 儲存個人或團體填寫的表單
def save_form(user_id, form_data, form_type="personal", created_at=None):
    if not created_at:
        created_at = datetime.utcnow()

    # 優先用前端帶來的 transportation，否則 derive
    if "transportation" not in form_data:
        form_data["transportation"] = derive_transportation(form_data)
    else:
        # 正規化一下前端傳來的值
        form_data["transportation"] = derive_transportation(form_data)

    doc = {
        "user_id": user_id,
        "form_type": form_type,
        **form_data,             # 平鋪，不多包一層 form
        "created_at": created_at
    }
    db["forms"].insert_one(doc)
    return created_at



def derive_transportation(form_data: Dict[str, Any]) -> str:
    """
    統一輸出 'drive' 或 'public'
    - 優先看 form_data["transportation"]
    - 沒有就判斷中文「交通方式」
    """
    # ① 先看已正規化的 transportation
    v = str(form_data.get("transportation", "")).strip().lower()
    if v in {"drive", "driving", "car"}:
        return "drive"
    if v in {"public", "transit", "bus", "metro", "train"}:
        return "public"

    # ② 再看中文欄位
    text = str(form_data.get("交通方式", "")).strip().lower()
    if any(k in text for k in ["汽車", "開車", "自駕", "車", "driving", "car", "drive"]):
        return "drive"
    if any(k in text for k in ["大眾", "捷運", "公車", "地鐵", "火車", "客運", "transit", "public", "bus", "metro", "train"]):
        return "public"

    # ③ fallback
    return "public"


def save_itinerary(user_id, itinerary, form_type="personal", created_at=None):
    if not created_at:
        created_at = datetime.utcnow()  # fallback（理論上應該永遠用外部傳入的）
    
    doc = {
        "user_id": user_id,
        "form_type": form_type,
        "created_at": created_at,
        "itinerary": itinerary
    }
    db["itineraries"].insert_one(doc)

def itinerary_linkedlist_to_day_structure(user_id, head, form_type="personal", trip_preference_id=None, created_at=None):
    if not created_at:
        created_at = datetime.utcnow()

    # 用來儲存每天的行程 linked list 結構
    day_map: Dict[int, Dict[str, Any]] = {}

    current = head
    prev_node = None

    while current:
        node_id = str(uuid.uuid4())[:8]  # 簡化 UUID 當作 _id
        node_data = {
            "_id": node_id,
            "name": ", ".join([p["name"] for p in current.places]) if getattr(current, "places", None) else "(空行程)",
            "start_time": getattr(current, "start_time", None),
            "end_time": getattr(current, "end_time", None),
            "transport": "",  # ⬅️ 可填交通方式，暫時留空
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

    # 回傳轉換後結構（整份行程文件）
    final_doc = {
        "user_id": user_id,
        "form_type": form_type,
        "trip_preference_id": trip_preference_id,
        "created_at": created_at,
        "days": list(day_map.values())
    }

    return final_doc

def save_structured_linked_itinerary(user_id, head, form_type="personal", trip_preference_id=None, created_at=None):
    doc = itinerary_linkedlist_to_day_structure(
        user_id=user_id,
        head=head,
        form_type=form_type,
        trip_preference_id=trip_preference_id,
        created_at=created_at
    )
    db["linked_itineraries"].insert_one(doc)


# ====================== 新增：行為資料讀取與彙整（資料層） =====================

def get_user_favorites(user_id: str) -> List[Dict[str, Any]]:
    """
    讀取使用者收藏清單（user_favorite），回傳精簡欄位列表。
    預期欄位：user_id, place_id?, place_name, city, type(restaurant/attraction/store…), tags(list)
    """
    return list(db["user_favorite"].find(
        {"user_id": user_id},
        {"_id": 0, "place_id": 1, "place_name": 1, "city": 1, "tags": 1, "type": 1}
    ))

def get_user_browse_summary(user_id: str, lookback_days: int = 180) -> Dict[str, Any]:
    """
    彙整使用者近 N 天的瀏覽紀錄（user_browse），回傳：
      - raw：原始精簡紀錄列表
      - cities/tags/types/names：Counter 統計
    預期欄位：user_id, place_name, city, type, tags(list), created_at(Date)
    若缺少 created_at，可移除時間過濾。
    """
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
        if doc.get("city"):
            cities[doc["city"]] += 1
        if doc.get("tags"):
            for t in doc["tags"]:
                tags[t] += 1
        if doc.get("type"):
            types[doc["type"]] += 1
        if doc.get("place_name"):
            names[doc["place_name"]] += 1

    return {
        "raw": places,
        "cities": cities,
        "tags": tags,
        "types": types,
        "names": names
    }

def build_behavior_profile(favs: List[Dict[str, Any]], browse_sum: Dict[str, Any]) -> Dict[str, Any]:
    """
    將收藏與瀏覽統計合併為可供排序加權的使用者行為輪廓（已正規化 0~1）。
    預設權重：收藏 2.0、瀏覽 1.0（可於此調整）
    回傳：
      - cities_pref / tags_pref / types_pref / names_pref：各自的 {key: 0~1 分數}
      - favs：原始收藏列表（供前端或其他節點需要時使用）
    """
    fav_cities = Counter([f.get("city") for f in favs if f.get("city")])
    fav_tags   = Counter([t for f in favs for t in (f.get("tags") or [])])
    fav_types  = Counter([f.get("type") for f in favs if f.get("type")])
    fav_names  = Counter([f.get("place_name") for f in favs if f.get("place_name")])

    def _merge_counter(c1: Counter, c2: Counter, w1: float = 2.0, w2: float = 1.0) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for k, v in c1.items():
            out[k] += w1 * v
        for k, v in c2.items():
            out[k] += w2 * v
        return dict(out)

    cities_score = _merge_counter(fav_cities, browse_sum.get("cities", Counter()))
    tags_score   = _merge_counter(fav_tags,   browse_sum.get("tags", Counter()))
    types_score  = _merge_counter(fav_types,  browse_sum.get("types", Counter()))
    names_score  = _merge_counter(fav_names,  browse_sum.get("names", Counter()))

    def _normalize(d: Dict[str, float]) -> Dict[str, float]:
        if not d:
            return {}
        m = max(d.values())
        if m <= 0:
            return {k: 0.0 for k in d}
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
    """
    測試 MongoDB 連線狀態。
    - 成功會印出 ✅
    - 失敗會印出 ❌ 與錯誤原因
    """
    try:
        # 1. ping 測試
        mongo_client.admin.command("ping")
        print("✅ MongoDB 連線成功")

        # 2. 列出資料庫
        dbs = mongo_client.list_database_names()
        print("📂 資料庫清單:", dbs)

        # 3. 列出 tripDemo-shan 裡的集合
        cols = db.list_collection_names()
        print(f"📑 tripDemo-shan 集合:", cols)

    except ServerSelectionTimeoutError as e:
        print("❌ MongoDB 連線失敗 (伺服器選擇逾時):", e)
    except Exception as e:
        print("❌ 測試過程發生錯誤:", e)
