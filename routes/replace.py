# routers/replace.py
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, model_validator
from typing import Any, Dict, Optional, Tuple, List
from bson import ObjectId
from pymongo.errors import ConfigurationError, ServerSelectionTimeoutError
from datetime import datetime, timezone
from urllib.parse import quote
import re

router = APIRouter()

# ---- 入參模型 ----
class ReplacePayload(BaseModel):
    trip_id: str                 # structured_itineraries._id
    original_id: str             # 例如 "nodeId:0" or "nodeId|1" or 純 nodeId / place_id / place 名稱
    new_spot: Dict[str, Any]     # 從推薦回來的 spot 物件

    @model_validator(mode="after")
    def _check(self):
        if not self.trip_id:
            raise ValueError("trip_id 不可為空")
        if not self.original_id:
            raise ValueError("original_id 不可為空")
        if not isinstance(self.new_spot, dict) or not self.new_spot.get("name"):
            raise ValueError("new_spot 至少需要 name 欄位")
        return self

# ---- 小工具 ----
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

def _parse_node_and_index_from_original_id(raw: str) -> Tuple[Optional[str], Optional[int]]:
    """
    支援多種 original_id 格式：
      1) {node_id}:{idx}
      2) {node_id}::{idx}
      3) {node_id}|{idx}
      4) {node_id}#idx
      5) 純 {node_id}（不含 idx，回 (node_id, None)）
    若無法解析，回 (None, None)
    """
    s = str(raw)
    m = re.match(r"^(.+?)(?::|::|\||#)(\d+)$", s)
    if m:
        node_id, idx_s = m.group(1), m.group(2)
        try:
            return node_id, int(idx_s)
        except Exception:
            return node_id, None

    # 僅 node_id（粗略用 8+ 位 16 進位/字元視為 id）
    if re.match(r"^[0-9a-f\-]{8,}$", s, re.IGNORECASE):
        return s, None
    return None, None

def _build_map_url(name: str, place_id: Optional[str]) -> Optional[str]:
    if not name and not place_id:
        return None
    if place_id:
        return f"https://www.google.com/maps/search/?api=1&query={quote(name or '')}&query_place_id={quote(place_id)}"
    return f"https://www.google.com/maps/search/?api=1&query={quote(name)}"

def _apply_new_spot(old_place: Dict[str, Any], new_spot: Dict[str, Any]) -> Dict[str, Any]:
    """
    在「保留時段/類別/停留時間」的前提下，覆蓋可替換欄位。
    """
    out = dict(old_place)  # 先複製舊的，保留未知欄位
    # 盡量保留 category / stay_minutes 等
    category_keep = old_place.get("category", "attraction")
    stay_keep = old_place.get("stay_minutes")

    name = new_spot.get("name") or old_place.get("name")
    place_id = new_spot.get("place_id") or new_spot.get("placeId") or old_place.get("place_id")
    address = new_spot.get("address") or new_spot.get("formatted_address") or old_place.get("address")
    rating = new_spot.get("rating", old_place.get("rating"))
    lat = new_spot.get("lat") or new_spot.get("latitude") or old_place.get("lat")
    lng = new_spot.get("lon") or new_spot.get("lng") or new_spot.get("longitude") or old_place.get("lng")
    open_text = new_spot.get("opening_hours_text") or old_place.get("open_text")
    types = new_spot.get("types") or old_place.get("types") or []

    out.update({
        "name": name,
        "place_id": place_id,
        "address": address,
        "rating": rating,
        "lat": lat,
        "lng": lng,
        "open_text": open_text,
        "types": types,
        "map_url": _build_map_url(name, place_id),
        "source": "replace_api",
        "replaced_at": datetime.now(timezone.utc).isoformat(),
        "replaced_from": old_place.get("name"),
    })

    # 保留或補回關鍵欄位
    out["category"] = category_keep or out.get("category") or "attraction"
    if stay_keep is not None:
        out["stay_minutes"] = stay_keep

    return out

