from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# 修改為你的 MongoDB 連線字串
MONGO_URI = "mongodb+srv://anne:1218@cluster0.g54wj9s.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"  # 本機預設端口

try:
    # 建立連線
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)  # 設定 timeout 為 5 秒

    # 嘗試取得伺服器資訊來測試是否連線成功
    server_info = client.server_info()
    print("✅ 成功連線到 MongoDB！伺服器資訊如下：")
    print(server_info)

except ConnectionFailure as e:
    print("❌ 無法連線到 MongoDB：", e)
