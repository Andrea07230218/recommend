from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta, date
from dateutil import parser
from openai import OpenAI
import httpx
import pytz
import re
from bson import ObjectId
import math
from typing import Optional, List, Dict, Any, Tuple, Set
from pathlib import Path
import json
import asyncio 
from fastapi import APIRouter, Request, HTTPException
import httpx
from datetime import datetime, timedelta
from dateutil import parser

# 引入 Pydantic 模型
from pydantic import BaseModel, Field # 👈 1. 匯入

router = APIRouter()

# --- 🔽🔽 2. 【修改 Pydantic 模型以匹配 UI】 🔽🔽 ---

# 1. 定義 Android App 會傳來的 Request Body
class StartInfoRequest(BaseModel):
    placeId: str
    lat: float
    lng: float

# 2. 定義要回傳給 App 的 Response Body (與 Kotlin data class 匹配)
class WeatherInfo(BaseModel):
    # ‼️ 欄位名稱 *完全* 依照您 UI 想要的
    summary: str = "未知"
    temperatureC: int = 0
    rainProbability: Optional[int] = None # 降雨機率 % (允許 null)

class StartInfo(BaseModel):
    placeId: str
    weather: WeatherInfo = Field(default_factory=WeatherInfo)
    openNow: bool = False
    openStatusText: str = "營業狀態未知"
    alternatives: List[Any] = Field(default_factory=list)
    page: int = 0
    openingHours: List[str] = Field(default_factory=list) # 👈 營業時間欄位

# =========================
# 記錄檔存取
# =========================
try:
    from .log import LOG_FILE as REC_LOG_FILE  # type: ignore
except Exception:
    REC_LOG_FILE = Path(__file__).resolve().parent.parent / "recommendations_store.json"

def _read_rec_logs_local() -> Dict[str, Any]:
    if Path(REC_LOG_FILE).exists():
        try:
            return json.loads(Path(REC_LOG_FILE).read_text(encoding="utf-8"))
        except Exception:
            return {"logs": []}
    return {"logs": []}

