# core/google_maps.py
from __future__ import annotations
import os
import re
import math
import requests
import json
from datetime import datetime, time, timedelta, date
from typing import List, Dict, Any, Optional, Tuple, Sequence

from core.place_filters import BAN_QUICK_STOPS, scrub_quick_stops, is_quick_stop
from pathlib import Path
from dotenv import load_dotenv, find_dotenv
ROOT = Path(__file__).resolve().parents[1]
candidates = [ROOT/".env", ROOT/".env.local", Path.cwd()/".env"]
for p in candidates:
    if p.exists():
        load_dotenv(p, override=False)
fd = find_dotenv(usecwd=True)
if fd:
    load_dotenv(fd, override=False)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
if not GOOGLE_API_KEY:
    tried = [str(p) for p in candidates] + ([fd] if fd else [])
    raise RuntimeError(f"缺少 GOOGLE_API_KEY，請在 .env 或環境變數設定；已嘗試載入：{tried}")

print("GMAPS key 前 8 碼：", GOOGLE_API_KEY[:8], "（長度）", len(GOOGLE_API_KEY))

# === New API bases & helpers ===
PLACES_V1_BASE = "https://places.googleapis.com/v1"
ROUTES_V2_BASE = "https://routes.googleapis.com"

def _g_header_fieldmask(mask: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": mask
    }

def _g_header_places() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "*"
    }

# ✅ 修正：正確映射 Places API (New) 的欄位（駝峰命名）
def _map_places_v1_to_legacy(p: Dict[str, Any]) -> Dict[str, Any]:
    loc = p.get("location") or {}
    lat = loc.get("latitude")
    lng = loc.get("longitude")

    opening = p.get("currentOpeningHours") or {}
    periods = []
    for per in opening.get("periods", []) or []:
        o = per.get("open", {})
        c = per.get("close", {})

        def _fmt_time(x: Dict[str, Any]) -> Optional[str]:
            if not x: 
                return None
            hh = x.get("hour")
            mm = x.get("minute", 0)
            if hh is None:
                return None
            return f"{int(hh):02d}{int(mm):02d}"

        od = (o.get("day") or 0) % 7
        cd = (c.get("day") or 0) % 7
        open_time = _fmt_time(o) or "0000"
        close_time = _fmt_time(c) or "2359"

        periods.append({
            "open":  {"day": od, "time": open_time},
            "close": {"day": cd, "time": close_time}
        })

    # ✅ 修正：Places API (New) 使用駝峰命名
    formatted_addr = (p.get("formattedAddress") 
                     or p.get("shortFormattedAddress")
                     or p.get("formatted_address"))  # 舊版兼容
    
    display_name = ((p.get("displayName") or {}).get("text") 
                   or p.get("name"))

    return {
        "place_id": p.get("id") or p.get("place_id"),
        "name": display_name,
        "formatted_address": formatted_addr,
        "vicinity": p.get("vicinity") or formatted_addr,  # ✅ 加入 vicinity
        "geometry": {"location": {"lat": lat, "lng": lng}} if lat and lng else {},
        "rating": p.get("rating", 0.0),
        "user_ratings_total": p.get("userRatingCount") or p.get("user_ratings_total") or 0,
        "types": p.get("types", []),
        "opening_hours": {"periods": periods} if periods else None,
        "url": p.get("googleMapsUri") or p.get("url"),
        "google_maps_url": p.get("googleMapsUri") or p.get("url"),
        "photos": p.get("photos", []),
    }

# ✅ 餐時段關鍵字 & 類型 & 保底連鎖
SLOT_KEYWORDS = {
    "早餐":   ["早餐","早餐店","早午餐","豆漿","美而美","吐司","蛋餅","咖啡","燒餅","飯糰"],
    "中午":   ["午餐","餐廳","小吃","便當","牛肉麵","麵館","拉麵","簡餐","熱炒","自助餐"],
    "晚上":   ["晚餐","餐廳","夜市","燒烤","居酒屋","熱炒","滷味","宵夜"],
}
MEAL_SLOT_LABELS = {"早餐","中午","晚上"}

