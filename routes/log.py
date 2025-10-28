# routers/log.py
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from pathlib import Path
import json, asyncio, os, datetime, re

router = APIRouter(prefix="/log", tags=["log"])

# 單進程內的檔案鎖（避免同時寫入互踩）
_FILE_LOCK = asyncio.Lock()

# === 檔案位置 ===
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BASE_DIR / "recommendations_store.json"

class RecPayload(BaseModel):
    trip_id: Optional[str] = None
    original_spot: Optional[str] = None
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)

def _norm_name(s: Any) -> str:
    """名稱正規化：大小寫、全半形、空白、符號；臺→台。"""
    if s is None:
        return ""
    t = str(s).strip()
    t = t.replace("臺", "台").lower()
    t = re.sub(r"\s+", "", t)
    # 只保留數字/英文字母/底線/CJK
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t

def _read_logs() -> Dict[str, Any]:
    """讀取現有 JSON；不存在或壞掉時回傳空結構。"""
    if LOG_FILE.exists():
        try:
            text = LOG_FILE.read_text(encoding="utf-8")
            if text.strip():
                return json.loads(text)
        except Exception:
            pass
    return {"logs": []}

def _atomic_write(data: Dict[str, Any]) -> None:
    """寫到暫存檔後原子替換，降低檔案損毀風險。"""
    tmp = LOG_FILE.with_suffix(LOG_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LOG_FILE)

@router.post("/recommendations")
async def log_recommendations(payload: RecPayload):
    # 便於後續查詢去重：抽取 place_ids 與正規化名稱
    place_ids = []
    names_norm = []
    for r in payload.recommendations:
        pid = r.get("place_id") or r.get("placeId") or r.get("id")
        if pid:
            place_ids.append(str(pid))
        n = _norm_name(r.get("name"))
        if n:
            names_norm.append(n)

    entry = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "trip_id": payload.trip_id,
        "original_spot": payload.original_spot,
        "recommendations": payload.recommendations,
        "place_ids": sorted(list(set(place_ids))),
        "names_norm": sorted(list(set(names_norm))),
    }

    async with _FILE_LOCK:
        data = _read_logs()
        data["logs"].append(entry)

        # 只保留最後 500 筆
        MAX_LOGS = 500
        if len(data["logs"]) > MAX_LOGS:
            data["logs"] = data["logs"][-MAX_LOGS:]

        _atomic_write(data)
        count = len(data["logs"])

    return {"success": True, "file": LOG_FILE.name, "count": count}

# --- 查詢：某個 (trip_id, original_spot) 已經推薦過哪些 ---
@router.get("/recommendations/seen")
async def seen_recommendations(
    trip_id: str = Query(...),
    original_spot: str = Query(...)
):
    data = _read_logs()
    want_name = _norm_name(original_spot)
    ids, names = set(), set()
    for e in data.get("logs", []):
        if str(e.get("trip_id")) == str(trip_id) and _norm_name(e.get("original_spot")) == want_name:
            for pid in e.get("place_ids", []):
                ids.add(str(pid))
            for nm in e.get("names_norm", []):
                names.add(nm)
            # 兼容舊格式（無 place_ids/names_norm）
            for r in e.get("recommendations", []):
                pid = r.get("place_id") or r.get("placeId") or r.get("id")
                if pid:
                    ids.add(str(pid))
                nm = _norm_name(r.get("name"))
                if nm:
                    names.add(nm)
    return {"place_ids": sorted(ids), "names": sorted(names)}

# --- 除錯用：查看累積筆數 ---
@router.get("/recommendations/count")
async def count_recommendations():
    data = _read_logs()
    return {"file": LOG_FILE.name, "count": len(data.get("logs", []))}

# --- 除錯用：直接取得全部內容 ---
@router.get("/recommendations")
async def get_all_recommendations():
    return _read_logs()
