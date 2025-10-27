# main.py
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager # 👈 為了 lifespan
import logging

# --- 你的其他 imports ---
from routes.recommend import router as recommend_router
from routes.users import router as user_router
from dotenv import load_dotenv
load_dotenv()
from core import mongo

# --- (你的 MongoDB 連線測試) ---
try:
    mongo.test_connection()
except UnicodeEncodeError:
    pass

# --- Lifespan Function (用於啟動/關閉事件) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 在 App 啟動時執行的程式碼
    print("FastAPI application startup...")
    yield
    # 在 App 關閉時執行的程式碼
    print("FastAPI application shutdown.")

# --- 建立 FastAPI app (使用 lifespan) ---
app = FastAPI(
    title="旅遊行程推薦系統 API",
    description="整合 GPT、Google Maps、使用者偏好與收藏紀錄，自動生成每日行程。",
    version="1.0.0",
    lifespan=lifespan # 👈 啟用 lifespan
)

# --- CORS 設定 (保持不變) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 掛載模板系統 (保持不變) ---
templates = Jinja2Templates(directory="templates")

# 🔽🔽 2. ‼️‼️ 加入這個全局錯誤處理器 ‼️‼️ 🔽🔽
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    捕捉所有 Pydantic 模型驗證失敗的錯誤 (RequestValidationError)。
    """
    # 在終端機印出非常詳細的錯誤
    print(f"🔥🔥🔥 Pydantic Validation Error Caught by Handler! 🔥🔥🔥")
    try:
        # 嘗試印出請求方法和路徑
        print(f"Request: {request.method} {request.url.path}")
    except Exception as req_err:
        print(f"(Could not print request details: {req_err})")
        
    # ‼️ 印出 Pydantic 提供的詳細錯誤列表 ‼️
    print(f"Validation Errors:\n{exc.errors()}") 
    
    # 回傳 422 給客戶端，包含詳細錯誤
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )
# 🔼🔼 (異常處理器結束) 🔼🔼

# --- 註冊路由 (保持不變) ---
app.include_router(recommend_router, prefix="/recommend", tags=["推薦行程"])
app.include_router(user_router, prefix="/users", tags=["使用者"])

# --- HTML 表單路由 (保持不變) ---
@app.get("/form/personal", include_in_schema=False)
def show_form(request: Request):
    return templates.TemplateResponse("recommend_ui.html", {"request": request})

@app.get("/form/group", include_in_schema=False)
def show_group_form(request: Request):
    return templates.TemplateResponse("group_form.html", {"request": request})


# uvicorn main:app --reload
# 個人：http://127.0.0.1:8000/form/personal
# 團體：http://127.0.0.1:8000/form/group
# （用這個）
# python -m uvicorn main:app --reload --env-file .env
# python -m uvicorn main:app --reload