SLOT_TYPE_MAP = {
    "早餐":   ["cafe","bakery","restaurant"],
    "中午":   ["restaurant"],
    "晚上":   ["restaurant","bar"],
}
FALLBACK_CHAINS = ["星巴克","麥當勞","肯德基","頂呱呱","八方雲集","路易莎","Cama"]
FALLBACK_SNACK_CHAINS = ["清心福全","迷客夏","可不可","五十嵐","茶湯會","萬波","大苑子"]

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    to_rad = math.radians
    dlat = to_rad(lat2 - lat1)
    dlon = to_rad(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def build_map_url(place_id: Optional[str], lat: Optional[float], lng: Optional[float]) -> str:
    if place_id and (lat is not None) and (lng is not None):
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f}%2C{lng:.6f}&query_place_id={place_id}"
    if (lat is not None) and (lng is not None):
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f}%2C{lng:.6f}"
    return "https://maps.google.com/"

def build_directions_url(olat: float, olng: float, dlat: float, dlng: float, mode: str = "driving") -> str:
    m = "driving" if mode in ("drive", "driving", "car") else ("transit" if mode in ("transit", "public") else ("walking" if mode == "walking" else "driving"))
    return f"https://www.google.com/maps/dir/?api=1&origin={olat:.6f}%2C{olng:.6f}&destination={dlat:.6f}%2C{dlng:.6f}&travelmode={m}"

def geocode_city(city: str, region: str = "tw", language: str = "zh-TW") -> Optional[Tuple[float, float]]:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": city, "region": region, "language": language, "key": GOOGLE_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=12).json()
        res = r.get("results", [])
        if not res: return None
        loc = res[0]["geometry"]["location"]
        return (loc["lat"], loc["lng"])
    except Exception:
        return None

def _places_new_search_nearby(center_latlng: Tuple[float, float],
                              radius_m: int,
                              keyword: Optional[str] = None,
                              types: Optional[str] = None,
                              language: str = "zh-TW") -> List[Dict[str, Any]]:
    if keyword:
        return _places_new_search_text(
            query=keyword,
            center_latlng=center_latlng,
            radius_m=radius_m,
            language=language
        )

    url = f"{PLACES_V1_BASE}/places:searchNearby"
    body: Dict[str, Any] = {
        "languageCode": language,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": center_latlng[0], "longitude": center_latlng[1]},
                "radius": float(radius_m)
            }
        }
    }
    if types:
        body["includedTypes"] = [types]

    try:
        r = requests.post(url, headers=_g_header_places(), data=json.dumps(body), timeout=15)
        data = r.json()
        places = data.get("places", []) or []
        return [_map_places_v1_to_legacy(p) for p in places]
    except Exception as e:
        print("❌ places:searchNearby 失敗：", e)
        return []

def _places_new_search_text(query: str,
                            center_latlng: Optional[Tuple[float, float]] = None,
                            radius_m: int = 5000,
                            region_code: str = "TW",
                            language: str = "zh-TW") -> List[Dict[str, Any]]:
    url = f"{PLACES_V1_BASE}/places:searchText"
    body: Dict[str, Any] = {
        "textQuery": query,
        "languageCode": language,
        "regionCode": region_code
    }
    if center_latlng:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": center_latlng[0], "longitude": center_latlng[1]},
                "radius": float(radius_m)
            }
        }
    try:
        r = requests.post(url, headers=_g_header_places(), data=json.dumps(body), timeout=15)
        data = r.json()
        places = data.get("places", []) or []
        return [_map_places_v1_to_legacy(p) for p in places]
    except Exception as e:
        print("❌ places:searchText 失敗：", e)
        return []

