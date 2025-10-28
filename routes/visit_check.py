# routers/visit_check.py  (Python 3.9 版)
from fastapi import APIRouter, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import httpx

# 3.9：用 typing 的 Optional / List / Dict / Tuple / Any 取代 PEP 604 的 |
from typing import Optional, List, Dict, Tuple, Any

router = APIRouter()

class VisitCheckIn(BaseModel):
    name: str
    dest_lat: float
    dest_lon: float
    user_lat: Optional[float] = None
    user_lon: Optional[float] = None
    transport: Optional[str] = None   # 例如：捷運、公車、步行、自行車、機車、開車

def to_google_mode(tp: Optional[str]) -> Tuple[str, str]:
    # 傳回 (google_mode, 中文說明)
    if not tp:
        return ("driving", "預設：開車")
    tp = tp.strip()
    mapping: Dict[str, Tuple[str, str]] = {
        "步行": ("walking", "步行"),
        "自行車": ("bicycling", "自行車"),
        "腳踏車": ("bicycling", "自行車"),
        "公車": ("transit", "大眾運輸"),
        "捷運": ("transit", "大眾運輸"),
        "機車": ("driving", "機車/汽車"),
        "開車": ("driving", "汽車"),
        "汽車": ("driving", "汽車"),
    }
    return mapping.get(tp, ("driving", "機車/汽車"))

def is_open_at(periods: List[Dict[str, Any]], dt: datetime) -> Optional[bool]:
    """
    使用 Places Details 的 opening_hours.periods 判斷 dt 時刻是否在營業中。
    若資料不足回傳 None。
    """
    if not periods:
        return None
    # Google weekday: 0=Sunday ... 6=Saturday
    # Python weekday: 0=Mon ... 6=Sun
    g_weekday = (dt.weekday() + 1) % 7
    hhmm = dt.strftime("%H%M")

    ok: Optional[bool] = None
    for p in periods:
        open_info = p.get("open")
        close_info = p.get("close")
        if not open_info or not close_info:
            continue
        if open_info.get("day") == g_weekday and close_info.get("day") == g_weekday:
            o = f"{open_info.get('time','0000')}"
            c = f"{close_info.get('time','2359')}"
            if o <= hhmm <= c:
                ok = True
            else:
                ok = False
    return ok

@router.post("/visit_check")
async def visit_check(payload: VisitCheckIn, request: Request):
    key = getattr(request.app.state, "google_api_key", None)
    if not key:
        return {"success": False, "message": "GOOGLE_API_KEY 未設定"}

    mode, mode_text = to_google_mode(payload.transport)

    async with httpx.AsyncClient(timeout=20) as client:
        # 1) 先 Find Place 取得 place_id
        find_url = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
        find_params = {
            "input": payload.name,
            "inputtype": "textquery",
            "fields": "place_id,name,formatted_address,geometry",
            "language": "zh-TW",
            "key": key
        }
        f = await client.get(find_url, params=find_params)
        fjs = f.json()
        if not fjs.get("candidates"):
            return {"success": False, "message": "找不到地點"}
        place_id = fjs["candidates"][0]["place_id"]

        # 2) 取 Details（含 opening_hours）
        detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
        d_params = {
            "place_id": place_id,
            "fields": "name,formatted_address,opening_hours,opening_hours/weekday_text,opening_hours/periods,url",
            "language": "zh-TW",
            "key": key
        }
        d = await client.get(detail_url, params=d_params)
        djs = d.json().get("result", {})
        weekday_text = "、".join(djs.get("opening_hours", {}).get("weekday_text", [])) or "未提供"
        periods = djs.get("opening_hours", {}).get("periods", []) or []

        # 3) 估算 ETA
        eta_min: Optional[int] = None
        arrival_local: Optional[str] = None
        if payload.user_lat is not None and payload.user_lon is not None:
            dm_url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            dm_params = {
                "origins": f"{payload.user_lat},{payload.user_lon}",
                "destinations": f"place_id:{place_id}",
                "mode": mode,
                "departure_time": "now",
                "language": "zh-TW",
                "key": key
            }
            dm = await client.get(dm_url, params=dm_params)
            dmjs = dm.json()
            elem = (dmjs.get("rows") or [{}])[0].get("elements", [{}])[0]
            dur = (elem.get("duration_in_traffic") or elem.get("duration") or {}).get("value")
            if isinstance(dur, int):
                eta_min = round(dur / 60)
                tz = ZoneInfo("Asia/Taipei")
                arrival_local = (datetime.now(tz) + timedelta(seconds=dur)).strftime("%p %I:%M").replace("AM","上午").replace("PM","下午")

        # 4) 判斷抵達時是否營業
        will_open: Optional[bool] = None
        if arrival_local and periods and eta_min is not None:
            tz = ZoneInfo("Asia/Taipei")
            dt_arrival = datetime.now(tz) + timedelta(minutes=eta_min)
            will_open = is_open_at(periods, dt_arrival)

        return {
            "success": True,
            "place_id": place_id,
            "opening_text": weekday_text,
            "eta_minutes": eta_min,
            "arrival_time_local": arrival_local,
            "will_be_open_at_arrival": will_open,
            "transport_mode_text": mode_text
        }