# 檔案： main.py (已修正路由，並將天氣 API 恢復註解)
from fastapi import FastAPI, Request, Query, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import httpx
import os
import traceback
from datetime import datetime
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from routes.recommendation import router as recommendation_router
import uvicorn

load_dotenv()

# --- 1. 讀取環境變數 ---
MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("DB_NAME", "tripDemo-shan")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

print(f"[Init] GOOGLE_API_KEY loaded: {GOOGLE_API_KEY[:20] if GOOGLE_API_KEY else 'None'}...")
print(f"[Init] OPENWEATHER_API_KEY loaded: {OPENWEATHER_API_KEY[:20] if OPENWEATHER_API_KEY else 'None'}...")
print(f"[Init] OPENAI_API_KEY loaded: {OPENAI_API_KEY[:20] if OPENAI_API_KEY else 'None'}...")

# --- 2. 匯入 routers (已清理) ---
# 這些是您專案中其他的新路由
from routes.auth import router as auth_router, ensure_user_indexes
from routes.preference import router as preference_router
from routes.trip import router as trip_router
from routes.log import router as log_router
from routes.visit_check import router as visit_check_router

# ✅ 這是我們剛剛修正的兩個主要路由檔案
from routes.recommend import router as recommend_router
from routes.replace import router as replace_router

# --- 3. 建立 Mongo 連線和模板 ---
client: AsyncIOMotorClient = AsyncIOMotorClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000
)
db = client[DB_NAME]
templates = Jinja2Templates(directory="templates")

# --- 4. Lifespan (已清理) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("FastAPI application startup...")
    
    try:
        await client.admin.command("ping")
    except Exception as e:
        raise RuntimeError(f"[MongoDB 連線失敗] {e}")
    
    # 將所有資源存入 app.state
    app.state.db = db
    app.state.client = client
    app.state.templates = templates
    app.state.google_api_key = GOOGLE_API_KEY
    app.state.openweather_api_key = OPENWEATHER_API_KEY
    app.state.openai_api_key = OPENAI_API_KEY
    
    # 執行索引檢查
    await ensure_user_indexes(app)
    
    print("[Startup] MongoDB Atlas 連線 OK，users 索引就緒。")
    print(f"[Startup] Google API Key in app.state: {app.state.google_api_key[:20] if app.state.google_api_key else 'None'}...")
    print(f"[Startup] OpenWeather API Key in app.state: {app.state.openweather_api_key[:20] if app.state.openweather_api_key else 'None'}...")
    print(f"[Startup] OpenAI API Key in app.state: {app.state.openai_api_key[:20] if app.state.openai_api_key else 'None'}...")
    
    yield # <--- 應用程式在這裡運行
    
    print("FastAPI application shutdown.")
    client.close()
    print("[Shutdown] MongoDB client closed.")

# --- 5. 建立 FastAPI app ---
app = FastAPI(
    title="[合併] 旅遊行程推薦系統 API",
    description="整合 GPT、Google Maps、使用者偏好、收藏、登入、行程管理、天氣與景點替換。",
    version="2.0.0",
    lifespan=lifespan
)

