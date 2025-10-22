# routes/recommend.py
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from markdown2 import markdown as md
from bson import SON
import re  # ✅ for to_locations_list

from models.schemas import RecommendRequest, RecommendGroupRequest

from core.group_merge import merge_group_preferences
from core.mongo import (
    users_collection,
    get_user,
    save_form,
    save_structured_linked_itinerary,
    form_collection,
)
from core import mongo

from core.langgraph_nodes import (
    extract_profile,
    analyze_preferences,
    generate_daily_slots,
    validate_plan_with_llms,
    assemble_markdown,
    return_plan,
)

router = APIRouter()

# 欄位固定順序（personal 轉這組）
GROUP_FORM_FIELD_ORDER = [
    "leader_id", "members", "trip_name",
    "date", "days",
    "locations", "time_range", "preferences",
    "exclude", "notes"
]

DEFAULTS = {
    "leader_id": "",
    "members": [],
    "trip_name": "",
    "date": "",
    "days": 0,
    "locations": [],        # ✅ 一律用陣列
    "time_range": "",
    "preferences": [],
    "exclude": [],
    "notes": "",
}

# ---------- Policy：硬性禁用（手搖／速食／超商等） ----------
_BANNED_TERMS = [
    # 類別/通用詞
    "手搖飲", "手搖", "飲料店", "茶飲", "珍珠奶茶", "珍奶",
    "連鎖速食", "速食", "超商", "便利商店",
    # 品牌/常見別名（增加 LLM/關鍵字命中率）
    "7-11", "7 eleven", "7-eleven", "7 Eleven", "全家", "FamilyMart",
    "星巴克", "Starbucks",
    "可不可", "Kebuke", "清心", "清心福全", "CoCo", "50嵐", "50lan", "迷客夏", "Milksha",
    "再睡5分鐘", "康青龍", "茶湯會", "COMEBUY", "Chatime", "日出茶太", "麻古", "Macu",
    "麥當勞", "McDonald", "McDonald’s", "肯德基", "KFC"
]

def _merge_exclude(user_exclude: list[str] | None) -> list[str]:
    """將使用者 exclude 與政策禁用詞合併去重。"""
    base = set()
    for x in (user_exclude or []):
        x = str(x).strip()
        if x:
            base.add(x)
    for x in _BANNED_TERMS:
        base.add(x)
    # 盡量把較通用的類別詞放前面
    ordered = []
    head = ["手搖飲", "連鎖速食", "速食", "超商", "便利商店", "飲料店", "珍珠奶茶", "珍奶"]
    for k in head:
        if k in base:
            ordered.append(k)
    for k in sorted(base):
        if k not in ordered:
            ordered.append(k)
    return ordered

# ---------- helpers ----------

def _to_locations_list(value) -> list[str]:
    """把地點欄位正規化為陣列：支援 list / 逗號或頓號分隔字串 / None。"""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(x).strip() for x in value]
    else:
        s = str(value)
        s = re.sub(r"[、，]", ",", s)
        items = [x.strip() for x in s.split(",")]
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out

