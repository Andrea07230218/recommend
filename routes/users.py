from fastapi import APIRouter, Response
from pymongo import MongoClient
from bson.json_util import dumps

router = APIRouter()

client = MongoClient("mongodb+srv://anne:1218@cluster0.g54wj9s.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
db = client["tripDemo-shan"]
users_collection = db["users"]

@router.get("/all")
def get_all_users():
    users = list(users_collection.find({}, {"_id": 0}))
    return Response(content=dumps(users), media_type="application/json")