async def _locate_target(si_doc: Dict[str, Any], original_id: str) -> Tuple[Optional[int], Optional[int]]:
    """
    回傳 (node_index, place_index)。
    支援：
      - 解析出 (node_id, idx)
      - 精準 node_id（若 places 只有一個）
      - 以 place_id 精準命中
      - 以 place.name（正規化）命中
    """
    node_id, idx = _parse_node_and_index_from_original_id(original_id)
    nodes: List[Dict[str, Any]] = si_doc.get("nodes") or []

    # 1) 若帶 node_id 與 idx
    if node_id and idx is not None:
        for ni, node in enumerate(nodes):
            if str(node.get("node_id")) == str(node_id):
                places = node.get("places") or []
                # 容錯：允許 1-based 的 idx
                if 0 <= idx < len(places):
                    return ni, idx
                if 1 <= idx <= len(places):
                    return ni, idx - 1
                raise HTTPException(
                    status_code=400,
                    detail=f"指定的索引超出範圍：node_id={node_id}, idx={idx}, places={len(places)}"
                )

    # 2) 只有 node_id：places=1 才能安全定位
    if node_id and idx is None:
        for ni, node in enumerate(nodes):
            if str(node.get("node_id")) == str(node_id):
                places = node.get("places") or []
                if len(places) == 1:
                    return ni, 0
                raise HTTPException(
                    status_code=400,
                    detail=f"node {node_id} 有 {len(places)} 個地點，請提供索引（例如 '{node_id}:0'）"
                )

    # 3) 嘗試以 place_id 命中
    for ni, node in enumerate(nodes):
        for pi, p in enumerate(node.get("places") or []):
            if str(p.get("place_id")) == str(original_id):
                return ni, pi

    # 4) 嘗試以名稱（正規化）命中
    target_norm = _norm_name(original_id)
    if target_norm:
        for ni, node in enumerate(nodes):
            for pi, p in enumerate(node.get("places") or []):
                if _norm_name(p.get("name")) == target_norm:
                    return ni, pi

    return None, None

# ---- 主路由 ----
@router.post("/replace_attraction")
async def replace_attraction(request: Request, payload: ReplacePayload):
    """
    在最新的 structured_itineraries 文件中，將某個 node 的某個 place「就地替換」。
    - trip_id：structured_itineraries._id
    - original_id：建議使用「{node_id}:{place_index}」；若 node 只有 1 筆 place，可只給 node_id
                    也支援直接給 place_id 或 place 名稱（會嘗試比對）
    - new_spot：推薦回來的新景點資訊（至少要有 name；若有 place_id / lat / lng 更佳）
    """
    db = request.app.state.db

    # 找到 structured_itineraries 文件
    try:
        si_doc = None
        oid = _as_object_id(payload.trip_id)
        if oid:
            si_doc = await db["structured_itineraries"].find_one({"_id": oid})
        if not si_doc:
            si_doc = await db["structured_itineraries"].find_one({"_id": payload.trip_id})
    except (ConfigurationError, ServerSelectionTimeoutError) as e:
        raise HTTPException(status_code=503, detail=f"MongoDB 連線/解析失敗：{e}")

    if not si_doc:
        raise HTTPException(status_code=404, detail="找不到對應的 structured_itineraries（請確認 trip_id）")

    # 定位要替換的 node / place
    node_idx, place_idx = await _locate_target(si_doc, payload.original_id)
    if node_idx is None or place_idx is None:
        raise HTTPException(status_code=404, detail="找不到要取代的景點（original_id 無法定位到 node/place）")

    nodes = si_doc.get("nodes") or []
    try:
        target_node = nodes[node_idx]
        places = target_node.get("places") or []
        old_place = places[place_idx]
    except Exception:
        raise HTTPException(status_code=400, detail="original_id 指向的 node/place 索引無效")

    # 準備新 place（保留關鍵欄位，覆蓋可替換欄位）
    new_place = _apply_new_spot(old_place, payload.new_spot)
    places[place_idx] = new_place  # 就地替換

    # 更新 used_places：移除舊名稱、加入新名稱（避免重複）
    old_name = str(old_place.get("name") or "").strip()
    new_name = str(new_place.get("name") or "").strip()
    used_places = list(si_doc.get("used_places") or [])
    if old_name and old_name in used_places:
        try:
            used_places.remove(old_name)
        except ValueError:
            pass
    if new_name and new_name not in used_places:
        used_places.append(new_name)

    # 寫回 DB（整個 nodes 與 used_places）
    res = await db["structured_itineraries"].update_one(
        {"_id": si_doc["_id"]},
        {"$set": {"nodes": nodes, "used_places": used_places}}
    )

    if res.matched_count != 1:
        raise HTTPException(status_code=500, detail="更新失敗：找不到文件或版本衝突")
    # modified_count 可能是 0（內容相同），仍視為成功
    return {
        "success": True,
        "node_id": target_node.get("node_id"),
        "place_index": place_idx
    }