def _clean_str_list(value):
    """將輸入正規化成『純字串陣列』：去頭尾空白、移除空項。可接受 None / str / list。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,\u3001，/|\s]+", value)]
    elif isinstance(value, list):
        parts = [str(p).strip() for p in value]
    else:
        return []
    return [p for p in parts if p]

def to_locations_list(val):
    """locations 正規化為陣列。"""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [p.strip() for p in re.split(r"[,、，\s]+", val) if p.strip()]
    return []

def _canonize_form_order(d: dict) -> SON:
    """依 GROUP_FORM_FIELD_ORDER 產生保序 SON；缺欄位補 DEFAULTS。"""
    ordered, seen = [], set()

    def _value(key):
        if key in d:
            v = d[key]
            return DEFAULTS.get(key) if v is None else v
        return DEFAULTS.get(key)

    for k in GROUP_FORM_FIELD_ORDER:
        ordered.append((k, _value(k))); seen.add(k)

    for k, v in d.items():
        if k not in seen:
            if v is None and k in DEFAULTS:
                v = DEFAULTS[k]
            ordered.append((k, v))
    return SON(ordered)

def _shape_result_for_frontend(result: dict, trip_name: str = "") -> dict:
    """整理輸出給前端使用，附帶 trip_name。"""
    itinerary_html = result.get("html") or result.get("itinerary_html") or ""
    itinerary_md = result.get("markdown") or result.get("itinerary_raw") or ""
    if not itinerary_html and itinerary_md:
        itinerary_html = md(itinerary_md)

    analysis_html = result.get("analysis_html") or (
        md(result.get("summary") or itinerary_md) if (result.get("summary") or itinerary_md) else ""
    )
    places = result.get("used_places") or result.get("places") or []

    ij = result.get("itinerary_json") or {}
    loc_arr = result.get("locations")
    if not isinstance(loc_arr, list):
        loc_arr = ij.get("locations")
    loc_arr = loc_arr if isinstance(loc_arr, list) else []

    loc_txt = result.get("locations_text") or ij.get("locations_text") or "、".join(loc_arr)

    shaped = {
        "analysis_html": analysis_html,
        "itinerary_html": itinerary_html,
        "html": itinerary_html,
        "markdown": itinerary_md,
        "places": places,
        "used_places": places,
        "missing_places": result.get("missing_places", []),
        "locations": loc_arr,
        "locations_text": loc_txt,
        "days": result.get("days", 1),
        "summary": result.get("summary", ""),
        "error": False,
        "error_message": "",
    }
    shaped["trip_name"] = (trip_name or result.get("trip_name") or "").strip()
    return shaped

def _shape_personal_form_to_group_format(raw_form: dict, user_id: str) -> dict:
    def pick(d: dict, *keys, default=None):
        for k in keys:
            if k in d and d.get(k) is not None:
                return d.get(k)
        return default

    trip_name  = pick(raw_form, "trip_name", "行程名稱", default=DEFAULTS["trip_name"])
    date       = pick(raw_form, "date", "旅遊日期", default=DEFAULTS["date"])
    days       = pick(raw_form, "days", "旅遊天數", "n_days", default=DEFAULTS["days"])
    locations  = pick(raw_form, "locations", "地點", default=DEFAULTS["locations"])
    time_range = pick(raw_form, "time_range", "活動時間", default=DEFAULTS["time_range"])

    preferences = _clean_str_list(pick(raw_form, "preferences", "偏好", "偏好類型", default=DEFAULTS["preferences"]))
    exclude     = _clean_str_list(pick(raw_form, "exclude", "避開", "避開條件", default=DEFAULTS["exclude"]))
    notes       = pick(raw_form, "notes", "備註", default=DEFAULTS["notes"])

    return {
        "leader_id": user_id or DEFAULTS["leader_id"],
        "members": [{"user_id": user_id}] if user_id else DEFAULTS["members"],
        "trip_name": trip_name if trip_name is not None else DEFAULTS["trip_name"],
        "date": date if date is not None else DEFAULTS["date"],
        "days": days if days is not None else DEFAULTS["days"],
        "locations": locations if locations is not None else DEFAULTS["locations"],
        "time_range": time_range if time_range is not None else DEFAULTS["time_range"],
        "preferences": preferences if preferences is not None else DEFAULTS["preferences"],
        "exclude": exclude if exclude is not None else DEFAULTS["exclude"],
        "notes": notes if notes is not None else DEFAULTS["notes"],
    }

def _run_planner(user_id: str, form_payload: dict) -> dict:
    """以 langgraph_nodes 的節點逐步執行。"""
    state = {"user_id": user_id, "form": form_payload}
    state = extract_profile(state)
    state = analyze_preferences(state)
    state = generate_daily_slots(state)
    state = validate_plan_with_llms(state)
    state = assemble_markdown(state)
    return return_plan(state)

def _persist_structured_itinerary(
    *,
    user_id: str,
    form_id: str | None,
    result: dict,
    title: str = "",
    fallback_locations: list[str] | None = None,
):
    """把結構化內容寫入 structured_itineraries。"""
    db = mongo.db
    itins = db["structured_itineraries"]

    itin_json = result.get("itinerary_json") or {}
    days_list = itin_json.get("days") or []

    days_for_db, all_nodes = [], []
    for i, d in enumerate(days_list, start=1):
        slots = d.get("slots", [])
        slot_nodes = []
        for s in slots:
            slot_nodes.append({
                "day": i,
                "slot": s.get("label"),
                "start": s["window"][0],
                "end": s["window"][1],
                "places": s.get("places", [])
            })
        if slot_nodes:
            from core.itinerary_linked_list import build_linked_list, flatten_linked
            head = build_linked_list(slot_nodes)
            flat = flatten_linked(head)
            all_nodes.extend(flat["nodes"])
            head_id = flat["head_id"]
        else:
            head_id = None

        days_for_db.append({
            "day": i,
            "date": d.get("date"),
            "city": d.get("city"),
            "head_id": head_id,
        })

    locations_from_result = itin_json.get("locations")
    if isinstance(locations_from_result, list):
        locations = [str(x).strip() for x in locations_from_result if str(x).strip()]
    elif isinstance(locations_from_result, str):
        locations = to_locations_list(locations_from_result)
    elif fallback_locations:
        locations = [str(x).strip() for x in fallback_locations if str(x).strip()]
    else:
        seen, locations = set(), []
        for d in days_for_db:
            c = (d.get("city") or "").strip()
            if c and c not in seen:
                seen.add(c); locations.append(c)

    doc = {
        "user_id": user_id,
        "form_id": form_id,
        "created_at": datetime.utcnow(),
        "title": (title or result.get("trip_name") or "未命名行程").strip(),
        "locations": locations,
        "start_date": itin_json.get("start_date"),
        "days": days_for_db,
        "nodes": all_nodes,
        "summary": result.get("summary", ""),
        "html": result.get("html", ""),
        "used_places": result.get("used_places", []),
    }
    itins.insert_one(doc)

# ---------- travel.mode 決策：優先 transportation，再看中文（僅判斷用，不入庫） ----------
def _decide_travel_mode(transportation: str | None, transport_text: str | None) -> str:
    t = (transportation or "").strip().lower()
    if t in {"drive", "driving", "car"}:
        return "driving"
    if t in {"public", "transit", "bus", "metro", "train"}:
        return "transit"
    s = (transport_text or "").strip().lower()
    if ("汽車" in s) or (s == "driving"):
        return "driving"
    if ("大眾" in s) or (s in {"transit", "public"}):
        return "transit"
    return "walking"

# ---------- planner flags（傳給規劃器/搜尋層的策略開關） ----------
def _planner_flags() -> dict:
    return {
        "ban_quick_stops": True,     # ✅ 禁用手搖／速食／超商
        "grid_diversity": True,      # ✅ 空間去重（降低同區重複）
        "dinner_min_reviews": 120,   # ✅ 晚餐提高評論門檻
        "dinner_min_rating": 4.2,    # ✅ 晚餐提高評分門檻
    }

# ----------------------------------------------------------------------

@router.post("/group", summary="團體推薦", description="主揪勾選偏好 + 成員收藏偏好")
async def recommend_group_trip(req: RecommendGroupRequest, request: Request):
    try:
        # 讀原始 body（避免 schema 忽略未知鍵），但『中文交通方式』只用來判斷，不入庫
        raw = await request.json()
        raw_transportation = (raw.get("transportation") or "").strip()
        raw_cn_transport = (raw.get("交通方式") or "").strip()

        # 1) 收集成員收藏
        user_ids = [m.user_id for m in (req.members or [])]
        favorites, missing_users = [], []
        for uid in user_ids:
            user = get_user(uid)
            if not user:
                missing_users.append(uid); continue
            if "favorites" in user and user["favorites"]:
                favorites.extend(user["favorites"])
        if missing_users:
            raise HTTPException(status_code=400, detail=f"找不到成員：{', '.join(missing_users)}")

        # 2) 合併偏好（先清理偏好/避開）+ 套用硬性禁用
        cleaned_preferences = _clean_str_list(req.preferences)
        cleaned_exclude = _clean_str_list(req.exclude)
        banned_exclude = _merge_exclude(cleaned_exclude)

        merged_preferences = merge_group_preferences(
            favorites=favorites,
            preferences=cleaned_preferences,
            exclude=banned_exclude,  # ✅ 把禁用一起帶入融合
            notes=req.notes or "",
        )

        # 3) 存問卷（固定欄位順序；只存 transportation，不存「交通方式」）
        created_at = datetime.utcnow()
        group_form_dict = {
            "leader_id": req.leader_id,
            "members": [{"user_id": m.user_id} for m in (req.members or [])],
            "trip_name": getattr(req, "trip_name", None),
            "date": req.date,
            "days": req.days,
            "locations": req.locations,
            "time_range": req.time_range,
            "preferences": cleaned_preferences,
            # ✅ 入庫也寫入禁用"exclude": banned_exclude,                     
            "notes": req.notes if req.notes is not None else DEFAULTS["notes"],
            "transportation": raw_transportation or None,  # ✅ 只存這個
        }

        locations_arr = to_locations_list(req.locations)
        if locations_arr:
            group_form_dict["locations"] = locations_arr

        save_form(req.leader_id, _canonize_form_order(group_form_dict), form_type="group", created_at=created_at)

        form_doc = form_collection.find_one({
            "user_id": req.leader_id,
            "created_at": created_at,
            "form_type": "group"
        })
        form_id = str(form_doc["_id"]) if form_doc else None

        # 4) travel.mode：優先 transportation，再看中文（僅判斷）
        travel_mode = _decide_travel_mode(raw_transportation, raw_cn_transport)

        # 5) 規劃行程（一次到位就帶 travel + planner flags + 避開條件）
        form_payload = {
            "locations": locations_arr or None,
            "旅遊日期": req.date,
            "活動時間": req.time_range,
            "旅遊天數": req.days,
            "偏好": merged_preferences,
            "避開條件": banned_exclude,           # ✅ 讓產生器 prompt 也看得到
            "planner": _planner_flags(),          # ✅ 傳策略給下游
            "form_type": "group",
            "created_at": created_at,
            "trip_preference_id": form_id,
            "travel": {
                "mode": travel_mode,
                "max_leg_minutes": 20,
                "search_radius_m": 1200
            }
        }
        result = _run_planner(user_id=req.leader_id, form_payload=form_payload)

        _persist_structured_itinerary(
            user_id=req.leader_id,
            form_id=form_id,
            result=result,
            title=(group_form_dict.get("trip_name") or "未命名行程"),
            fallback_locations=locations_arr
        )

        return _shape_result_for_frontend(result, trip_name=group_form_dict.get("trip_name"))

    except HTTPException:
        raise
    except Exception as e:
        import traceback, logging
        logging.getLogger("uvicorn.error").exception("Group recommend failed")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"團體推薦失敗：{str(e)}")

@router.post("", summary="產生推薦行程", description="根據使用者問卷與收藏紀錄，自動產生每日行程 markdown 與 HTML")
async def recommend_trip(req: RecommendRequest, request: Request):
    try:
        # 個人表單可能把 transportation 放在 form 或頂層；中文只做判斷不入庫
        raw = await request.json()
        raw_transportation = (raw.get("transportation")
                              or (raw.get("form") or {}).get("transportation")
                              or "").strip()
        raw_cn_transport = (raw.get("交通方式")
                            or (raw.get("form") or {}).get("交通方式")
                            or "").strip()

        user = users_collection.find_one({"username": req.user_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"找不到使用者 {req.user_id}")

        # 1) 規劃用表單：把收藏也當成一個偏好來源
        form = req.form.copy() if req.form else {}
        favorites = user.get("favorites", [])
        if favorites:
            form.setdefault("偏好", []).append({"source": "收藏", "類型": favorites})

        # ✅ 多城市正規化
        raw_locations = form.get("locations") or form.get("地點")
        locations_arr = to_locations_list(raw_locations)
        if locations_arr:
            form["locations"] = locations_arr

        # ✅ 個人表單的「避開」欄位 + 硬性禁用 合併
        user_exclude = _clean_str_list(form.get("exclude") or form.get("避開條件"))
        banned_exclude = _merge_exclude(user_exclude)
        form["避開條件"] = banned_exclude  # 供下游產生器直接食用

        # 2) 存問卷（只存 transportation）
        created_at = datetime.utcnow()
        group_like_form = _shape_personal_form_to_group_format(req.form or {}, req.user_id)
        if locations_arr:
            group_like_form["locations"] = locations_arr
        group_like_form["exclude"] = banned_exclude                      # ✅ 入庫也寫入禁用
        group_like_form["transportation"] = raw_transportation or None   # ✅ 只存這個

        save_form(req.user_id, _canonize_form_order(group_like_form), form_type="personal", created_at=created_at)

        form_doc = form_collection.find_one({
            "user_id": req.user_id,
            "created_at": created_at,
            "form_type": "personal"
        })
        form_id = str(form_doc["_id"]) if form_doc else None

        # 3) travel.mode：優先 transportation，再看中文（僅判斷）
        travel_mode = _decide_travel_mode(raw_transportation, (form.get("交通方式") or raw_cn_transport))

        # 4) 規劃行程（帶 travel + planner flags，一次完成）
        form_payload = {
            **form,
            "form_type": "personal",
            "created_at": created_at,
            "trip_preference_id": form_id,
            "planner": _planner_flags(),  # ✅ 傳策略給下游
            "travel": {
                "mode": travel_mode,
                "max_leg_minutes": 20,
                "search_radius_m": 1200
            }
        }
        result = _run_planner(user_id=req.user_id, form_payload=form_payload)

        _persist_structured_itinerary(
            user_id=req.user_id,
            form_id=form_id,
            result=result,
            title=(group_like_form.get("trip_name") or "未命名行程"),
            fallback_locations=locations_arr
        )

        return _shape_result_for_frontend(result, trip_name=group_like_form.get("trip_name"))

    except HTTPException:
        raise
    except Exception as e:
        import traceback, logging
        logging.getLogger("uvicorn.error").exception("Personal recommend failed")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"生成行程時發生錯誤：{str(e)}")