# =========================
# 共用小工具
# =========================
def _extract_preferences(form_doc: dict) -> List[str]:
    """
    從 form_doc 與 form_doc['form'] 裡彈性抽出「偏好」類別。
    支援：
      - 欄位名：preferences / preference / prefs / categories / tags / interests / likes
      - 形態：str（逗號分隔）、list[str]、list[dict{label|name|text|title|value}]、
              dict[str,bool]（true 代表勾選）
    會做：去重、去空白、把「臺」正規化成「台」。
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", (s or "").replace("臺", "台")).strip()

    # 可能存放的位置：根層 & form 子物件
    containers: List[dict] = [form_doc or {}, (form_doc.get("form") or {})]
    keys = ["preferences", "preference", "prefs", "categories", "tags", "interests", "likes"]

    raw_items: List[Any] = []
    for src in containers:
        for k in keys:
            if k in src and src[k] not in (None, ""):
                raw_items.append(src[k])

    prefs: List[str] = []
    for v in raw_items:
        if isinstance(v, str):
            prefs.extend([x.strip().replace("臺","台") for x in v.split(",") if x.strip()])
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, str):
                    s = it.strip().replace("臺","台")
                    if s: prefs.append(s)
                elif isinstance(it, dict):
                    # 依序嘗試這些欄位，拿到第一個有值的就採用
                    for field in ("label", "name", "text", "title", "value"):
                        if it.get(field):
                            s = str(it[field]).strip().replace("臺","台")
                            if s:
                                prefs.append(s)
                                break
        elif isinstance(v, dict):
            # 例如 {"博物館": true, "美術館": false}
            for k, flag in v.items():
                ok = False
                try:
                    ok = bool(flag)
                except Exception:
                    ok = False
                if ok and isinstance(k, str) and k.strip():
                    prefs.append(k.strip().replace("臺","台"))

    # 依輸入順序去重
    seen = set()
    out: List[str] = []
    for p in prefs:
        n = _norm(p)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(p.strip())
    return out

def _norm_name(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip()
    t = t.replace("臺", "台").lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t

async def _collect_exclusions(request: Request, trip_id: str, original_spot: str) -> Tuple[Set[str], Set[str]]:
    """
    回傳 (exclude_place_ids, exclude_names_norm)
    來源：
      1) 本地 JSON logs：同一 (trip_id, original_spot) 已推薦過的
      2) 現有行程 structured_itineraries：nodes[].places[].name + used_places[]
      3) 原始景點名稱本身
    """
    exclude_ids, exclude_names = set(), set()
    data = _read_rec_logs_local()
    want_name = _norm_name(original_spot)
    for e in data.get("logs", []):
        if str(e.get("trip_id")) == str(trip_id) and _norm_name(e.get("original_spot")) == want_name:
            for pid in e.get("place_ids", []):
                exclude_ids.add(str(pid))
            for nm in e.get("names_norm", []):
                exclude_names.add(nm)
            for r in e.get("recommendations", []):  # 兼容舊格式
                pid = r.get("place_id") or r.get("placeId") or r.get("id")
                if pid:
                    exclude_ids.add(str(pid))
                nm = _norm_name(r.get("name"))
                if nm:
                    exclude_names.add(nm)

    # 行程中已存在的名稱
    db = request.app.state.db
    si_doc = None
    try:
        si_doc = await db["structured_itineraries"].find_one({"_id": ObjectId(trip_id)})
    except Exception:
        si_doc = await db["structured_itineraries"].find_one({"_id": trip_id})

    if si_doc:
        for n in si_doc.get("nodes", []) or []:
            for p in n.get("places", []) or []:
                nm = _norm_name(p.get("name"))
                if nm:
                    exclude_names.add(nm)
        for nm in si_doc.get("used_places", []) or []:
            nn = _norm_name(nm)
            if nn:
                exclude_names.add(nn)

    # 原始景點本身也排除
    exclude_names.add(_norm_name(original_spot))

    return exclude_ids, exclude_names

def _as_object_id(v):
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, dict) and "$oid" in v:
        try:
            return ObjectId(str(v["$oid"]))
        except Exception:
            return None
    if isinstance(v, str):
        try:
            return ObjectId(v)
        except Exception:
            return None
    return None

# =========================
# 位置/時間/距離工具
# =========================
async def get_city_from_latlon(request: Request, lat: float, lon: float) -> str:
    google_api_key = request.app.state.google_api_key
    base_url = "https://maps.googleapis.com/maps/api/geocode/json"

    TW_CITIES = set("""台北市 新北市 基隆市 桃園市 新竹市 新竹縣 苗栗縣 台中市 彰化縣 南投縣
                       雲林縣 嘉義市 嘉義縣 台南市 高雄市 屏東縣 宜蘭縣 花蓮縣 台東縣
                       澎湖縣 金門縣 連江縣""".split())
    city_regex = r'(台北市|新北市|基隆市|桃園市|新竹市|新竹縣|苗栗縣|台中市|彰化縣|南投縣|雲林縣|嘉義市|嘉義縣|台南市|高雄市|屏東縣|宜蘭縣|花蓮縣|台東縣|澎湖縣|金門縣|連江縣)'

    def pick_city(results: List[Dict[str, Any]]) -> Optional[str]:
        priority = [
            "locality", "administrative_area_level_2", "administrative_area_level_1",
            "postal_town", "administrative_area_level_3", "sublocality_level_1",
        ]
        for r in results:
            for comp in r.get("address_components", []):
                name = (comp.get("long_name") or "").replace("臺", "台")
                types = comp.get("types", [])
                if any(t in types for t in priority) and name in TW_CITIES:
                    return name
        for r in results:
            addr = (r.get("formatted_address") or "").replace("臺", "台")
            m = re.search(city_regex, addr)
            if m:
                return m.group(1)
        return None

    async with httpx.AsyncClient(timeout=15) as client:
        params1 = {
            "latlng": f"{lat},{lon}",
            "language": "zh-TW",
            "region": "tw",
            "result_type": "locality|administrative_area_level_2",
            "key": google_api_key,
        }
        res1 = await client.get(base_url, params=params1)
        data1 = res1.json()

        results = data1.get("results") or []
        city = pick_city(results)
        if city:
            return city

        params2 = {
            "latlng": f"{lat},{lon}",
            "language": "zh-TW",
            "region": "tw",
            "key": google_api_key,
        }
        res2 = await client.get(base_url, params=params2)
        data2 = res2.json()

        results2 = data2.get("results") or []
        city2 = pick_city(results2)
        if city2:
            return city2

    return "未知城市"

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def is_open_now_by_weekday_text(weekday_text: List[str]) -> bool:
    now = datetime.now(pytz.timezone('Asia/Taipei'))
    weekday = now.weekday()
    time_line = weekday_text[weekday] if len(weekday_text) > weekday else ""
    match = re.search(r'(\d{1,2}:\d{2})\s*[-–—~～]\s*(\d{1,2}:\d{2})', time_line)
    if not match:
        return False
    open_str, close_str = match.groups()
    try:
        open_time = datetime.strptime(open_str, "%H:%M").time()
        close_time = datetime.strptime(close_str, "%H:%M").time()
        now_time = now.time()
        if close_time <= open_time:
            return now_time >= open_time or now_time <= close_time
        else:
            return open_time <= now_time <= close_time
    except ValueError:
        return False

# ★ 今天公休（必要排除條件）
def is_closed_today_by_weekday_text(weekday_text: List[str]) -> bool:
    now = datetime.now(pytz.timezone('Asia/Taipei'))
    weekday = now.weekday()
    if not weekday_text or len(weekday_text) <= weekday:
        return False
    line = (weekday_text[weekday] or "").replace("：", ":").replace("　", " ").strip().lower()
    if ":" in line:
        line = line.split(":", 1)[1].strip()
    closed_keywords = ["公休", "休息", "未營業", "停止營業", "暫停營業", "closed"]
    return any(kw in line for kw in closed_keywords)

def within_user_activity_window(activity_time: Dict[str, str]) -> bool:
    try:
        tz = pytz.timezone('Asia/Taipei')
        now = datetime.now(tz).time()
        start = datetime.strptime(activity_time.get("start", "08:00"), "%H:%M").time()
        end = datetime.strptime(activity_time.get("end", "20:00"), "%H:%M").time()
        if end <= start:
            return (now >= start) or (now <= end)
        return start <= now <= end
    except Exception:
        return True

# =========================
# 天氣 / Geocode
# =========================
@router.get("/weather")
async def get_weather(request: Request, lat: float, lon: float):
    """
    取得天氣資訊並判斷未來 1 小時是否會下雨
    供 Android App 使用
    """
    print(f"\n=== /weather called: lat={lat}, lon={lon} ===")
    
    weather_key = request.app.state.openweather_api_key
    gmap_key = request.app.state.google_api_key
    
    # 檢查 API Keys
    if not weather_key:
        print("[ERROR] OpenWeather API Key not found")
        raise HTTPException(status_code=500, detail="OpenWeather API Key 未設定")
    
    if not gmap_key:
        print("[ERROR] Google Maps API Key not found")
        raise HTTPException(status_code=500, detail="Google Maps API Key 未設定")
    
    print(f"[DEBUG] Weather Key: {weather_key[:10]}..., GMaps Key: {gmap_key[:10]}...")
    
    # API URLs
    forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={weather_key}&units=metric&lang=zh_tw"
    current_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={weather_key}&units=metric&lang=zh_tw"
    geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&language=zh-TW&key={gmap_key}"
    
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            print("[DEBUG] Fetching weather data...")
            forecast_res = await client.get(forecast_url)
            current_res = await client.get(current_url)
            geo_res = await client.get(geocode_url)
            
            print(f"[DEBUG] Forecast status: {forecast_res.status_code}")
            print(f"[DEBUG] Current status: {current_res.status_code}")
            print(f"[DEBUG] Geocode status: {geo_res.status_code}")
            
            forecast_res.raise_for_status()
            current_res.raise_for_status()
            geo_res.raise_for_status()
            
        except httpx.HTTPStatusError as e:
            print(f"[ERROR] API request failed: {e.response.status_code}")
            print(f"[ERROR] Response: {e.response.text}")
            raise HTTPException(
                status_code=500, 
                detail=f"天氣 API 請求失敗: {e.response.text}"
            )
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            raise HTTPException(status_code=500, detail=f"查詢天氣時發生錯誤: {str(e)}")
    
    # 解析回應
    forecast_data = forecast_res.json()
    current_data = current_res.json()
    geo_data = geo_res.json()
    
    # 驗證資料
    if "list" not in forecast_data:
        print("[ERROR] Invalid forecast data")
        raise HTTPException(status_code=500, detail="天氣預報資料格式錯誤")
    
    if "weather" not in current_data or "main" not in current_data:
        print("[ERROR] Invalid current weather data")
        raise HTTPException(status_code=500, detail="當前天氣資料格式錯誤")
    
    # 找出最接近 1 小時後的預報
    target_time = datetime.utcnow() + timedelta(hours=1)
    closest = min(
        forecast_data["list"], 
        key=lambda item: abs(parser.parse(item["dt_txt"]) - target_time)
    )
    
    # 計算降雨機率
    pop = closest.get("pop", 0)  # Probability of Precipitation
    forecast_weather = closest.get("weather", [])
    
    # 判斷是否會下雨（降雨機率 > 40% 或天氣描述包含 rain）
    will_rain = pop > 0.4 or any("rain" in w.get("main", "").lower() for w in forecast_weather)
    
    # 當前天氣
    current_weather = current_data.get("weather", [{}])[0]
    temperature = current_data.get("main", {}).get("temp")
    
    # 位置名稱
    location_name = "未知位置"
    if geo_data.get("results"):
        location_name = geo_data["results"][0].get("formatted_address", "未知位置")
    
    result = {
        "will_rain": will_rain,
        "rain_probability": pop,
        "temperature": temperature,
        "description": current_weather.get("description", "未知"),
        "location": location_name,
        "forecast_time": closest.get("dt_txt")
    }
    
    print(f"[SUCCESS] Weather data: will_rain={will_rain}, pop={pop}, temp={temperature}°C")
    return result

# ### 強化：支援 city_hint，避免抓到錯城市
@router.get("/geocode")
async def geocode(request: Request, place_name: str, city_hint: Optional[str] = None):
    google_api_key = request.app.state.google_api_key

    async def _do(address: str) -> Optional[Dict[str, float]]:
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={address}&region=tw&key={google_api_key}"
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(url)
            data = res.json()
        if data.get("status") != "OK":
            return None
        loc = data["results"][0]["geometry"]["location"]
        return {"lat": loc["lat"], "lon": loc["lng"]}

    # 1) city_hint + place
    if city_hint:
        q1 = f"{city_hint} {place_name}"
        r = await _do(q1)
        if r:
            return r

    # 2) 原字串（含可能的縣市）
    r2 = await _do(place_name)
    if r2:
        return r2

    # 3) fallback：只丟地名（移除常見城市關鍵字以外文字）
    m = re.search(r"(台北|新北|基隆|桃園|新竹|苗栗|台中|彰化|南投|雲林|嘉義|台南|高雄|屏東|宜蘭|花蓮|台東)", place_name)
    if m:
        plain = place_name.replace(m.group(0), "").strip()
        r3 = await _do(plain)
        if r3:
            return r3

    return {"error": "Geocoding failed"}

# =========================
# 偏好 / GPT
# =========================
@router.get("/api/get_preferences")
async def get_user_preferences(request: Request, trip_id: str):
    """
    支援兩種 trip_id：
      1) 直接是 forms._id
      2) 是 structured_itineraries._id（此時優先讀 structured_itineraries.form_id，再去 forms 查）

    回傳：
    {
        "preferences": [...],
        "browse_categories": [...],
        "saved_categories": [...],
        "google_rating": float,
        "activity_time": {"start":"HH:MM","end":"HH:MM"},
        "start_date": "YYYY-MM-DD",
        "end_date":   "YYYY-MM-DD",
        "locations":  [<縣市> ...]
    }
    """
    db = request.app.state.db

    # ---------- A) 嘗試把 trip_id 視為 forms._id ----------
    form_doc = None
    oid = _as_object_id(trip_id)
    if oid:
        form_doc = await db["forms"].find_one({"_id": oid})
    if not form_doc:
        # 也試試純字串 id（若你的 forms._id 不是 ObjectId）
        form_doc = await db["forms"].find_one({"_id": trip_id}) or form_doc

    # ---------- B) 若不是 forms，就把它當 structured_itineraries._id ----------
    if not form_doc:
        si_doc = None
        oid2 = _as_object_id(trip_id)
        if oid2:
            si_doc = await db["structured_itineraries"].find_one({"_id": oid2})
        if not si_doc:
            si_doc = await db["structured_itineraries"].find_one({"_id": trip_id})

        if si_doc:
            # ★ 這裡支援多種欄位名稱：form_id（你現在用的）、trip_preference_id（舊版/其他專案）
            pref_id = (
                si_doc.get("form_id")
                or si_doc.get("trip_preference_id")
                or si_doc.get("preference_form_id")
            )
            if pref_id is not None:
                pref_oid = _as_object_id(pref_id)
                if pref_oid:
                    form_doc = await db["forms"].find_one({"_id": pref_oid})
                if not form_doc:
                    # 有些專案會用純字串 id
                    form_doc = await db["forms"].find_one({"_id": str(pref_id)})

    if not form_doc:
        return JSONResponse({"error": "找不到行程表單"}, status_code=404)

    # ---- 解析 forms ----
    form = form_doc.get("form") or {}

    # 1) 聚合 user 名單（leader + members）
    leader_username = form.get("leader_id") or form_doc.get("user_id")  # 字串 username，如 "amy"
    members = [m.get("user_id") for m in form.get("members", []) if isinstance(m, dict) and m.get("user_id")]
    usernames = []
    if leader_username:
        usernames.append(str(leader_username))
    for u in members:
        su = str(u)
        if su not in usernames:
            usernames.append(su)

    # 2) 日期/天數 → start/end
    raw_date = form.get("date") or form_doc.get("date")
    raw_days = form.get("days") or form_doc.get("days") or 1
    try:
        days = int(raw_days.get("$numberInt")) if isinstance(raw_days, dict) and "$numberInt" in raw_days else int(raw_days)
    except Exception:
        days = 1

    try:
        if isinstance(raw_date, str):
            start_dt = parser.parse(raw_date).date()
        else:
            start_dt = parser.parse(str(raw_date)).date()
    except Exception:
        start_dt = datetime.now().date()
    end_dt = start_dt + timedelta(days=max(days, 1) - 1)
    start_date = start_dt.isoformat()
    end_date = end_dt.isoformat()

    # 3) 活動時間
    raw_time = form.get("time_range") or form_doc.get("time_range") or "08:00-20:00"
    norm_time = str(raw_time).replace("—", "-").replace("–", "-").replace("～", "-").replace("~", "-")
    m = re.search(r"^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$", norm_time)
    activity_time = {"start": m.group(1), "end": m.group(2)} if m else {"start": "08:00", "end": "20:00"}

    # 4) 偏好（★改用彈性抽取：同時支援根層與 form 子物件，以及多種形態）
    preferences = _extract_preferences(form_doc)

    # 5) Google 評分門檻（表單沒提供就 4.0）
    google_rating = 4.0
    inner_google_rates = form.get("Google_rates") or form.get("google_rates") or form_doc.get("Google_rates") or form_doc.get("google_rates")
    if inner_google_rates is not None:
        try:
            google_rating = float(inner_google_rates)
        except Exception:
            pass

    # 6) 旅遊地點：支援新版 `locations`（陣列）與舊版 `location`（字串），也同時檢查根層
    raw_locations = (
        form.get("locations") or form_doc.get("locations") or
        form.get("location") or form_doc.get("location")
    )
    if isinstance(raw_locations, list):
        locations = [str(x).strip().replace("臺", "台") for x in raw_locations if str(x).strip()]
    elif isinstance(raw_locations, str) and raw_locations.strip():
        locations = [raw_locations.strip().replace("臺", "台")]
    else:
        locations = []

    # 7) 從 users 取得每位 username 的 ObjectId（字串），用作 user_browse / user_favorite 的 user_id
    username_to_oid_str = {}
    if usernames:
        cursor = db["users"].find({"username": {"$in": usernames}}, {"_id": 1, "username": 1})
        async for u in cursor:
            username_to_oid_str[u["username"]] = str(u["_id"])

    # 8) 聚合 browse_categories（user_browse.tags，且 dwell_seconds > 60）
    browse_tag_set = set()
    for uname in usernames:
        oid_str = username_to_oid_str.get(uname)
        q = {"dwell_seconds": {"$gt": 60}}
        if oid_str:
            q["user_id"] = oid_str  # 你的 user_browse.user_id 是字串形態的 ObjectId
        cursor = db["user_browse"].find(q, {"tags": 1})
        async for b in cursor:
            for tag in (b.get("tags") or []):
                t = str(tag).strip()
                if t:
                    browse_tag_set.add(t)

    # 9) 聚合 saved_categories（user_favorite.tags；支援 user_id 或 user=username）
    saved_tag_set = set()
    for uname in usernames:
        oid_str = username_to_oid_str.get(uname)
        or_filters = []
        if oid_str:
            or_filters.append({"user_id": oid_str})
        or_filters.append({"user": uname})  # 你的 sample 有 user="amy"
        q = {"$or": or_filters} if len(or_filters) > 1 else or_filters[0]
        cursor = db["user_favorite"].find(q, {"tags": 1})
        async for fav in cursor:
            for tag in (fav.get("tags") or []):
                t = str(tag).strip()
                if t:
                    saved_tag_set.add(t)

    browse_categories = sorted(browse_tag_set)
    saved_categories = sorted(saved_tag_set)

    return {
        "preferences": preferences,
        "browse_categories": browse_categories,
        "saved_categories": saved_categories,
        "google_rating": google_rating,
        "activity_time": activity_time,
        "start_date": start_date,
        "end_date": end_date,
        "locations": locations
    }

# =========================
# GPT：分類/室內/清單
# =========================

# 多標籤分類（最多三個）
async def get_attraction_categories(request: Request, name: str) -> List[str]:
    openai_client = OpenAI(api_key=request.app.state.openai_api_key)
    prompt = f"""
