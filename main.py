from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ✅ 導入路由
from routes.recommend import router as recommend_router
from routes.users import router as user_router  # 👈 使用者 API
from dotenv import load_dotenv
load_dotenv()
from core import mongo

# Avoid console UnicodeEncodeError on Windows terminals (cp950) when test prints emoji
try:
    mongo.test_connection()
except UnicodeEncodeError:
    pass

# ✅ 建立 FastAPI app
app = FastAPI(
    title="旅遊行程推薦系統 API",
    description="整合 GPT、Google Maps、使用者偏好與收藏紀錄，自動生成每日行程。",
    version="1.0.0"
)

# ✅ CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ 掛載模板系統
templates = Jinja2Templates(directory="templates")

# ✅ 註冊路由
app.include_router(recommend_router, prefix="/recommend", tags=["推薦行程"])
app.include_router(user_router, prefix="/users", tags=["使用者"])  # 👈 加入這行很關鍵！

# ✅ 首頁（個人推薦表單）
@app.get("/form/personal", include_in_schema=False)
def show_form(request: Request):
    return templates.TemplateResponse("recommend_ui.html", {"request": request})

# ✅ 團體推薦表單
@app.get("/form/group", include_in_schema=False)
def show_group_form(request: Request):
    return templates.TemplateResponse("group_form.html", {"request": request})


# uvicorn main:app --reload
# 個人：http://127.0.0.1:8000/form/personal
# 團體：http://127.0.0.1:8000/form/group
# （用這個）
# python -m uvicorn main:app --reload --env-file .env
# python -m uvicorn main:app --reload
