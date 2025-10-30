# main.py (合併版)
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import httpx
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


load_dotenv()

# --- 1. 讀取環境變數 ---
MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("DB_NAME", "tripDemo-shan")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")  # 👈 這個是全域變數
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# --- Debug: 啟動時印出 API Key 資訊 ---
print(f"[Init] GOOGLE_API_KEY loaded: {GOOGLE_API_KEY[:20] if GOOGLE_API_KEY else 'None'}... (length: {len(GOOGLE_API_KEY) if GOOGLE_API_KEY else 0})")

# --- 2. 匯入 routers ---
from routes.auth import router as auth_router, ensure_user_indexes
from routes.preference import router as preference_router
from routes.trip import router as trip_router
from routes.recommendation import router as recommendation_router
from routes.replace import router as replace_router
from routes.log import router as log_router
from routes.visit_check import router as visit_check_router
from routes.recommend import router as old_recommend_router
from routes.users import router as old_user_router

# --- 3. 建立 Mongo 連線和模板 ---
client: AsyncIOMotorClient = AsyncIOMotorClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000
)
db = client[DB_NAME]
templates = Jinja2Templates(directory="templates")

# --- 4. Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("FastAPI application startup...")
    
    try:
        await client.admin.command("ping")
    except Exception as e:
        raise RuntimeError(f"[MongoDB 連線失敗] {e}")
    
    # ✅ 直接使用全域變數，不要重複宣告
    app.state.db = db
    app.state.client = client
    app.state.templates = templates
    app.state.google_api_key = GOOGLE_API_KEY  # 👈 使用全域變數
    app.state.openweather_api_key = OPENWEATHER_API_KEY
    app.state.openai_api_key = OPENAI_API_KEY
    app.include_router(recommendation_router, tags=["(新) 推薦 (含雨備)"])
    
    await ensure_user_indexes(app)
    
    print("[Startup] MongoDB Atlas 連線 OK，users 索引就緒。")
    print(f"[Startup] Google API Key in app.state: {app.state.google_api_key[:20] if app.state.google_api_key else 'None'}...")
    
    yield
    
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

# --- 8. 註冊路由 ---
app.include_router(auth_router, tags=["(新) 認證 & 使用者"])
app.include_router(preference_router, tags=["(新) 偏好表單"])
app.include_router(trip_router, tags=["(新) 行程管理"])
app.include_router(replace_router, tags=["(新) 替換景點"])
app.include_router(log_router, tags=["(新) Log"])
app.include_router(visit_check_router, tags=["(新) 抵達檢查 (ETA)"])
app.include_router(recommendation_router, tags=["(新) 推薦 (含雨備)"])
app.include_router(old_recommend_router, tags=["(舊) 推薦行程"])
app.include_router(old_user_router, prefix="/users", tags=["(舊) 使用者"])

# --- 9. 天氣 API ---
# @app.get("/weather")
# async def get_weather_for_location(
#     lat: float = Query(..., description="緯度"),
#     lon: float = Query(..., description="經度")
# ):
#     print(f"\n=== /weather endpoint called ===")
#     print(f"[DEBUG] lat={lat}, lon={lon}")
#     print(f"[DEBUG] app.state.openweather_api_key exists: {hasattr(app.state, 'openweather_api_key')}")
    
#     if hasattr(app.state, 'openweather_api_key') and app.state.openweather_api_key:
#         print(f"[DEBUG] OPENWEATHER_API_KEY (first 10 chars): {app.state.openweather_api_key[:10]}...")
#     else:
#         print(f"[DEBUG] OPENWEATHER_API_KEY is None or not set")
    
#     if not app.state.openweather_api_key:
#         print("[ERROR] Weather API key not configured in app.state")
#         raise HTTPException(status_code=500, detail="Weather API key not configured")
    
#     url = "https://api.openweathermap.org/data/2.5/weather"
#     params = {
#         "lat": lat,
#         "lon": lon,
#         "appid": app.state.openweather_api_key,
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
#                 "raw": weather_data
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
#             import traceback
#             traceback.print_exc()
#             raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# --- 10. HTML 頁面 ---
@app.get("/form/personal", include_in_schema=False)
def show_form(request: Request):
    return templates.TemplateResponse("recommend_ui.html", {"request": request})

@app.get("/form/group", include_in_schema=False)
def show_group_form(request: Request):
    return templates.TemplateResponse("group_form.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
async def show_login_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def show_register(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/trips", response_class=HTMLResponse)
async def show_trips(request: Request):
    if not request.cookies.get("user_id"):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("trips.html", {"request": request})

# python -m uvicorn main:app --reload --env-file .env