你是一位旅遊景點分類助理。請為以下台灣景點產出**最多三個**類別標籤（以逗號分隔，不要加其它文字）。
常見範例標籤：自然景觀, 公園, 文創, 文化, 歷史, 歷史古蹟, 藝術, 藝文, 博物館, 美術館, 展覽, 表演, 親子, 教育, 建築, 攝影, 美食巡禮, 購物, 室內樂園, 科學, 天文, 水族館, 室內植物園

景點名稱：{name}
輸出格式範例：博物館, 藝術, 文化
"""
    result = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "只輸出標籤清單，最多三個，使用逗號分隔。"},
            {"role": "user", "content": prompt.strip()}
        ],
        temperature=0.1
    )
    cats = (result.choices[0].message.content or "").strip()
    return [c.strip() for c in cats.split(",") if c.strip()][:3]

# 是否室內
async def is_indoor_place(request: Request, name: str) -> bool:
    openai_client = OpenAI(api_key=request.app.state.openai_api_key)
    prompt = f"""
以下列出一個台灣景點的名稱，請你判斷它是否為【室內景點】。
請直接回答「是」或「否」。

景點名称：{name}
"""
    result = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "你是一個旅遊分類助理，負責判斷是否為室內場所。"},
            {"role": "user", "content": prompt.strip()}
        ]
    )
    reply = (result.choices[0].message.content or "").strip().replace("。", "")
    return "是" in reply

# GPT 推薦清單
async def generate_gpt_recommendations(request: Request, prompt: str) -> List[Dict[str, str]]:
    openai_client = OpenAI(api_key=request.app.state.openai_api_key)
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "你是一個旅遊推薦助手，根據使用者偏好提供室內景點建議。請以繁體中文回應。"},
            {"role": "user", "content": prompt.strip()}
        ],
        temperature=0.4
    )
    content = response.choices[0].message.content

    recommendations: List[Dict[str, str]] = []
    # 預期格式：
    # 1. 景點名稱
    #    簡介：...
    #    推薦依據：...
    pattern = re.compile(r'^\s*\d+\.\s*(.*?)\n\s*簡介：(.*?)\n\s*推薦依據：(.*?)\s*$', re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(content):
        recommendations.append({
            "name": match.group(1).strip(),
            "summary": match.group(2).strip(),
            "reason": match.group(3).strip()
        })

    return recommendations

# =========================
# Google Places：驗證/照片
# =========================

# 支援可調整半徑、今天公休必排除、關閉狀態必排除
async def get_verified_google_place(
    request: Request, 
    name: str, 
    lat: float, 
    lon: float, 
    min_rating: float = 4.0,
    relaxed: bool = False,
    radius_m: int = 5000,
):
    google_api_key = request.app.state.google_api_key

    async def fetch_place_detail(place_id: str, client: httpx.AsyncClient):
        detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
        detail_params = {
            "place_id": place_id,
            "key": google_api_key,
            "fields": "name,rating,formatted_address,opening_hours,geometry,types,business_status",
            "language": "zh-TW"
        }
        detail_res = await client.get(detail_url, params=detail_params)
        return detail_res.json().get("result", {})

    async with httpx.AsyncClient(timeout=15) as client:
        nearby_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        nearby_params = {
            "key": google_api_key,
            "location": f"{lat},{lon}",
            "radius": radius_m,
            "keyword": name,
            "language": "zh-TW"
        }
        res = await client.get(nearby_url, params=nearby_params)
        data = res.json()

        if not data.get("results"):
            text_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
            text_params = {
                "key": google_api_key,
                "query": name,
                "location": f"{lat},{lon}",
                "radius": radius_m,
                "language": "zh-TW"
            }
            res = await client.get(text_url, params=text_params)
            data = res.json()

        candidates = []
        for result in data.get("results", []):
            place_id = result.get("place_id")
            if not place_id:
                continue
            detail_data = await fetch_place_detail(place_id, client)

            # ★ business_status 關閉 => 直接排除
            bs = (detail_data.get("business_status") or result.get("business_status") or "").upper()
            if bs in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}:
                continue

            rating = detail_data.get("rating", 0) or result.get("rating", 0) or 0
            open_hours = detail_data.get("opening_hours", {})
            weekday_text = open_hours.get("weekday_text", [])
            name_g = detail_data.get("name") or result.get("name")
            addr = detail_data.get("formatted_address") or result.get("formatted_address")
            location = (detail_data.get("geometry") or {}).get("location") or (result.get("geometry") or {}).get("location") or {}
            types = detail_data.get("types") or result.get("types", [])

            # ★ 今天公休 → 必排除
            if weekday_text and is_closed_today_by_weekday_text(weekday_text):
                continue

            open_now_flag = is_open_now_by_weekday_text(weekday_text) if weekday_text else False

            if not relaxed:
                if rating < min_rating:
                    continue
                if weekday_text and not open_now_flag:
                    continue

            candidates.append({
                "name": name_g,
                "address": addr,
                "rating": rating,
                "place_id": place_id,
                "types": types,
                "opening_hours": open_hours,
                "lat": location.get("lat"),
                "lon": location.get("lng"),
                "open_now": open_now_flag
            })

        if not candidates:
            return None

        if relaxed:
            def pick_key(x):
                dist_penalty = 0.0
                try:
                    if x.get("lat") and x.get("lon"):
                        dist_penalty = haversine(lat, lon, x["lat"], x["lon"]) * 0.05
                except Exception:
                    dist_penalty = 0.0
                return (x.get("rating", 0) or 0) - dist_penalty + (0.3 if x.get("open_now") else 0.0)
            best = sorted(candidates, key=pick_key, reverse=True)[0]
            return best

        return candidates[0]

async def get_place_photo_url(google_api_key: str, place_id: str, maxwidth: int = 720) -> Optional[str]:
    # Places API (New)
    try:
        place_url = f"https://places.googleapis.com/v1/places/{place_id}"
        headers = {"X-Goog-Api-Key": google_api_key, "X-Goog-FieldMask": "photos"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(place_url, headers=headers)
            data = r.json()

        photos = (data or {}).get("photos") or []
        if photos:
            photo_name = photos[0].get("name")
            if photo_name:
                return f"https://places.googleapis.com/v1/{photo_name}/media?maxWidthPx={maxwidth}&key={google_api_key}"
    except Exception:
        pass

    # Legacy fallback
    try:
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        params = {"key": google_api_key, "place_id": place_id, "fields": "photos"}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(details_url, params=params)
            data = r.json()

        photos = (data.get("result") or {}).get("photos") or []
        if not photos:
            return None

        ref = photos[0].get("photo_reference")
        if not ref:
            return None

        return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth={maxwidth}&photo_reference={ref}&key={google_api_key}"
    except Exception:
        return None

# =========================
# 計分
# =========================
async def score_candidate(
    request: Request,
    candidate: Dict[str, Any],
    *,
    original_city: str,
    original_categories: List[str],  # ★ 改成多標籤
    user_lat: Optional[float],
    user_lon: Optional[float],
    pref_rating_threshold: float,
    activity_time: Dict[str, str]
) -> Dict[str, Any]:
    score = 0.0
    breakdown = []

    name = candidate.get("name")
    lat = candidate.get("lat")
    lon = candidate.get("lon")
    rating = float(candidate.get("rating") or 0)
    weekday_text = (candidate.get("opening_hours") or {}).get("weekday_text", [])
    open_now_flag = is_open_now_by_weekday_text(weekday_text) if weekday_text else False

    distance = None
    if user_lat is not None and user_lon is not None and lat and lon:
        try:
            distance = round(haversine(user_lat, user_lon, lat, lon), 2)
        except Exception:
            distance = None

    city = None
    if lat and lon:
        try:
            city = await get_city_from_latlon(request, lat, lon)
        except Exception:
            city = None

    # A. 營業中
    if open_now_flag:
        score += 1.0
        breakdown.append({"name": "目前營業中", "delta": +1.0, "why": "weekday_text 判定為營業中"})
    else:
        breakdown.append({"name": "目前未營業", "delta": 0.0, "why": "weekday_text 顯示未營業或無法解析"})

    # B. 評分
    if rating >= pref_rating_threshold + 0.3:
        score += 2.0
        breakdown.append({"name": "高評分(門檻+0.3)", "delta": +2.0, "why": f"評分={rating}，門檻={pref_rating_threshold}"})
    elif rating >= pref_rating_threshold:
        score += 1.0
        breakdown.append({"name": "達評分門檻", "delta": +1.0, "why": f"評分={rating}，門檻={pref_rating_threshold}"})
    else:
        breakdown.append({"name": "低於評分門檻", "delta": 0.0, "why": f"評分={rating}，門檻={pref_rating_threshold}"})

    # C. 距離
    if distance is not None:
        if distance <= 3:
            score += 1.0
            breakdown.append({"name": "距離近(≤3km)", "delta": +1.0, "why": f"{distance}km"})
        elif distance <= 6:
            score += 0.5
            breakdown.append({"name": "距離中(≤6km)", "delta": +0.5, "why": f"{distance}km"})
        else:
            breakdown.append({"name": "距離遠(>6km)", "delta": 0.0, "why": f"{distance}km"})
    else:
        breakdown.append({"name": "距離未知", "delta": 0.0, "why": "缺少座標"})

    # D. 同城市
    if city and original_city and city == original_city:
        score += 1.0
        breakdown.append({"name": "同城市", "delta": +1.0, "why": f"{city}"})
    else:
        breakdown.append({"name": "不同城市/未知", "delta": 0.0, "why": f"{city} vs {original_city}"})

    # E. 活動時間
    if within_user_activity_window(activity_time):
        score += 0.5
        breakdown.append({"name": "符合活動時間", "delta": +0.5, "why": f"{activity_time['start']}~{activity_time['end']}"})
    else:
        breakdown.append({"name": "不在活動時間", "delta": 0.0, "why": f"{activity_time['start']}~{activity_time['end']}"})

    # F. 類別相似度（多標籤）
    try:
        cand_categories = await get_attraction_categories(request, name)
        if set(cand_categories) & set(original_categories):
            score += 1.0
            breakdown.append({"name": "類別相近", "delta": +1.0, "why": f"{cand_categories} ≈ {original_categories}"})
        else:
            breakdown.append({"name": "類別不相近/未知", "delta": 0.0, "why": f"{cand_categories} vs {original_categories}"})
    except Exception as e:
        breakdown.append({"name": "類別判斷失敗", "delta": 0.0, "why": str(e)})

    out = dict(candidate)
    out["score"] = round(score, 2)
    out["score_breakdown"] = breakdown
    out["distance"] = distance
    out["city"] = city
    return out

# =========================
# 備援搜尋（無 GPT）
# =========================
INDOOR_KEYWORDS_BY_CATEGORY: Dict[str, List[str]] = {
    "博物館": ["博物館", "美術館", "展覽館"],
    "藝文":   ["美術館", "藝文中心", "文化中心", "演藝廳"],
    "文化":   ["文化中心", "展覽館", "藝文中心"],
    "表演":   ["音樂廳", "演藝廳", "室內表演場"],
    "親子":   ["兒童博物館", "科學館", "室內樂園"],
    "購物":   ["購物中心", "百貨公司", "商場"],
    "美食":   ["美食廣場", "市場(室內)", "商場"],
    "自然景觀": ["博物館", "展覽館", "水族館", "天文館", "室內植物園"],
    "_default": ["博物館", "美術館", "展覽館", "購物中心", "百貨公司", "音樂廳", "文化中心"]
}

async def backup_search_candidates(
    request: Request,
    *,
    center_lat: float,
    center_lon: float,
    original_city: str,
    original_categories: List[str],
    user_lat: Optional[float],
    user_lon: Optional[float],
    pref_rating_threshold: float,
    activity_time: Dict[str, str],
    exclude_ids: Set[str],
    exclude_names: Set[str],
    run_seen_ids: Set[str],
    run_seen_names: Set[str],
    radius_m: int
) -> List[Dict[str, Any]]:
    google_api_key = request.app.state.google_api_key
    # 以第一個原始標籤挑對應關鍵字，否則用預設
    base_key = original_categories[0] if original_categories else "_default"
    keywords = INDOOR_KEYWORDS_BY_CATEGORY.get(base_key) or INDOOR_KEYWORDS_BY_CATEGORY["_default"]

    async def text_search(keyword: str) -> List[Dict[str, Any]]:
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {
            "key": google_api_key,
            "query": f"{keyword}",
            "location": f"{center_lat},{center_lon}",
            "radius": radius_m,
            "language": "zh-TW",
            "region": "tw",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params)
            data = r.json()
        return data.get("results", []) or []

    out: List[Dict[str, Any]] = []
    for kw in keywords:
        results = await text_search(kw)
        for res in results:
            name = res.get("name")
            if not name:
                continue

            verified = await get_verified_google_place(
                request, name, center_lat, center_lon,
                min_rating=pref_rating_threshold, relaxed=True, radius_m=radius_m
            )
            if not verified:
                continue

            pid = str(verified.get("place_id") or "")
            nm_norm = _norm_name(verified.get("name") or name)

            if pid and (pid in exclude_ids or pid in run_seen_ids):
                continue
            if nm_norm in exclude_names or nm_norm in run_seen_names:
                continue

            if not await is_indoor_place(request, verified["name"]):
                continue

            verified.setdefault("summary", f"{kw} 類型的室內景點。")
            verified.setdefault("reason",  f"符合下雨備援、室內、且鄰近 {original_city}。")

            scored = await score_candidate(
                request,
                verified,
                original_city=original_city,
                original_categories=original_categories,
                user_lat=user_lat,
                user_lon=user_lon,
                pref_rating_threshold=pref_rating_threshold,
                activity_time=activity_time
            )
            out.append(scored)

            if pid:
                run_seen_ids.add(pid)
            run_seen_names.add(nm_norm)

    return out

# =========================
# 主流程（多輪蒐集 + 備援 + 完整 log）
# =========================
@router.post("/recommend_alternative")
async def recommend_alternative(request: Request):
    """
    輸入 JSON：
    {
        "trip_id": "<forms._id 或 structured_itineraries._id>",
        "user_lat": <float | null>,
        "user_lon": <float | null>,
        "spot_name": "<原始景點名稱>"
    }
    """
    print("===== 開始推薦替代景點流程（計分排序版） =====")
    data = await request.json()
    trip_id = data.get("trip_id") or data.get("_id")
    user_lat = data.get("user_lat")
    user_lon = data.get("user_lon")
    spot_name = data.get("spot_name")

    # 1. 取得使用者偏好
    print("[流程] 取得使用者行程偏好...")
    pref_res = await get_user_preferences(request, trip_id)
    if isinstance(pref_res, JSONResponse):
        print("[流程] 錯誤：找不到行程偏好。")
        return JSONResponse(content={"success": False, "message": "找不到對應的行程偏好資料"})

    pref_city_hint = (pref_res.get("locations") or [None])[0]
    if pref_city_hint:
        print(f"[流程] 城市提示（from 偏好）：{pref_city_hint}")

    # 2. 原始景點定位（帶城市提示）
    print(f"[流程] 正在為原始景點 '{spot_name}' 進行地理編碼...")
    geocode_res = await geocode(request, spot_name, city_hint=pref_city_hint)
    if geocode_res.get("error"):
        print("[流程] 錯誤：原始景點無法進行地理編碼。")
        return JSONResponse(content={"success": False, "message": "無法找到原始景點位置，請稍後再試。"})

    spot_lat = geocode_res["lat"]
    spot_lon = geocode_res["lon"]
    original_city = await get_city_from_latlon(request, spot_lat, spot_lon)
    print(f"[流程] 原始景點所在城市：{original_city}")

    # 3. GPT 分析原始類別（多標籤）
    original_spot_categories = await get_attraction_categories(request, spot_name)
    print(f"[流程] 原始景點類別標籤：{original_spot_categories}")

    # 4. 產生 GPT 提示詞（維持你喜歡的完整 log）
    prompt_parts = [
        f"使用者偏好類別：{', '.join(pref_res['preferences']) or '（無）'}",
        f"曾瀏覽類別：{', '.join(pref_res['browse_categories']) or '（無）'}",
        f"已收藏類別：{', '.join(pref_res['saved_categories']) or '（無）'}",
        f"原始景點 '{spot_name}' 屬於這些類別：{', '.join(original_spot_categories) or '（未知）'}。請推薦風格相近的景點。",
        f"地點：{original_city}，希望推薦適合下雨天的【室內景點】。",
        f"活動時間 {pref_res['activity_time'].get('start')}~{pref_res['activity_time'].get('end')}，Google 評分大於 {pref_res['google_rating']}。",
        "請以繁體中文，列出 10 個真實的景點，並為每個景點提供簡短的簡介和推薦理由，格式如下：",
        "1. 景點名稱",
        "   簡介：...",
        "   推薦依據：..."
    ]
    final_prompt_preview = "\n".join(prompt_parts)
    print("[流程] 產生 GPT 提示詞:\n" + final_prompt_preview)

    # 5. 歷史/行程去重集合
    hist_ex_ids, hist_ex_names = await _collect_exclusions(request, trip_id, spot_name)

    # 6. 多輪蒐集設定
    RADIUS_STEPS_M = [5000, 8000, 12000, 20000]
    collected: List[Dict[str, Any]] = []
    run_seen_ids: Set[str] = set()
    run_seen_names: Set[str] = set()
    seen_names_for_prompt: Set[str] = set(hist_ex_names)

    for round_idx, radius_m in enumerate(RADIUS_STEPS_M, start=1):
        # ✅ 修正：改成根據前端要求的數量 (max_results) 來決定何時停止
        if len(collected) >= data.get("max_results", 10): 
            break

        exclude_list_for_prompt = ", ".join(sorted({n for n in seen_names_for_prompt if n})) or "（無）"
        parts = prompt_parts + [f"請**不要**包含以下已看過或已存在的地點：{exclude_list_for_prompt}"]
        final_prompt = "\n".join(parts)
        print(f"[流程] Round {round_idx}/{len(RADIUS_STEPS_M)} 半徑 {radius_m/1000:.1f}km，送出 GPT 提示詞。")

        suggested_list = await generate_gpt_recommendations(request, final_prompt)

        print(f"[流程] 驗證與計分（本輪候選數：{len(suggested_list)}）...")
        for item in suggested_list:
            raw_name = item['name']
            info = await get_verified_google_place(
                request, raw_name, spot_lat, spot_lon,
                pref_res['google_rating'], relaxed=True, radius_m=radius_m
            )
            if not info:
                print(f"[流程 - 濾除] 景點 '{raw_name}'：Google 驗證失敗、關閉或今天公休。")
                seen_names_for_prompt.add(_norm_name(raw_name))
                continue

            pid = str(info.get("place_id") or "")
            nm_norm = _norm_name(info.get("name") or raw_name)

            if pid and (pid in hist_ex_ids or pid in run_seen_ids):
                print(f"[流程 - 濾除] '{info['name']}'：歷史/本次已存在（place_id）。")
                seen_names_for_prompt.add(nm_norm)
                continue
            if nm_norm in hist_ex_names or nm_norm in run_seen_names:
                print(f"[流程 - 濾除] '{info['name']}'：歷史/行程/本次重複（名稱）。")
                seen_names_for_prompt.add(nm_norm)
                continue

            if not await is_indoor_place(request, info['name']):
                print(f"[流程 - 濾除] '{info['name']}'：非室內場所。")
                seen_names_for_prompt.add(nm_norm)
                continue

            info["summary"] = item.get("summary") or "室內景點，適合作為雨備方案。"
            info["reason"] = item.get("reason") or "符合偏好與評分門檻。"

            scored = await score_candidate(
                request,
                info,
                original_city=original_city,
                original_categories=original_spot_categories,
                user_lat=user_lat,
                user_lon=user_lon,
                pref_rating_threshold=pref_res['google_rating'],
                activity_time=pref_res['activity_time']
            )
            collected.append(scored)
            if pid:
                run_seen_ids.add(pid)
            run_seen_names.add(nm_norm)
            seen_names_for_prompt.add(nm_norm)
            print(f"[流程 - 計分] {scored['name']} 分數={scored['score']}")

        if len(collected) < 3:
            print(f"[流程] Round {round_idx} 不足三筆，啟動備援搜尋（半徑 {radius_m/1000:.1f}km）...")
            backup = await backup_search_candidates(
                request,
                center_lat=spot_lat, center_lon=spot_lon,
                original_city=original_city,
                original_categories=original_spot_categories,
                user_lat=user_lat, user_lon=user_lon,
                pref_rating_threshold=pref_res['google_rating'],
                activity_time=pref_res['activity_time'],
                exclude_ids=hist_ex_ids, exclude_names=hist_ex_names,
                run_seen_ids=run_seen_ids, run_seen_names=run_seen_names,
                radius_m=radius_m
            )
            collected.extend(backup)

    if not collected:
        print("[流程] 錯誤：所有推薦景點均未通過條件或被去重。")
        return JSONResponse(content={"success": False, "message": "未能找到符合條件的替代景點。"})

    # 7. 排序 + 去重
    collected.sort(key=lambda x: (
        -(x.get("score", 0)),
        (x.get("distance", float('inf')) if x.get("distance") is not None else float('inf')),
        -(x.get("rating", 0) or 0)
    ))
    unique_sorted = []
    seen_pid_final, seen_name_final = set(), set()
    for c in collected:
        pid = str(c.get("place_id") or "")
        nm = _norm_name(c.get("name"))
        if pid and pid in seen_pid_final:
            continue
        if nm in seen_name_final:
            continue
        seen_name_final.add(nm)
        if pid:
            seen_pid_final.add(pid)
        unique_sorted.append(c)

    # 8. 根據前端請求數量回傳 (如果沒傳 max_results 則預設 10)
    # ⚠️ 注意：這裡我們直接取 data (request json) 裡的 max_results
    target_count = data.get("max_results", 10)
    
    # 先取前 N 筆
    top = unique_sorted[:target_count]
    
    # 如果數量不足 target_count (且還沒達到 Google API 的物理極限)，嘗試備援
    # 這裡我們設定一個合理的備援觸發門檻，例如如果連 3 筆都不到，或是使用者要求更多但我們不夠
    if len(top) < target_count and len(top) < 20: 
        print(f"[流程] 排序後僅 {len(top)} 筆 (目標 {target_count})，進行最後補齊流程...")
        final_backup = await backup_search_candidates(
            request,
            center_lat=spot_lat, center_lon=spot_lon,
            original_city=original_city,
            original_categories=original_spot_categories,
            user_lat=user_lat, user_lon=user_lon,
            pref_rating_threshold=pref_res['google_rating'],
            activity_time=pref_res['activity_time'],
            exclude_ids=hist_ex_ids, exclude_names=hist_ex_names,
            run_seen_ids=run_seen_ids, run_seen_names=run_seen_names,
            radius_m=25000 # 擴大半徑到 25km 找最後機會
        )
        
        if final_backup:
            # 合併原有結果與備援結果
            merged = unique_sorted + final_backup
            
            # 重新排序 (分數高 -> 距離近 -> 評分高)
            merged.sort(key=lambda x: (
                -(x.get("score", 0)),
                (x.get("distance", float('inf')) if x.get("distance") is not None else float('inf')),
                -(x.get("rating", 0) or 0)
            ))
            
            # 重新去重
            uniq2, spid2, sname2 = [], set(), set()
            for c in merged:
                pid = str(c.get("place_id") or "")
                nm = _norm_name(c.get("name"))
                if pid and pid in spid2: 
                    continue
                if nm in sname2:
                    continue
                sname2.add(nm)
                if pid: spid2.add(pid)
                uniq2.append(c)
            
            # ✅ 修正：截取到 target_count (不再是 3)
            top = uniq2[:target_count]
            
    # 9. 補照片與今日營業字串
    try:
        google_api_key = request.app.state.google_api_key
        for r in top:
            if r.get("place_id") and not r.get("photo_url"):
                photo_url = await get_place_photo_url(google_api_key, r["place_id"], maxwidth=720)
                if photo_url:
                    r["photo_url"] = photo_url
    except Exception as e:
        print(f"[Photos] 批次補照片失敗: {e}")

    now = datetime.now(pytz.timezone('Asia/Taipei'))
    for r in top:
        weekday_text = (r.get('opening_hours') or {}).get('weekday_text', ['無資料'])
        r['opening_hours_text'] = weekday_text[now.weekday()] if len(weekday_text) > now.weekday() else '無資料'

    print("===== 推薦流程結束，準備回傳結果（至少三筆） =====")
    return JSONResponse(content={
        "success": True,
        "recommendations": top
    })

# --- 🔽🔽 4. 【修改輔助函式 (回傳 openingHours)】 🔽🔽 ---

async def _get_google_place_details(request: Request, place_id: str) -> Dict[str, Any]:
    """
    (輔助函式) 呼叫 Google Places Details API 獲取營業狀態
    """
    print(f"\n=== _get_google_place_details 開始 ===")
    print(f"Place ID: {place_id}")
    
    google_api_key = request.app.state.google_api_key
    if not google_api_key:
        print("[ERROR] Google API Key 未設定")
        return {"open_now": False, "status_text": "未設定 Google API Key", "opening_hours": []}
    
    print(f"Google API Key (前10碼): {google_api_key[:10]}...")

    detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
    detail_params = {
        "place_id": place_id,
        "key": google_api_key,
        "fields": "opening_hours,business_status",
        "language": "zh-TW"
    }
    
    print(f"請求 URL: {detail_url}")
    print(f"請求參數: place_id={place_id}, fields=opening_hours,business_status")
    
    status_text = "營業狀態未知"
    open_now = False
    opening_hours_list: List[str] = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(detail_url, params=detail_params)
            
            print(f"Google API 回應狀態碼: {res.status_code}")
            print(f"Google API 回應內容 (前 300 字): {res.text[:300]}")
            
            res.raise_for_status()
            full_data = res.json()
            
            print(f"Google API 回應 status: {full_data.get('status')}")
            
            # 檢查 Google API 的 status
            if full_data.get("status") != "OK":
                error_msg = full_data.get("error_message", "未知錯誤")
                print(f"[ERROR] Google API 回傳非 OK 狀態: {full_data.get('status')}")
                print(f"[ERROR] 錯誤訊息: {error_msg}")
                return {
                    "open_now": False, 
                    "status_text": f"Google API 錯誤: {full_data.get('status')} - {error_msg}", 
                    "opening_hours": []
                }
            
            data = full_data.get("result", {})
            print(f"result 物件內容: {data}")
        
        business_status = (data.get("business_status") or "UNKNOWN").upper()
        open_hours = data.get("opening_hours", {})
        weekday_text = open_hours.get("weekday_text", [])
        opening_hours_list = weekday_text
        
        print(f"business_status: {business_status}")
        print(f"weekday_text 數量: {len(weekday_text)}")
        
        if business_status == "OPERATIONAL":
            # 檢查是否「今天公休」
            if is_closed_today_by_weekday_text(weekday_text):
                status_text = "今日公休"
                open_now = False
                print(f"判斷結果: 今日公休")
            # 檢查是否「目前營業中」
            elif open_hours.get("open_now") is not None:
                open_now = open_hours.get("open_now", False)
                status_text = weekday_text[datetime.now(pytz.timezone('Asia/Taipei')).weekday()] if weekday_text else "營業中"
                # 簡化文字
                if ":" in status_text:
                    status_text = status_text.split(":", 1)[1].strip()
                print(f"判斷結果: open_now={open_now}, status_text={status_text}")
            else:
                # 雖然 OPERATIONAL 但沒有 open_now 資訊
                status_text = "營業中 (時間未知)"
                open_now = True
                print(f"判斷結果: 營業中但無時間資訊")
        
        elif business_status == "CLOSED_TEMPORARILY":
            status_text = "暫停營業"
            print(f"判斷結果: 暫停營業")
        elif business_status == "CLOSED_PERMANENTLY":
            status_text = "已永久歇業"
            print(f"判斷結果: 永久歇業")
        else:
            status_text = "營業狀態不明"
            print(f"判斷結果: 狀態不明 (business_status={business_status})")

    except httpx.HTTPStatusError as e:
        print(f"[ERROR] HTTP 錯誤: {e.response.status_code}")
        print(f"[ERROR] 回應內容: {e.response.text}")
        status_text = f"Google API 錯誤: {e.response.status_code}"
    except Exception as e:
        print(f"[ERROR] 未知錯誤: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        status_text = "查詢營業狀態失敗"

    result = {"open_now": open_now, "status_text": status_text, "opening_hours": opening_hours_list}
    print(f"=== _get_google_place_details 結束，回傳: {result} ===\n")
    return result

async def _get_open_weather(request: Request, lat: float, lon: float) -> Dict[str, Any]:
    """
    (輔助函式) 呼叫 OpenWeatherMap 獲取天氣
    """
    weather_key = request.app.state.openweather_api_key
    if not weather_key:
        return {"desc": "未設定 Weather API Key", "temp": 0, "pop": 0}

    # 使用 3.0 的 One Call API (如果可用) 或 2.5 的 forecast
    forecast_url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={weather_key}&units=metric&lang=zh_tw"
    
    desc = "天氣未知"
    temp = 0
    pop = 0 # 降雨機率 (Probability of precipitation)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(forecast_url)
            res.raise_for_status()
            data = res.json()
        
        if data.get("list"):
            # 取得未來 1-3 小時內的天氣預報
            first_forecast = data["list"][0]
            weather_data = first_forecast.get("weather", [{}])[0]
            
            desc = weather_data.get("description", "無描述")
            temp = int(round(first_forecast.get("main", {}).get("temp", 0)))
            pop = int(round(first_forecast.get("pop", 0) * 100)) # pop 是 0-1 的小數

    except httpx.HTTPStatusError as e:
        print(f"Error fetching OpenWeather: {e}")
        desc = f"Weather API 錯誤: {e.response.status_code}"
    except Exception as e:
        print(f"Error processing OpenWeather: {e}")
        desc = "查詢天氣失敗"

    return {"desc": desc, "temp": temp, "pop": pop}


# --- 🔽🔽 5. 【修改 get_start_info 函式】 🔽🔽 ---
@router.post("/startInfo")
async def get_start_info(
    payload: StartInfoRequest,
    request: Request
):
    print(f"===== 收到 /startInfo 請求 for placeId: {payload.placeId} =====")
    
    try:
        google_task = _get_google_place_details(request, payload.placeId)
        weather_task = _get_open_weather(request, payload.lat, payload.lng)
        
        results = await asyncio.gather(google_task, weather_task)
        
        google_result = results[0]
        weather_result = results[1]
        
        # 如果 Place ID 失效，嘗試用座標搜尋
        if "NOT_FOUND" in google_result.get("status_text", ""):
            print(f"[WARNING] Place ID 失效，嘗試用座標搜尋替代方案")
            # 這裡可以呼叫 Nearby Search 來找最近的景點
            # 或者只回傳天氣資訊，讓前端處理

    except Exception as e:
        print(f"[/startInfo] 併發 API 呼叫時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        
        error_weather = WeatherInfo(
            summary="查詢失敗",
            temperatureC=0,
            rainProbability=None
        )
        error_response = StartInfo(
            placeId=payload.placeId,
            weather=error_weather,
            openNow=False,
            openStatusText="查詢失敗",
            openingHours=[]
        )
        return JSONResponse(content=error_response.model_dump(by_alias=True))

    # 2. 組合回傳資料 (使用 Pydantic 模型)
    try:
        final_weather = WeatherInfo(
            summary=weather_result.get("desc", "未知"),
            temperatureC=int(weather_result.get("temp", 0)),
            rainProbability=int(weather_result.get("pop", 0)) if weather_result.get("pop") is not None else None
        )
        
        final_response = StartInfo(
            placeId=payload.placeId,
            weather=final_weather,
            openNow=google_result.get("open_now", False),
            openStatusText=google_result.get("status_text", "營業狀態未知"),
            alternatives=[], 
            page=0,
            openingHours=google_result.get("opening_hours", [])
        )
        
        print(f"===== /startInfo 回傳: {final_response.model_dump(by_alias=True)} =====")
        
        return JSONResponse(content=final_response.model_dump(by_alias=True))
        
    except Exception as e:
        print(f"[/startInfo] 組合回應時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        
        error_weather = WeatherInfo(
            summary="資料格式錯誤",
            temperatureC=0,
            rainProbability=None
        )
        error_response = StartInfo(
            placeId=payload.placeId,
            weather=error_weather,
            openNow=False,
            openStatusText="資料處理失敗",
            openingHours=[]
        )
        return JSONResponse(content=error_response.model_dump(by_alias=True))

@router.get("/test-openweather")
async def test_openweather_connection(request: Request):
    """測試 OpenWeather API 連接"""
    api_key = request.app.state.openweather_api_key
    
    print(f"\n=== 測試 OpenWeather API ===")
    print(f"API Key (前10碼): {api_key[:10]}...")
    print(f"API Key 長度: {len(api_key)}")
    
    if not api_key:
        return {"status": "ERROR", "message": "OpenWeather API Key 未設定"}
    
    if len(api_key) != 32:
        return {"status": "ERROR", "message": f"API Key 長度錯誤: {len(api_key)} (應為 32)"}
    
    # 測試台北天氣
    test_lat, test_lon = 25.0330, 121.5654
    test_url = f"https://api.openweathermap.org/data/2.5/weather?lat={test_lat}&lon={test_lon}&appid={api_key}&units=metric&lang=zh_tw"
    
    print(f"請求 URL: {test_url[:80]}...")
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(test_url)
            
            print(f"回應狀態碼: {res.status_code}")
            
            if res.status_code == 401:
                print(f"錯誤回應: {res.text}")
                return {
                    "status": "ERROR", 
                    "message": "API Key 無效 (401 Unauthorized)",
                    "response": res.text
                }
            
            if res.status_code == 429:
                print(f"錯誤回應: {res.text}")
                return {
                    "status": "ERROR", 
                    "message": "API 呼叫次數超過限制 (429 Too Many Requests)",
                    "response": res.text
                }
            
            res.raise_for_status()
            data = res.json()
            
            print(f"成功取得天氣資料: {data.get('weather', [{}])[0].get('description')}")
            
            return {
                "status": "SUCCESS",
                "message": "✅ OpenWeather API 連接正常",
                "api_key_prefix": api_key[:10] + "...",
                "test_location": "台北 (25.0330, 121.5654)",
                "temperature": data.get("main", {}).get("temp"),
                "feels_like": data.get("main", {}).get("feels_like"),
                "description": data.get("weather", [{}])[0].get("description"),
                "humidity": data.get("main", {}).get("humidity"),
                "wind_speed": data.get("wind", {}).get("speed"),
                "raw_response": data
            }
            
    except httpx.TimeoutException:
        print("錯誤: 連接超時")
        return {"status": "ERROR", "message": "連接超時 (Timeout)"}
    
    except httpx.HTTPStatusError as e:
        print(f"HTTP 錯誤: {e.response.status_code}")
        print(f"錯誤回應: {e.response.text}")
        return {
            "status": "ERROR", 
            "message": f"HTTP 錯誤: {e.response.status_code}",
            "response": e.response.text
        }
    
    except Exception as e:
        print(f"未知錯誤: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "status": "ERROR", 
            "message": f"未知錯誤: {str(e)}"
        }
    
@router.get("/find-place-id")
async def find_valid_place_id(request: Request, query: str = "台北101"):
    """
    動態搜尋景點並回傳最新的 Place ID
    """
    google_api_key = request.app.state.google_api_key
    
    # 使用 Text Search 搜尋景點
    search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "key": google_api_key,
        "language": "zh-TW"
    }
    
    print(f"\n=== 搜尋景點: {query} ===")
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(search_url, params=params)
            
            print(f"Text Search 回應狀態碼: {res.status_code}")
            
            res.raise_for_status()
            data = res.json()
            
            print(f"Text Search status: {data.get('status')}")
            
            if data.get("status") != "OK":
                return {
                    "success": False,
                    "error": f"搜尋失敗: {data.get('status')}",
                    "response": data
                }
            
            results = data.get("results", [])
            if not results:
                return {
                    "success": False,
                    "error": "找不到景點"
                }
            
            # 取第一個結果
            place = results[0]
            place_id = place.get("place_id")
            name = place.get("name")
            location = place.get("geometry", {}).get("location", {})
            
            print(f"找到景點: {name}")
            print(f"Place ID: {place_id}")
            print(f"座標: {location}")
            
            return {
                "success": True,
                "place_id": place_id,
                "name": name,
                "lat": location.get("lat"),
                "lng": location.get("lng"),
                "formatted_address": place.get("formatted_address"),
                "full_response": place
            }
            
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "error": str(e)
        }