# --- 6. CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 7. 全局錯誤處理 ---
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print(f"🔥🔥🔥 Pydantic Validation Error Caught! 🔥🔥🔥")
    print(f"Request: {request.method} {request.url.path}")
    print(f"Validation Errors:\n{exc.errors()}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )

# --- 8. 註冊路由 (已清理) ---
app.include_router(auth_router, tags=["(新) 認證 & 使用者"])
app.include_router(preference_router, tags=["(新) 偏好表單"])
app.include_router(trip_router, tags=["(新) 行程管理"])
app.include_router(log_router, tags=["(新) Log"])
app.include_router(visit_check_router, tags=["(新) 抵達檢查 (ETA)"])

# ✅ 載入我們修正後的「推薦」和「替換」路由
app.include_router(
    recommend_router,
    tags=["(核心) 推薦 & 查詢 (Recommend & Query)"],
    prefix="" # 處理 /, /trip/{id}, /api/recommendations, /alternatives
)
app.include_router(
    replace_router,
    tags=["(核心) 替換景點 (Replace)"],
    prefix="" # 處理 /trips/{trip_id}/replace
)

app.include_router(recommendation_router, tags=["(新) 推薦 (含雨備)"])
# --- 9. 天氣 API (保持使用者原本的註解狀態) ---
# @app.get("/weather", tags=["(新) 工具"])
# async def get_weather_for_location(
#     request: Request, # 👈 加上 request 來存取 app.state
#     lat: float = Query(..., description="緯度"),
#     lon: float = Query(..., description="經度")
# ):
#     print(f"\n=== /weather endpoint called ===")
#     print(f"[DEBUG] lat={lat}, lon={lon}")
    
#     # ✅ 從 app.state 獲取 API key
#     api_key = request.app.state.openweather_api_key
    
#     if not api_key:
#         print("[ERROR] Weather API key not configured in app.state")
#         raise HTTPException(status_code=500, detail="Weather API key not configured")
    
#     print(f"[DEBUG] OPENWEATHER_API_KEY (first 10 chars): {api_key[:10]}...")
    
#     url = "https://api.openweathermap.org/data/2.5/weather"
#     params = {
#         "lat": lat,
#         "lon": lon,
#         "appid": api_key,
#         "units": "metric",
#         "lang": "zh_tw"
#     }
    
#     print(f"[DEBUG] Requesting: {url}")
#     print(f"[DEBUG] Params: {params}")
    
#     async with httpx.AsyncClient() as client:
#         try:
#             response = await client.get(url, params=params)
#             print(f"[DEBUG] Response status: {response.status_code}")
            
#             response.raise_for_status()
#             weather_data = response.json()
            
#             print(f"[DEBUG] Weather data: {weather_data.get('weather', [{}])[0].get('description', 'N/A')}")
            
#             result = {
#                 "temperature": weather_data.get("main", {}).get("temp"),
#                 "description": weather_data.get("weather", [{}])[0].get("description", "N/A"),
#                 "raw": weather_data # 方便 App 未來擴充
#             }
#             print(f"[SUCCESS] Returning weather data: temp={result['temperature']}, desc={result['description']}")
#             return result
            
#         except httpx.HTTPStatusError as e:
#             print(f"[ERROR] HTTPStatusError: {e.response.status_code}")
#             print(f"[ERROR] Response text: {e.response.text}")
#             raise HTTPException(
#                 status_code=e.response.status_code, 
#                 detail=f"Error fetching weather: {e.response.text}"
#             )
#         except Exception as e:
#             print(f"[ERROR] Unexpected error: {type(e).__name__}: {str(e)}")
#             traceback.print_exc()
#             raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# --- 10. HTML 頁面 (保持不變) ---
@app.get("/form/personal", include_in_schema=False)
def show_form(request: Request):
    return templates.TemplateResponse("recommend_ui.html", {"request": request})

@app.get("/form/group", include_in_schema=False)
def show_group_form(request: Request):
    return templates.TemplateResponse("group_form.html", {"request": request})

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def show_login_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def show_register(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/trips", response_class=HTMLResponse, include_in_schema=False)
async def show_trips(request: Request):
    if not request.cookies.get("user_id"):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("trips.html", {"request": request})

# --- 11. 啟動伺服器 ---
if __name__ == "__main__":
    print("--- 準備啟動 Uvicorn 伺服器 (main:app) ---")
    
    # ⚠️ 提醒：使用 "0.0.0.0" 才能讓您的實體手機透過 Wi-Fi IP (例如 192.168.x.x) 連線
    
    uvicorn.run(
        "main:app",  # 指向這個檔案 (main.py) 中的 app 物件
        host="0.0.0.0", 
        port=8000,
        reload=True  # 開發模式：程式碼變更時自動重啟
    )

# python -m uvicorn main:app --reload --env-file .env
