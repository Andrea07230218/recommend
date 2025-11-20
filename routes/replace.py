# 檔案： routes/replace.py
from fastapi import APIRouter, HTTPException, Request, Path
from models.schemas import ReplaceActivityRequest
from bson import ObjectId
from core import mongo
import logging
import traceback

# 從共用檔案匯入 formatter
from core.formatters import format_trip_for_kotlin

router = APIRouter()

# 假設 db 是 同步 PyMongo client
try:
    db = mongo.db
    itins_col = db["structured_itineraries"]
except Exception as e:
    print(f"⚠️ 警告：無法載入 replace.py 所需的 MongoDB collections: {e}")
    itins_col = None

# 專案所需的欄位
TRIP_PROJECTION = {
    "_id": 1, "user_id": 1, "created_at": 1, "trip_preference_id": 1,
    "title": 1, "meta": 1, "locations": 1, "start_date": 1, "end_date": 1,
    "activity_start": 1, "activity_end": 1, "avg_age": 1, "transportation": 1,
    "use_gmaps_rating": 1, "preferences": 1, "visibility": 1, "total_budget": 1,
    "days": 1, "nodes": 1, "cover_photo_url": 1, "members": 1, "chat_id": 1
}


@router.post(
    "/trips/{trip_id}/replace",
    summary="更換行程中的一個景點",
    description="將行程中的一個舊景點替換為一個新的景點"
)
def replace_activity_in_trip(
    req: ReplaceActivityRequest, 
    request: Request,            
    trip_id: str = Path(..., description="行程的 ID")
):
    print(f"\n--- [SYNC] replace_activity_in_trip endpoint called for trip: {trip_id} ---")
    
    if itins_col is None:
        raise HTTPException(status_code=500, detail="itineraries collection 未載入")

    try:
        oid = ObjectId(trip_id)
        trip_doc = itins_col.find_one({"_id": oid}, TRIP_PROJECTION)
        
        if not trip_doc:
            print(f"--- Trip not found: {trip_id} ---")
            raise HTTPException(status_code=404, detail=f"找不到行程 ID: {trip_id}")

        new_data = req.new_activity_data
        
        # 準備新節點資料
        new_node_data = {
            "place_id": new_data.place_id,
            "place_name": new_data.name,
            "name": new_data.name,
            "address": new_data.address,
            "rating": new_data.rating,
            "reviews": new_data.user_ratings_total,
            "lat": new_data.lat,
            "lng": new_data.lng,
            "open_text": new_data.open_status_text,
            "map_url": f"http://googleusercontent.com/maps/google.com/90{new_data.place_id}",
            
            # 🔽🔽 ✅ 修正：使用 Pydantic 的屬性名稱 (snake_case) 🔽🔽
            "photoUrl": new_data.photo_url, 
            "photo_url": new_data.photo_url 
            # 🔼🔼
        }
        new_node_data = {k: v for k, v in new_node_data.items() if v is not None}

        updated_nodes = []
        found = False

        # 遍歷所有節點 (Nodes)
        for node in trip_doc.get("nodes", []):
            try:
                places_list = node.get("places", [])
                if not places_list:
                    updated_nodes.append(node)
                    continue
                
                new_places_list = []
                node_modified = False
                
                for place in places_list:
                    current_place_pid = place.get("place_id") or place.get("id")
                    
                    if current_place_pid == req.old_activity_id:
                        found = True
                        print(f"--- Found old activity: {place.get('name')} (ID: {current_place_pid}). Replacing... ---")
                        
                        # 複製舊資料並更新
                        updated_place = place.copy()
                        updated_place.update(new_node_data)
                        new_places_list.append(updated_place)
                        node_modified = True
                    else:
                        new_places_list.append(place)
                
                if node_modified:
                    updated_node = node.copy()
                    updated_node["places"] = new_places_list
                    updated_nodes.append(updated_node)
                else:
                    updated_nodes.append(node)

            except (IndexError, TypeError, Exception) as e:
                print(f"--- Error processing node {node.get('node_id')}, skipping: {e} ---")
                updated_nodes.append(node)
        
        if not found:
            print(f"--- Old activity ID not found in nodes: {req.old_activity_id} ---")
            raise HTTPException(status_code=404, detail=f"在行程中找不到舊景點 ID: {req.old_activity_id}")

        print(f"--- Updating database for trip {trip_id} with new nodes... ---")
        
        update_result = itins_col.update_one(
            {"_id": oid},
            {"$set": {"nodes": updated_nodes}}
        )

        if update_result.matched_count == 0:
            raise HTTPException(status_code=404, detail="更新失敗：找不到原始文件")
            
        print(f"--- Update complete. Fetching updated document... ---")
        updated_doc = itins_col.find_one({"_id": oid}, TRIP_PROJECTION)
        
        if not updated_doc:
             raise HTTPException(status_code=500, detail="更新後讀取行程失敗")
             
        return format_trip_for_kotlin(updated_doc)

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"--- Error in replace_activity_in_trip for ID {trip_id} ---")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"更換景點時發生錯誤: {str(e)}")