def parse_time_str(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def format_google_hours(hours_obj: Optional[Dict[str, Any]]) -> str:
    if not hours_obj: return "未提供"
    txt = ""
    for p in hours_obj.get("periods", []):
        o = p.get("open", {}); c = p.get("close", {})
        od, ot = o.get("day"), o.get("time")
        cd, ct = c.get("day"), c.get("time")
        if ot and ct:
            txt += f"{od}-{cd} {ot}-{ct}\n"
    return txt.strip() or "未提供"

def _times_overlap(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return (a_start < b_end) and (b_start < a_end)

def _hhmm_to_time(s: str) -> time:
    return time(int(s[:2]), int(s[2:]))

def _python_weekday_to_google_day(py_wd: int) -> int:
    return (py_wd + 1) % 7

def is_open_during_slot(hours_obj: Optional[Dict[str, Any]], slot_range: Tuple[str, str], date_str: Optional[str] = None, require_full_cover: bool = False) -> bool:
    if not hours_obj or "periods" not in hours_obj:
        return True
    start_t, end_t = map(parse_time_str, slot_range)
    if date_str:
        dt = datetime.fromisoformat(date_str)
    else:
        dt = datetime.now()
    g_day = _python_weekday_to_google_day(dt.weekday())

    intervals: List[Tuple[time, time]] = []
    for p in hours_obj.get("periods", []):
        o, c = p.get("open"), p.get("close")
        if not o: continue
        if "time" not in o: continue
        o_day, o_time = o.get("day"), _hhmm_to_time(o["time"])
        if not c:
            intervals.append((time(0, 0), time(23, 59)))
            continue
        if "time" not in c: continue
        c_day, c_time = c.get("day"), _hhmm_to_time(c["time"])
        if o_day == g_day and c_day == g_day:
            intervals.append((o_time, c_time))
        elif o_day == g_day and c_day != g_day:
            intervals.append((o_time, time(23, 59)))
        elif o_day != g_day and c_day == g_day:
            intervals.append((time(0, 0), c_time))

    if not intervals:
        return True

    if require_full_cover:
        return any(a_start <= start_t and a_end >= end_t for (a_start, a_end) in intervals)
    else:
        return any(_times_overlap(a_start, a_end, start_t, end_t) for (a_start, a_end) in intervals)

PREFIXES = ["早餐：", "午餐：", "晚餐：", "下午茶：", "走訪", "參觀"]

def clean_place_name(name: str) -> str:
    for prefix in PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
    name = re.sub(r"[（(].*?[)）]", "", name)
    name = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", name)
    return name.strip()

def opening_line_for_date(hours_obj: Optional[Dict[str, Any]], date_str: str) -> str:
    if not hours_obj:
        return "未提供"
    try:
        dt = datetime.fromisoformat(date_str)
    except Exception:
        dt = datetime.now()
    g_day = _python_weekday_to_google_day(dt.weekday())
    for p in hours_obj.get("periods", []):
        o, c = p.get("open"), p.get("close")
        if not o or "time" not in o: continue
        if o.get("day") != g_day: continue
        ot = o["time"]; ct = (c or {}).get("time", "2359")
        return f"{ot[:2]}:{ot[2:]}–{ct[:2]}:{ct[2:]}"
    return "未提供"

# ✅ 修正：修正 search_place 的 debug 代碼
def search_place(name: str, location: str,
                 center_latlng: Optional[Tuple[float, float]] = None,
                 radius_m: int = 5000) -> Optional[Dict[str, Any]]:
    queries = [f"{name} {location}", f"{location} {name}", f"{name} {location} 台灣", name]
    for q in queries:
        results = _places_new_search_text(q, center_latlng=center_latlng, radius_m=radius_m)
        if results:
            print(f"✅ 查到：{results[0]['name']} ➜ 來源查詢：{q}")
            return results[0]
    
    # ✅ 修正：使用正確的變數名稱
    print(f"❌ search_place 失敗：name={name}, location={location}")
    return None

def search_meal_places(
    city: str,
    slot_label: str,
    date: str,
    start_hhmm: str,
    end_hhmm: str,
    center_latlng: Optional[Tuple[float, float]] = None,
    radius_m: int = 4500,
) -> List[Dict[str, Any]]:
    import math

    keywords = SLOT_KEYWORDS.get(slot_label, [])
    collected: List[Dict[str, Any]] = []
    if not center_latlng:
        center_latlng = geocode_city(city)

    def _quality_ok(rating: float, reviews: int) -> bool:
        if slot_label == "晚上":
            return rating >= 4.2 and reviews >= 120
        elif slot_label == "中午":
            return rating >= 4.0 and reviews >= 60
        elif slot_label == "早餐":
            return rating >= 3.9 and reviews >= 30
        else:
            return rating >= 4.0 and reviews >= 40

    def _push(pid: str):
        if pid in seen:
            return
        seen.add(pid)
        det = get_place_details(pid)
        if not det:
            return

        if is_quick_stop(det.get("name", ""), det.get("types", [])):
            return

        rating = float(det.get("rating", 0))
        reviews = int(det.get("user_ratings_total", 0))
        if not _quality_ok(rating, reviews):
            return

        if is_open_during_slot(
            det.get("opening_hours"),
            (f"{start_hhmm[:2]}:{start_hhmm[2:]}", f"{end_hhmm[:2]}:{end_hhmm[2:]}"),
            date_str=date,
            require_full_cover=False,
        ):
            loc = (det.get("geometry") or {}).get("location") or {}
            url = det.get("url") or build_map_url(det.get("place_id"), loc.get("lat"), loc.get("lng"))
            collected.append({
                "place_id": det.get("place_id"),
                "name": det.get("name"),
                "formatted_address": det.get("formatted_address"),
                "vicinity": det.get("vicinity"),  # ✅ 加入
                "location": loc,
                "rating": rating,
                "user_ratings_total": reviews,
                "types": det.get("types", []),
                "opening_hours": det.get("opening_hours"),
                "google_maps_url": url,
                "photos": det.get("photos", []),
            })

    def _diversify_by_grid(items: List[Dict[str, Any]], cell_m: int = 500, limit_per_cell: int = 2) -> List[Dict[str, Any]]:
        if not items:
            return items
        items_sorted = sorted(items, key=lambda x: (-(x.get("rating") or 0), -(x.get("user_ratings_total") or 0)))
        out: List[Dict[str, Any]] = []
        bucket_count: Dict[Tuple[int, int], int] = {}
        for p in items_sorted:
            loc = p.get("location") or {}
            lat = loc.get("lat"); lng = loc.get("lng")
            if lat is None or lng is None:
                key = ("none", "none")
            else:
                m_per_deg_lat = 111_320.0
                m_per_deg_lng = 111_320.0 * max(0.1, math.cos(math.radians(float(lat))))
                gx = int(float(lng) / (cell_m / m_per_deg_lng))
                gy = int(float(lat) / (cell_m / m_per_deg_lat))
                key = (gx, gy)
            if bucket_count.get(key, 0) >= limit_per_cell:
                continue
            bucket_count[key] = bucket_count.get(key, 0) + 1
            out.append(p)
        return out

    seen = set()

    type_list = SLOT_TYPE_MAP.get(slot_label, ["restaurant"])
    if center_latlng:
        for t in type_list:
            for kw in keywords:
                raw = _places_new_search_nearby(center_latlng, radius_m, keyword=kw, types=t)
                for r in raw:
                    if r.get("place_id"):
                        _push(r["place_id"])
                if len(collected) >= 8:
                    break
            if len(collected) >= 8:
                break

    if len(collected) < 5:
        for kw in keywords:
            q = f"{kw} {city}"
            raw = _places_new_search_text(q, center_latlng=center_latlng, radius_m=radius_m)
            for r in raw:
                if r.get("place_id"):
                    _push(r["place_id"])
            if len(collected) >= 8:
                break

    collected = _diversify_by_grid(collected, cell_m=500, limit_per_cell=2)
    return collected[:8]

def _latlng_tuple(p: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    loc = ((p.get("geometry") or {}).get("location") or {})
    lat = p.get("lat", None) if p.get("lat") is not None else loc.get("lat")
    lng = p.get("lng", None) if p.get("lng") is not None else loc.get("lng")
    if lat is None or lng is None:
        return None
    return (float(lat), float(lng))

def _route_matrix_api(latlngs: Sequence[Tuple[float, float]], mode: str = "DRIVE") -> Optional[List[List[int]]]:
    if len(latlngs) <= 1:
        return [[0]]
    url = f"{ROUTES_V2_BASE}/distanceMatrix/v2:computeRouteMatrix"
    origins = [{"waypoint": {"location": {"latLng": {"latitude": a, "longitude": b}}}} for a, b in latlngs]
    destinations = [{"waypoint": {"location": {"latLng": {"latitude": a, "longitude": b}}}} for a, b in latlngs]
    body = {
        "origins": origins,
        "destinations": destinations,
        "travelMode": mode
    }
    try:
        r = requests.post(url, headers=_g_header_fieldmask("originIndex,destinationIndex,duration"), data=json.dumps(body), timeout=30)
        if r.status_code != 200:
            print("❌ RouteMatrix HTTP:", r.status_code, r.text[:200])
            return None
        lines = [json.loads(x) for x in r.text.strip().splitlines() if x.strip()]
        n = len(latlngs)
        mat = [[0]*n for _ in range(n)]
        for item in lines:
            oi = item.get("originIndex", 0)
            di = item.get("destinationIndex", 0)
            dur = item.get("duration", "0s")
            secs = int(dur.replace("s", "")) if isinstance(dur, str) and dur.endswith("s") else 0
            mat[oi][di] = max(secs, 1)
        return mat
    except Exception as e:
        print("❌ RouteMatrix 失敗：", e)
        return None

def _nearest_neighbor_order(dist: List[List[int]], start: int = 0) -> List[int]:
    n = len(dist); unvis = set(range(n)); order = [start]; unvis.remove(start); cur = start
    while unvis:
        nxt = min(unvis, key=lambda k: dist[cur][k])
        order.append(nxt); unvis.remove(nxt); cur = nxt
    return order

def _route_cost(dist: List[List[int]], order: List[int]) -> int:
    return sum(dist[order[i]][order[i+1]] for i in range(len(order)-1))

def _two_opt(dist: List[List[int]], order: List[int], max_iter: int = 200) -> List[int]:
    best = order[:]; best_cost = _route_cost(dist, best); n = len(order); improved = True; it = 0
    while improved and it < max_iter:
        improved = False; it += 1
        for i in range(1, n-2):
            for k in range(i+1, n-1):
                new_order = best[:i] + best[i:k+1][::-1] + best[k+1:]
                new_cost = _route_cost(dist, new_order)
                if new_cost < best_cost:
                    best, best_cost = new_order, new_cost; improved = True
        if n <= 4: break
    return best

def _suggest_mode_by_span(latlngs: Sequence[Tuple[float, float]]) -> str:
    if len(latlngs) <= 1: return "walking"
    max_d = 0.0
    for i in range(len(latlngs)):
        for j in range(i+1, len(latlngs)):
            d = haversine_km(latlngs[i][0], latlngs[i][1], latlngs[j][0], latlngs[j][1])
            if d > max_d: max_d = d
    return "walking" if max_d <= 2.5 else "driving"

def travel_time_minutes(origin: Dict[str, float] | Tuple[float,float],
                        dest: Dict[str, float] | Tuple[float,float],
                        mode: str="driving") -> int:
    def _to_tuple(x):
        if isinstance(x, (tuple, list)): return (float(x[0]), float(x[1]))
        if isinstance(x, dict): return (float(x.get("lat")), float(x.get("lng")))
        return None
    a = _to_tuple(origin); b = _to_tuple(dest)
    if a is None or b is None:
        return 999

    travel_mode = ("DRIVE" if mode == "driving" else ("TRANSIT" if mode == "transit" else ("WALK" if mode == "walking" else "DRIVE")))

    url = f"{ROUTES_V2_BASE}/directions/v2:computeRoutes"
    body = {
        "origin": {"location": {"latLng": {"latitude": a[0], "longitude": a[1]}}},
        "destination": {"location": {"latLng": {"latitude": b[0], "longitude": b[1]}}},
        "travelMode": travel_mode,
        "transitPreferences": {"routingPreference": "LESS_WALKING"},
    }
    try:
        r = requests.post(
            url,
            headers=_g_header_fieldmask("routes.duration,routes.distanceMeters"),
            data=json.dumps(body),
            timeout=12
        )
        if r.status_code != 200:
            raise RuntimeError(f"computeRoutes HTTP {r.status_code}")
        data = r.json()
        routes = data.get("routes", []) or []
        if routes:
            dur = routes[0].get("duration", "0s")
            secs = int(dur.replace("s", "")) if isinstance(dur, str) and dur.endswith("s") else 0
            if secs > 0:
                return int(round(secs/60))
    except Exception:
        pass
    km = haversine_km(a[0], a[1], b[0], b[1])
    return int(round(km * (2 if mode == "driving" else 12)))

def optimize_visit_order(places: List[Dict[str, Any]], start_idx: int = 0, mode: Optional[str] = None) -> Dict[str, Any]:
    pts = []; idx_map = []
    for i, p in enumerate(places):
        ll = _latlng_tuple(p)
        if ll is None: continue
        pts.append(ll); idx_map.append(i)
    if len(pts) <= 2:
        return {"order": list(range(len(places))), "total_travel_secs": 0, "mode": mode or "walking"}

    m = mode or _suggest_mode_by_span(pts)
    route_mode = "WALK" if m == "walking" else "DRIVE"
    dist = _route_matrix_api(pts, mode=route_mode)

    if dist is None:
        speed = 4.0 if m == "walking" else 30.0
        total = 0
        for i in range(len(pts)-1):
            total += int((haversine_km(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]) / speed) * 3600)
        return {"order": list(range(len(places))), "total_travel_secs": total, "mode": m}

    order = _nearest_neighbor_order(dist, start_idx)
    order = _two_opt(dist, order)
    total = _route_cost(dist, order)
    return {"order": order, "total_travel_secs": total, "mode": m}

def annotate_travel_minutes(cands: List[Dict[str, Any]],
                            anchor: Dict[str,float] | Tuple[float,float],
                            mode: str="driving") -> List[Dict[str,Any]]:
    def _loc(c):
        loc = (c.get("location") or ((c.get("geometry") or {}).get("location") or {}))
        if "lat" in loc and "lng" in loc:
            return (float(loc["lat"]), float(loc["lng"]))
        return None
    if isinstance(anchor, dict):
        anc = (float(anchor.get("lat")), float(anchor.get("lng")))
    else:
        anc = (float(anchor[0]), float(anchor[1]))
    scored = []
    for c in cands:
        ll = _loc(c)
        if not ll:
            c["travel_minutes_from_anchor"] = 999
            continue
        km = haversine_km(anc[0], anc[1], ll[0], ll[1])
        c["_rough_km"] = km
        scored.append((km, c))
    scored.sort(key=lambda x: x[0])
    half = max(1, len(scored)//2)
    for _, c in scored[:half]:
        ll = (c.get("location") or ((c.get("geometry") or {}).get("location") or {}))
        c["travel_minutes_from_anchor"] = travel_time_minutes(
            {"lat": anc[0], "lng": anc[1]},
            {"lat": float(ll["lat"]), "lng": float(ll["lng"])},
            mode
        )
    for _, c in scored[half:]:
        c["travel_minutes_from_anchor"] = int(round(c["_rough_km"] * (2 if mode == "driving" else 12)))
    return [c for _, c in scored]

def nearby_filter(cands: List[Dict[str,Any]],
                  anchor: Dict[str,float] | Tuple[float,float],
                  mode: str,
                  max_leg_minutes: int) -> List[Dict[str,Any]]:
    cands = annotate_travel_minutes(cands, anchor, mode)
    return [c for c in cands if c.get("travel_minutes_from_anchor", 999) <= max_leg_minutes]

def search_attraction_candidates(city: str,
                                 center_latlng: Optional[Tuple[float,float]],
                                 radius_m: int = 2000,
                                 keywords: Optional[List[str]] = None,
                                 limit: int = 12,
                                 min_rating: float = 4.0,
                                 min_reviews: int = 50,
                                 date: Optional[str] = None,
                                 slot_range: Optional[Tuple[str,str]] = None,
                                 require_full_cover: bool = False,
                                 mode: str = "driving") -> List[Dict[str,Any]]:
    if not center_latlng:
        center_latlng = geocode_city(city) or (None, None)
    results: List[Dict[str,Any]] = []
    seen = set()
    kw_list = keywords or [city, "景點", "博物館", "公園", "步道", "藝文", "老街"]
    for kw in kw_list:
        raw = _places_new_search_nearby(center_latlng, radius_m, keyword=kw, types="tourist_attraction")
        for r in raw:
            pid = r.get("place_id")
            if not pid or pid in seen: continue
            seen.add(pid)
            det = get_place_details(pid)
            if not det: continue
            rating = float(det.get("rating", 0))
            rc = int(det.get("user_ratings_total", 0))
            if rating < min_rating or rc < min_reviews: continue
            if slot_range:
                if not is_open_during_slot(det.get("opening_hours"), slot_range, date_str=date, require_full_cover=require_full_cover):
                    continue
            loc = (det.get("geometry") or {}).get("location") or {}
            url = det.get("url") or build_map_url(pid, loc.get("lat"), loc.get("lng"))
            results.append({
                "place_id": pid,
                "name": det.get("name"),
                "formatted_address": det.get("formatted_address"),
                "vicinity": det.get("vicinity"),  # ✅ 加入
                "location": loc,
                "rating": rating,
                "user_ratings_total": rc,
                "types": det.get("types", []),
                "opening_hours": det.get("opening_hours"),
                "google_maps_url": url,
                "photos": det.get("photos", []),
            })
            if len(results) >= limit: break
        if len(results) >= limit: break
    if center_latlng[0] is not None:
        results = annotate_travel_minutes(results, {"lat":center_latlng[0], "lng":center_latlng[1]}, mode)
    return results[:limit]

def search_store_candidates(keyword: str,
                            city: str,
                            center_latlng: Optional[Tuple[float,float]] = None,
                            radius_m: int = 1200,
                            types: Optional[List[str]] = None,
                            limit: int = 12,
                            min_rating: float = 4.2,
                            min_reviews: int = 100,
                            date: Optional[str] = None,
                            slot_range: Optional[Tuple[str,str]] = None,
                            require_full_cover: bool = False,
                            mode: str = "driving") -> List[Dict[str,Any]]:
    if not center_latlng:
        center_latlng = geocode_city(city)
    if not center_latlng:
        return []
    results: List[Dict[str,Any]] = []
    seen = set()
    tps = types or ["restaurant","cafe","bakery"]
    for t in tps:
        raw = _places_new_search_nearby(center_latlng, radius_m, keyword=keyword, types=t)
        for r in raw:
            pid = r.get("place_id")
            if not pid or pid in seen: continue
            seen.add(pid)
            det = get_place_details(pid)
            if not det: continue
            rating = float(det.get("rating", 0))
            rc = int(det.get("user_ratings_total", 0))
            if rating < min_rating or rc < min_reviews: continue
            if slot_range and not is_open_during_slot(det.get("opening_hours"), slot_range, date_str=date, require_full_cover=require_full_cover):
                continue
            loc = (det.get("geometry") or {}).get("location") or {}
            url = det.get("url") or build_map_url(pid, loc.get("lat"), loc.get("lng"))
            results.append({
                "place_id": pid,
                "name": det.get("name"),
                "formatted_address": det.get("formatted_address"),
                "vicinity": det.get("vicinity"),  # ✅ 加入
                "location": loc,
                "rating": rating,
                "user_ratings_total": rc,
                "types": det.get("types", []),
                "opening_hours": det.get("opening_hours"),
                "google_maps_url": url,
                "photos": det.get("photos", []),
            })
            if len(results) >= limit: break
        if len(results) >= limit: break
    if len(results) < limit:
        q = f"{keyword} {city}"
        raw = _places_new_search_text(q, center_latlng=center_latlng, radius_m=radius_m)
        for r in raw:
            pid = r.get("place_id")
            if not pid or pid in seen: continue
            seen.add(pid)
            det = get_place_details(pid)
            if not det: continue
            rating = float(det.get("rating", 0))
            rc = int(det.get("user_ratings_total", 0))
            if rating < min_rating or rc < min_reviews: continue
            if slot_range and not is_open_during_slot(det.get("opening_hours"), slot_range, date_str=date, require_full_cover=require_full_cover):
                continue
            loc = (det.get("geometry") or {}).get("location") or {}
            url = det.get("url") or build_map_url(pid, loc.get("lat"), loc.get("lng"))
            results.append({
                "place_id": pid,
                "name": det.get("name"),
                "formatted_address": det.get("formatted_address"),
                "vicinity": det.get("vicinity"),  # ✅ 加入
                "location": loc,
                "rating": rating,
                "user_ratings_total": rc,
                "types": det.get("types", []),
                "opening_hours": det.get("opening_hours"),
                "google_maps_url": url,
                "photos": det.get("photos", []),
            })
            if len(results) >= limit: break
    results = annotate_travel_minutes(results, {"lat":center_latlng[0], "lng":center_latlng[1]}, mode)
    return results[:limit]

# 在 get_place_details 函式之前加入（約第 590 行）
def build_photo_url_from_photos(photos: List[Dict], maxwidth: int = 720) -> Optional[str]:
    if not photos or not isinstance(photos, list) or len(photos) == 0:
        return None
    
    try:
        # Places API (New) 格式
        photo_name = photos[0].get("name")
        if photo_name:
            return f"https://places.googleapis.com/v1/{photo_name}/media?maxWidthPx={maxwidth}&key={GOOGLE_API_KEY}"
        
        # Legacy API fallback
        photo_ref = photos[0].get("photo_reference")
        if photo_ref:
            return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth={maxwidth}&photo_reference={photo_ref}&key={GOOGLE_API_KEY}"
    except (IndexError, AttributeError, TypeError):
        return None
    
    return None

def get_place_details(place_id: str) -> Dict[str, Any]:
    url = f"{PLACES_V1_BASE}/places/{place_id}"
    params = {"languageCode": "zh-TW"}
    headers = _g_header_places()
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            print(f"❌ Place Details(New) HTTP {r.status_code} place_id={place_id}")
            return {}
        
        result = _map_places_v1_to_legacy(r.json())
        
        # ✅ 新增：自動生成第一張照片的 URL
        if result.get("photos"):
            result["photo_url"] = build_photo_url_from_photos(result["photos"])
        
        return result
    except Exception as e:
        print(f"❌ Place Details(New) 失敗 place_id={place_id} err={e}")
        return {}