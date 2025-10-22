# scripts/migrate_locations_and_days_city.py
from core import mongo
from core.langgraph_nodes import to_locations_list, parse_day_city_plan_from_notes, norm_city
from datetime import datetime

db = mongo.db

def migrate_forms():
    forms = db.forms.find({})
    for f in forms:
        form = f.get("form", {}) or {}
        raw = form.get("locations") or form.get("地點") or form.get("location") or ""
        arr = to_locations_list(raw)
        if not arr and isinstance(raw, str) and raw.strip():
            arr = to_locations_list(raw)

        changed = False
        if f.get("locations") != arr and arr:
            db.forms.update_one({"_id": f["_id"]}, {"$set": {"locations": arr}})
            changed = True

        # 也把 form 內部的 locations 改成 array（保持一致）
        if form.get("locations") != arr and arr:
            form["locations"] = arr
            db.forms.update_one({"_id": f["_id"]}, {"$set": {"form": form}})
            changed = True

        if changed:
            print("forms updated:", f["_id"], arr)

def migrate_itineraries():
    itins = db.structured_itineraries.find({})
    for it in itins:
        days = it.get("days", [])
        changed = False
        for i, d in enumerate(days):
            if "city" not in d or not d.get("city"):
                # 嘗試從 per_day_city 或 title/notes 推導
                per_day = it.get("per_day_city", {})
                city = None
                if per_day:
                    city = per_day.get(str(d.get("day"))) or per_day.get(d.get("day"))
                if not city and it.get("locations"):
                    # 保底：用行程 locations 依序
                    locs = it["locations"]
                    if isinstance(locs, list) and len(locs) > 0:
                        city = locs[(d.get("day", i+1)-1) % len(locs)]
                if city:
                    d["city"] = norm_city(city)
                    changed = True
        if changed:
            db.structured_itineraries.update_one({"_id": it["_id"]}, {"$set": {"days": days}})
            print("itinerary days.city patched:", it["_id"])

if __name__ == "__main__":
    migrate_forms()
    migrate_itineraries()
