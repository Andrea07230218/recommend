# routes/recommend.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List
from datetime import datetime

from core import mongo
# 直接用各個節點，不再依賴 run_itinerary_planner
from core.langgraph_nodes import (
    extract_profile,
    analyze_preferences,
    generate_daily_slots,
    validate_plan_with_llms,
    assemble_markdown,
    return_plan,
    to_locations_list,   # 解析 locations（字串/陣列 → 正規化陣列）
)

router = APIRouter()

class RecommendInput(BaseModel):
    user_id: str
    form: Dict[str, Any]

@router.post("/recommend")
def recommend(payload: RecommendInput):
    user_id = payload.user_id
    form = payload.form or {}

    # --- 1) 正規化 locations 成 array（支援逗號字串或已是陣列） ---
    raw_locations = form.get("locations") or form.get("地點") or form.get("location") or ""
    locations_arr: List[str] = to_locations_list(raw_locations)
    if not locations_arr:
        raise HTTPException(status_code=400, detail="請至少提供一個旅遊城市")
    form["locations"] = locations_arr  # 寫回給流程使用

    # --- 2) 先把表單存進 forms（locations 為陣列） ---
    forms_col = mongo.db["forms"]
    form_doc = {
        "user_id": user_id,
        "created_at": datetime.utcnow(),
        "form": form,                    # 保留完整原表單
        "locations": locations_arr,      # ✅ 所有旅遊城市（array）
        "days_count": int(form.get("旅遊天數") or form.get("days") or 1),
        "start_date": form.get("旅遊日期") or form.get("start_date") or None,
    }
    form_id = forms_col.insert_one(form_doc).inserted_id

    # --- 3) 呼叫規劃節點（沿用我們改好的多城市流程） ---
    state = {"user_id": user_id, "form": form}
    state = extract_profile(state)
    state = analyze_preferences(state)
    state = generate_daily_slots(state)
    state = validate_plan_with_llms(state)
    state = assemble_markdown(state)
    plan = return_plan(state)

    # --- 4) 組 structured_itineraries，每天都要存 city ---
    itin_json = plan.get("itinerary_json", {})
    days_list: List[Dict[str, Any]] = itin_json.get("days", [])

    days_for_db: List[Dict[str, Any]] = []
    for i, d in enumerate(days_list, start=1):
        days_for_db.append({
            "day": i,
            "date": d.get("date"),
            "city": d.get("city"),          # ✅ 關鍵：逐日城市
            "slots": d.get("slots", []),    # 保留 slot 結構
        })

    itins_col = mongo.db["structured_itineraries"]
    itin_doc = {
        "user_id": user_id,
        "form_id": form_id,
        "created_at": datetime.utcnow(),
        "title": form.get("行程名稱") or "未命名行程",
        "locations": locations_arr,
        # "per_day_city": itin_json.get("per_day_city", {}),
        "start_date": itin_json.get("start_date"),
        "days": days_for_db,                 # ✅ 每一天獨立存
        "summary": plan.get("summary", ""),
        "html": plan.get("html", ""),
        "used_places": plan.get("used_places", []),
    }
    itin_id = itins_col.insert_one(itin_doc).inserted_id

    # --- 5) 回傳（與前端相容） ---
    return {
        "ok": True,
        "form_id": str(form_id),
        "itinerary_id": str(itin_id),
        "summary": plan.get("summary", ""),
        "html": plan.get("html", ""),
        "itinerary_json": itin_json,
    }
