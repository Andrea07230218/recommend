# routers/trip.py
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from bson import ObjectId
from typing import Optional, Any, Dict, List
from datetime import datetime

router = APIRouter()

# ---------- 小工具 ----------

def _to_str(v: Any) -> Any:
    """ObjectId 轉字串，其餘原樣回傳。"""
    return str(v) if isinstance(v, ObjectId) else v

def _normalize_created_at(value: Any) -> Optional[datetime]:
    """將各種可能型別的 created_at 轉成 datetime，失敗回 None。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            # 毫秒或秒
            if value > 10_000_000_000:
                return datetime.utcfromtimestamp(value / 1000.0)
            return datetime.utcfromtimestamp(value)
        except Exception:
            return None
    if isinstance(value, dict) and "$date" in value:
        try:
            raw = value["$date"]
            ms = int(raw.get("$numberLong") if isinstance(raw, dict) and "$numberLong" in raw else raw)
            return datetime.utcfromtimestamp(ms / 1000.0)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            # 兼容 ISO 字串與 Z 結尾
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

def _build_trip_name(doc: Dict[str, Any]) -> str:
    """決定行程列表顯示名稱：title/自動由 created_at 生成/未命名。"""
    name = doc.get("title") or doc.get("trip_name") or doc.get("name")
    if name:
        return str(name)
    dt = _normalize_created_at(doc.get("created_at"))
    if dt:
        return f"{dt.strftime('%Y/%m/%d')} 行程"
    return "未命名行程"

def _safe_str(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default

def _parse_day(value: Any) -> int:
    """
    將 day 欄位解析為整數。
    支援：
    - 整數/字串
    - {"$numberInt":"1"} 這種 Mongo Extended JSON
    """
    if isinstance(value, dict) and "$numberInt" in value:
        try:
            return int(value["$numberInt"])
        except Exception:
            return 1
    try:
        return int(value)
    except Exception:
        return 1

# ---------- 1) 顯示所有行程（左側清單 + 右側容器） ----------

@router.get("/trips", response_class=HTMLResponse)
async def trips_page(
    request: Request,
    user_id: Optional[str] = Query(None)
):
    db = request.app.state.db
    templates = request.app.state.templates

    # cookie 補 user_id（users._id）
    uid = user_id or request.cookies.get("user_id")
    if not uid:
        return RedirectResponse(url="/", status_code=302)

    # 以 users._id 找 username，因 structured_itineraries.user_id 是 username（如 "amy"）
    try:
        user = await db["users"].find_one({"_id": ObjectId(uid)})
    except Exception:
        # 若不是合法 ObjectId，則直接導回首頁
        return RedirectResponse(url="/", status_code=302)

    if not user:
        return RedirectResponse(url="/", status_code=302)

    username = user.get("username")
    cursor = db["structured_itineraries"].find({"user_id": username})
    docs = await cursor.to_list(length=None)

    trips: List[Dict[str, Any]] = []
    for d in docs:
        trips.append({
            "_id": _safe_str(d.get("_id")),
            "trip_name": _build_trip_name(d),
        })

    return templates.TemplateResponse(
        "trips.html",
        {"request": request, "trips": trips, "user_id": _safe_str(user.get("_id"))}
    )

# ---------- 2) 行程詳細資訊（右側 Day / Attractions） ----------
# 依 head_id → next_id 串接 nodes，並把每個 node.places 攤平成多筆 attraction

@router.get("/trip_detail/{trip_id}", response_class=JSONResponse)
async def trip_detail(request: Request, trip_id: str):
    db = request.app.state.db

    # structured_itineraries._id 可能是 ObjectId 或字串
    doc = None
    try:
        doc = await db["structured_itineraries"].find_one({"_id": ObjectId(trip_id)})
    except Exception:
        doc = await db["structured_itineraries"].find_one({"_id": trip_id})

    if not doc:
        return {"days": []}

    # 讀出所有 nodes（混合不同天），稍後每一天各自過濾
    all_nodes: List[Dict[str, Any]] = doc.get("nodes", []) or []

    out_days: List[Dict[str, Any]] = []

    for d in doc.get("days", []) or []:
        # 1) 解析 day 編號
        day_num = _parse_day(d.get("day", 1))

        # 2) 取該天 head_id（新版結構）
        head_id = _safe_str(d.get("head_id") or d.get("head") or None, default="") or None

        # 3) 過濾出「屬於這一天」的 node，建立索引
        nodes_for_day: List[Dict[str, Any]] = [
            n for n in all_nodes
            if _parse_day(n.get("day", 1)) == day_num
        ]
        by_id: Dict[str, Dict[str, Any]] = {}
        for n in nodes_for_day:
            nid = _safe_str(n.get("node_id") or n.get("_id") or "")
            if nid:
                by_id[nid] = n

        # 4) 依 head_id → next_id 串接（僅限當天）
        ordered_nodes: List[Dict[str, Any]] = []
        current = head_id
        guard = 0
        while current and current in by_id and guard < 10_000:
            node = by_id[current]
            ordered_nodes.append(node)
            nxt = node.get("next_id")
            current = _safe_str(nxt, default="") or None
            guard += 1

        # 5) 若鏈結殘缺，補上「當天尚未串到」的 node（保持原始順序或用開始時間排序）
        if len(ordered_nodes) < len(by_id):
            seen = { _safe_str(n.get("node_id") or n.get("_id") or "") for n in ordered_nodes }
            leftovers = [n for nid, n in by_id.items() if nid not in seen]

            # 嘗試依 start 時間排序（若無 start 就排最後）
            def _start_key(n: Dict[str, Any]) -> tuple:
                s = _safe_str(n.get("start"))
                # "HH:MM" → (HH, MM)；無或錯誤 → 超大數以便排到後面
                try:
                    hh, mm = s.split(":")
                    return (int(hh), int(mm))
                except Exception:
                    return (99, 99)

            leftovers.sort(key=_start_key)
            ordered_nodes.extend(leftovers)

        # 6) 攤平成 attractions（不合併，一個 place 產一筆）
        attractions: List[Dict[str, Any]] = []
        for node in ordered_nodes:
            node_id = _safe_str(node.get("node_id") or node.get("_id") or "")
            start = _safe_str(node.get("start"))
            end = _safe_str(node.get("end"))
            slot = _safe_str(node.get("slot"))             # 上午/中午/下午/晚上...
            transport = _safe_str(node.get("transport"))   # 若將來有加

            places = node.get("places") or []
            for idx, p in enumerate(places):
                name = _safe_str(p.get("name")).strip()
                if not name:
                    continue
                attractions.append({
                    "_id": f"{node_id}::p{idx}",    # 供 /replace_attraction 使用
                    "name": name,
                    "start_time": start,
                    "end_time": end,
                    "transport": transport,
                    "note": slot or "",             # 右側會顯示在「｜」後面
                    # （以下欄位目前前端沒有用到，但保留可擴充）
                    "place_id": p.get("place_id"),
                    "rating": p.get("rating"),
                    "address": p.get("address"),
                })

        out_days.append({
            "day": day_num,
            "head": head_id,          # 供除錯觀察
            "attractions": attractions
        })

    # 7) 保障天數順序（以 day 編號排序）
    out_days.sort(key=lambda x: x.get("day", 1))

    return {"days": out_days}
