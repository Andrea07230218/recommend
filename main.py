# main.py (合併版)
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager # 👈 為了 lifespan
import httpx
import os
from dotenv import load_dotenv

# --- 1. 匯入 Mongo 和所有 API Keys ---
from motor.motor_asyncio import AsyncIOMotorClient # 👈 正確的非同步驅動
load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("DB_NAME", "tripDemo-shan") # 確保 .env 有這個
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


# --- 2. 匯入 *所有* 你需要的 routers ---

# 來自你的 "START按鈕" 專案 (routers.zip)
# ‼️‼️ 修正：全部改成從 'routes' 資料夾匯入 ‼️‼️
from routes.auth import router as auth_router, ensure_user_indexes
from routes.preference import router as preference_router
from routes.trip import router as trip_router
from routes.recommendation import router as recommendation_router
from routes.replace import router as replace_router
from routes.log import router as log_router
from routes.visit_check import router as visit_check_router

# 來自你 *原本* 的 "推薦系統" 專案
from routes.recommend import router as old_recommend_router
from routes.users import router as old_user_router

# --- 3. 建立 Mongo 連線 和 模板 ---
# (使用 'main_shan.py' 的方式，因為所有 router 都依賴它)
client: AsyncIOMotorClient = AsyncIOMotorClient( # 👈 正確的非同步 Client
    MONGODB_URI,
    serverSelectionTimeoutMS=10000
)
db = client[DB_NAME] # 👈 正確的非同步 DB 物件
templates = Jinja2Templates(directory="templates")


# --- 4. 合併的 Lifespan (管理啟動與關閉) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("FastAPI application startup...")
    # 進行 'main_shan.py' 的啟動工作
    try:
        await client.admin.command("ping")
    except Exception as e:
        raise RuntimeError(f"[MongoDB 連線失敗] {e}")

    # ‼️ 將 *所有* 共享資源放進 app.state
    app.state.db = db # 👈 把非同步 db 放進 state
    app.state.client = client # 👈 把非同步 client 放進 state
    app.state.templates = templates
    app.state.google_api_key = GOOGLE_API_KEY
    app.state.openweather_api_key = OPENWEATHER_API_KEY
    app.state.openai_api_key = OPENAI_API_KEY

    # 執行 'auth.py' 需要的索引
    await ensure_user_indexes(app)
    print("[Startup] MongoDB Atlas 連線 OK，users 索引就緒。")

    yield

    # 關閉
    print("FastAPI application shutdown.")
    client.close()
    print("[Shutdown] MongoDB client closed.")


# --- 5. 建立 FastAPI app (使用 lifespan) ---
app = FastAPI(
    title="[合併] 旅遊行程推薦系統 API",
    description="整合 GPT、Google Maps、使用者偏好、收藏、登入、行程管理、天氣與景點替換。",
    version="2.0.0", # 升級版
    lifespan=lifespan # 👈 啟用 lifespan
)

# --- 6. CORS (來自 'main_recommend.py') ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 7. 全局錯誤處理 (來自 'main_recommend.py') ---
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    捕捉所有 Pydantic 模型驗證失敗的錯誤 (RequestValidationError)。
    """
    print(f"🔥🔥🔥 Pydantic Validation Error Caught! 🔥🔥🔥")
    print(f"Request: {request.method} {request.url.path}")
    print(f"Validation Errors:\n{exc.errors()}")

    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )


# --- 8. 註冊 *所有* 路由 ---

# 來自 "START按鈕" (routers.zip)
# (注意：我沒有加 prefix，這樣 /login 就在根目錄)
app.include_router(auth_router, tags=["(新) 認證 & 使用者"])
app.include_router(preference_router, tags=["(新) 偏好表單"])
app.include_router(trip_router, tags=["(新) 行程管理"])
app.include_router(replace_router, tags=["(新) 替換景點"])
app.include_router(log_router, tags=["(新) Log"])
app.include_router(visit_check_router, tags=["(新) 抵達檢查 (ETA)"])
app.include_router(recommendation_router, tags=["(新) 推薦 (含雨備)"])

# 來自你 "原本的" main.py (routes 資料夾)
# (我保留了你原本的 prefix，並更新 tag 以便區分)
app.include_router(old_recommend_router, prefix="/recommend", tags=["(舊) 推薦行程"])
app.include_router(old_user_router, prefix="/users", tags=["(舊) 使用者"])


# --- 9. 天氣 API (來自 'main_recommend.py') ---
# 這個 /weather 是獨立的，和你 routers/recommendation.py 裡的 /check_weather 不衝突
@app.get("/weather")
async def get_weather_for_location(
    lat: float = Query(..., description="緯度"),
    lon: float = Query(..., description="經度")
):
    """
    獲取指定經緯度的即時天氣資訊 (代理 OpenWeatherMap)
    """
    # ‼️ 改用 app.state 來讀取 API Key，確保一致性
    if not app.state.openweather_api_key:
        raise HTTPException(status_code=500, detail="Weather API key not configured")

    url = f"https.api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": app.state.openweather_api_key, # 👈 從 app.state 拿
        "units": "metric",  # 獲取攝氏溫度
        "lang": "zh_tw"     # 獲取繁體中文描述
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            weather_data = response.json()
            return {
                "temperature": weather_data.get("main", {}).get("temp"),
                "description": weather_data.get("weather", [{}])[0].get("description", "N/A"),
                "raw": weather_data
            }
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Error fetching weather: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# --- 10. 註冊 *所有* HTML 頁面 ---

# 來自 "原本的" main.py
@app.get("/form/personal", include_in_schema=False)
def show_form(request: Request):
    return templates.TemplateResponse("recommend_ui.html", {"request": request})

@app.get("/form/group", include_in_schema=False)
def show_group_form(request: Request):
    return templates.TemplateResponse("group_form.html", {"request": request})

# 來自 "START按鈕"
@app.get("/", response_class=HTMLResponse)
async def show_login_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def show_register(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

# 你的 trips.html 頁面會依賴 /trip_detail API
@app.get("/trips", response_class=HTMLResponse)
async def show_trips(request: Request):
    # 這裡檢查 cookie，如果沒有就導回登入頁
    if not request.cookies.get("user_id"):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("trips.html", {"request": request})

# --- 11. 啟動提示 ---
# (請使用這個指令啟動)
# python -m uvicorn main:app --reload --env-file .env


