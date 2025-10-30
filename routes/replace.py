# routes/replace.py (已修正)
from fastapi import APIRouter, Request, HTTPException, Path # 👈 1. 新增 Path
# 🔽🔽 2. 移除 ReplacePayload, 改用 schemas 裡的 ReplaceActivityRequest 🔽🔽
# from pydantic import BaseModel, model_validator # 移除舊的
from models.schemas import ReplaceActivityRequest # 👈 假設它在 schemas.py
# 🔼🔼
from typing import Any, Dict, Optional, Tuple, List
from bson import ObjectId
from pymongo.errors import ConfigurationError, ServerSelectionTimeoutError
from datetime import datetime, timezone
from urllib.parse import quote
import re
# 🔽🔽 3. Import _format_trip_for_kotlin (假設在 recommend.py) 🔽🔽
try:
    from routes.recommend import _format_trip_for_kotlin
except ImportError:
    # 如果 _format_trip_for_kotlin 不在 recommend.py 或無法匯入，
    # 你需要將該函式複製到這個檔案或一個共享的 utils 檔案中
    print("⚠️ 警告：無法從 routes.recommend 匯入 _format_trip_for_kotlin。請確保該函式可用。")
    # 定義一個假的函式以避免 NameError，但實際執行會失敗
    def _format_trip_for_kotlin(doc: Dict[str, Any]) -> Dict[str, Any]:
        print("錯誤：_format_trip_for_kotlin 未正確載入！")
        return doc # 只回傳原始文件，App 端可能會解析失敗
# 🔼🔼

router = APIRouter()

# ---- 移除 ReplacePayload 模型 ----
# class ReplacePayload(BaseModel): ...

# ---- 小工具 (_as_object_id, _norm_name, _build_map_url, _apply_new_spot 保持不變) ----
def _as_object_id(v: str) -> Optional[ObjectId]:
    try:
        return ObjectId(v)
    except Exception:
        return None

def _norm_name(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip()
    t = t.replace("臺", "台").lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t

# _parse_node_and_index_from_original_id 不再需要，可以移除或註解

def _build_map_url(name: str, place_id: Optional[str]) -> Optional[str]:
    if not name and not place_id:
        return None
    if place_id:
        # 修正 URL 格式 (移除 googleusercontent.com, 使用標準 Google Maps 連結)
        return f"https://www.google.com/maps/search/?api=1&query={quote(name or '')}&query_place_id={quote(place_id)}"
    # 修正 URL 格式
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}"


