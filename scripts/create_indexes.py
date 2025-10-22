# scripts/create_indexes.py
from core import mongo

db = mongo.db
db.forms.create_index([("user_id", 1), ("created_at", -1)])
db.forms.create_index([("locations", 1)])  # 查某城市的表單

db.structured_itineraries.create_index([("user_id", 1), ("created_at", -1)])
db.structured_itineraries.create_index([("locations", 1)])
db.structured_itineraries.create_index([("days.city", 1)])  # 查「某天在某城市」