def _apply_new_spot(old_place: Dict[str, Any], new_spot_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    在「保留時段/類別/停留時間」的前提下，覆蓋可替換欄位。
    現在 new_spot_data 是來自 Alternative 模型的字典 (已處理 alias)。
    """
    out = dict(old_place)
    category_keep = old_place.get("category", "attraction")
    stay_keep = old_place.get("stay_minutes")

    # 直接從 new_spot_data 取值 (已是 snake_case)
    name = new_spot_data.get("name") or old_place.get("name")
    place_id = new_spot_data.get("place_id") or old_place.get("place_id") # new_spot_data 裡是 place_id
    address = new_spot_data.get("address") or old_place.get("address")
    rating = new_spot_data.get("rating", old_place.get("rating")) # 保留 None 的可能性
    lat = new_spot_data.get("lat") or old_place.get("lat")
    lng = new_spot_data.get("lng") or old_place.get("lng")
    open_text = new_spot_data.get("open_status_text") or old_place.get("open_text") # 來自 Alternative
    types = old_place.get("types", []) # Alternative 模型裡沒有 types，保留舊的

    out.update({
        "name": name,
        "place_id": place_id,
        "address": address,
        "rating": rating,
        "reviews": new_spot_data.get("user_ratings_total", old_place.get("reviews")), # 更新 reviews
        "lat": lat,
        "lng": lng,
        "open_text": open_text,
        "types": types, # 保留舊 types
        "map_url": _build_map_url(name, place_id),
        "source": "replace_api",
        "replaced_at": datetime.now(timezone.utc).isoformat(),
        "replaced_from": old_place.get("name"),
        # 顯式移除可能來自 Alternative 但 node 不需要/格式不同的欄位
        "userRatingsTotal": None,
        "openStatusText": None,
        "photoUrl": None,
    })
    # 移除值為 None 的 key
    out = {k:v for k,v in out.items() if v is not None}


    # 保留或補回關鍵欄位
    out["category"] = category_keep or out.get("category") or "attraction"
    if stay_keep is not None:
        out["stay_minutes"] = stay_keep
    # 確保 place_name 也更新 (若存在)
    if "place_name" in out:
        out["place_name"] = name

    return out

# _locate_target 不再需要，邏輯已移入主路由

# ---- 主路由 ----
# 🔽🔽 4. 修改路徑、參數、輸入模型，並改為 async def 🔽🔽
@router.post("/trips/{trip_id}/replace")
async def replace_attraction(
    payload: ReplaceActivityRequest,                   # 👈 payload 先 (無預設值)
    request: Request,                                  # 👈 request 再來 (無預設值)
    trip_id: str = Path(..., description="行程的 ID") # 👈 trip_id 最後 (有 Path)
):
    """
    在最新的 structured_itineraries 文件中，將某個 node 的某個 place「就地替換」。
    - trip_id：(來自路徑) structured_itineraries._id
    - (來自 Body) old_activity_id：要被替換的景點 ID (place_id)
    - (來自 Body) new_activity_data：推薦回來的新景點資訊 (Alternative 模型)

    回傳：更新後的完整 Trip 物件 (符合 Kotlin 格式)
    """
    db = request.app.state.db
    if not hasattr(db, "structured_itineraries"):
         raise HTTPException(status_code=500, detail="Database collection 'structured_itineraries' not available.")
    itins_col = db["structured_itineraries"] # 在 async 函式內取得 collection

    # 找到 structured_itineraries 文件
    try:
        si_doc = None
        oid = _as_object_id(trip_id) # 👈 使用路徑參數 trip_id
        if oid:
            # 🔽🔽 5. 使用 await 🔽🔽
            si_doc = await itins_col.find_one({"_id": oid})
        if not si_doc:
             # 🔽🔽 5. 使用 await 🔽🔽
            si_doc = await itins_col.find_one({"_id": trip_id}) # 👈 使用路徑參數 trip_id
    except (ConfigurationError, ServerSelectionTimeoutError) as e:
        raise HTTPException(status_code=503, detail=f"MongoDB 連線/解析失敗：{e}")
    except Exception as e:
        # 捕捉其他可能的資料庫錯誤
        print(f"Error finding trip document: {e}")
        raise HTTPException(status_code=500, detail="讀取行程資料時發生錯誤")

    if not si_doc:
        raise HTTPException(status_code=404, detail="找不到對應的 structured_itineraries（請確認 trip_id）")

    # 定位要替換的 node / place (使用 payload.old_activity_id)
    node_idx, place_idx = None, None
    nodes = si_doc.get("nodes") or []
    found = False
    for ni, node in enumerate(nodes):
        places = node.get("places") or []
        for pi, p in enumerate(places):
            # 檢查 "place_id" 或 "id" 欄位
            current_pid = str(p.get("place_id") or p.get("id") or "")
            if current_pid == payload.old_activity_id:
                 node_idx, place_idx = ni, pi
                 found = True
                 break
        if found:
             break

    if node_idx is None or place_idx is None:
        # 如果用 ID 找不到，可以選擇性地嘗試用 name 匹配 (作為備援)
        target_name_norm = _norm_name(payload.old_activity_id) # 假設 old_id 可能傳了 name
        if target_name_norm:
              for ni, node in enumerate(nodes):
                  places = node.get("places") or []
                  for pi, p in enumerate(places):
                      if _norm_name(p.get("name")) == target_name_norm:
                           node_idx, place_idx = ni, pi
                           found = True
                           break
                  if found:
                       break

    if not found:
        raise HTTPException(status_code=404, detail=f"找不到要取代的景點 (old_activity_id: {payload.old_activity_id})")

    try:
        target_node = nodes[node_idx]
        places = target_node.get("places") or []
        old_place = places[place_idx]
    except (IndexError, TypeError):
        # 防禦性程式碼，以防 node_idx/place_idx 計算錯誤
        raise HTTPException(status_code=500, detail="定位舊景點時發生內部索引錯誤")

    # 準備新 place (使用 payload.new_activity_data)
    try:
        # 將 Pydantic 模型轉為字典，並使用 alias (例如 placeId -> place_id)
        new_place_dict = payload.new_activity_data.model_dump(by_alias=True)
        new_place = _apply_new_spot(old_place, new_place_dict)
        places[place_idx] = new_place  # 就地替換
    except Exception as e:
        print(f"Error applying new spot data: {e}")
        raise HTTPException(status_code=500, detail="處理新景點資料時發生錯誤")


    # 更新 used_places：移除舊名稱、加入新名稱
    old_name = str(old_place.get("name") or "").strip()
    new_name = str(new_place.get("name") or "").strip()
    used_places = list(si_doc.get("used_places") or [])
    temp_used_places = set(used_places) # 使用 set 以方便操作
    if old_name:
        temp_used_places.discard(old_name)
    if new_name:
        temp_used_places.add(new_name)
    final_used_places = sorted(list(temp_used_places)) # 轉換回排序列表


    # 寫回 DB
    try:
        # 🔽🔽 6. 使用 await 🔽🔽
        res = await itins_col.update_one(
            {"_id": si_doc["_id"]},
            {"$set": {"nodes": nodes, "used_places": final_used_places}}
        )
    except Exception as e:
        print(f"Error updating database: {e}")
        raise HTTPException(status_code=500, detail="更新行程資料庫時發生錯誤")


    if res.matched_count == 0: # 應該不可能發生，因為前面 find_one 成功了
        raise HTTPException(status_code=404, detail="更新失敗：找不到原始文件")
    # modified_count 可能是 0（內容相同），仍視為成功

    # 🔽🔽 7. 修改回傳格式：讀取更新後的資料並格式化 🔽🔽
    try:
        updated_doc = await itins_col.find_one({"_id": si_doc["_id"]})
        if not updated_doc:
             raise HTTPException(status_code=500, detail="更新後讀取行程失敗")

        # 確保 _format_trip_for_kotlin 可用
        if "_format_trip_for_kotlin" not in globals():
             raise HTTPException(status_code=500, detail="內部錯誤：格式化函式遺失")

        return _format_trip_for_kotlin(updated_doc)
    except Exception as e:
        print(f"Error fetching/formatting updated trip: {e}")
        raise HTTPException(status_code=500, detail="讀取或格式化更新後行程時發生錯誤")
    # 🔼🔼