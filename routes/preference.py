from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from bson import ObjectId
from datetime import datetime
from typing import List, Optional

router = APIRouter()

# ========= 顯示表單（維持既有邏輯，帶入 user_id → Leader_id） =========
@router.get("/preference", response_class=HTMLResponse)
async def preference_page(request: Request, user_id: str = ""):
    """
    顯示建立旅遊單表單。前端隱藏欄位 Leader_id 會帶入 user_id。
    """
    templates = request.app.state.templates
    return templates.TemplateResponse("preference.html", {"request": request, "user_id": user_id})


# ========= 送出表單，寫入 tripDemo-shan.forms =========
@router.post("/submit_preference")
async def submit_preference(
    request: Request,

    # —— 完全以 DB 欄位為主的參數命名（必要欄位）——
    Form_type: str = Form("trip_preference"),
    Trip_name: str = Form(...),
    Leader_id: str = Form(...),

    # 改為可多選縣市
    Location: List[str] = Form(...),            # 允許多個值（checkbox 同名送出）

    Date_start: str = Form(...),                # YYYY-MM-DD
    Date_end: str = Form(...),                  # YYYY-MM-DD
    Time_start: str = Form(...),                # HH:MM
    Time_end: str = Form(...),                  # HH:MM
    Days: int = Form(...),
    Budget: int = Form(...),
    Google_rates: float = Form(...),

    # 多選陣列
    Preferences: Optional[List[str]] = Form(None),
    transportation: Optional[List[str]] = Form(None),

    # 可選欄位
    Exclude: str = Form(""),
    Notes: str = Form(""),

    # 其他交通方式（若勾選「其他」時使用）
    other_transport: str = Form(""),

    # Members：以文字框提交，多位成員使用「逗號」或「換行」分隔
    Members_text: str = Form("", alias="Members")
):
    """
    以 tripDemo-shan.forms 的結構寫入：
    {
      Form_type, Trip_name, Leader_id(ObjectId),
      Location: [str, ...],
      Date:{start,end}, Time_range:{start,end},
      Days, Budget, Google_rates, Preferences[], Exclude, Notes,
      transportation[], Members[], Create_time
    }
    """
    templates = request.app.state.templates

    # ===== 取得 Motor Client，切換到指定 DB / Collection =====
    try:
        default_db = request.app.state.db                            # AsyncIOMotorDatabase
        client = default_db.client                                   # AsyncIOMotorClient
        target_db = client["tripDemo-shan"]                          # 目標 Database
        forms_col = target_db["forms"]                               # 目標 Collection
    except Exception as e:
        return templates.TemplateResponse(
            "preference.html",
            {"request": request, "user_id": Leader_id, "error": f"資料庫連線錯誤：{str(e)}"}
        )

    # ===== 整理 transportation（處理「其他」）=====
    if transportation is None:
        transportation = []
    if other_transport and ("其他" in transportation or "其它" in transportation):
        transportation = [t for t in transportation if t not in ("其他", "其它")]
        transportation.append(other_transport)

    # ===== 解析 Members：允許逗號或換行分隔；去除空白與空行 =====
    members_list: List[str] = []
    if Members_text:
        raw = Members_text.replace("\r", "\n")
        parts = []
        for line in raw.split("\n"):
            parts.extend([p.strip() for p in line.split(",")])
        members_list = [p for p in (x.strip() for x in parts) if p]

    # ===== 組合入庫文件 =====
    try:
        doc = {
            "Form_type": Form_type,
            "Trip_name": Trip_name,
            "Leader_id": ObjectId(Leader_id) if Leader_id else None,
            "Location": Location,                 # ← 存陣列
            "Date": {
                "start": Date_start,
                "end": Date_end
            },
            "Time_range": {
                "start": Time_start,
                "end": Time_end
            },
            "Days": Days,
            "Budget": Budget,
            "Google_rates": Google_rates,
            "Preferences": Preferences or [],
            "Exclude": Exclude,
            "Notes": Notes,
            "transportation": transportation,
            "Members": members_list,              # 統一存成字串陣列
            "Create_time": datetime.utcnow()
        }

        # 寫入 DB
        await forms_col.insert_one(doc)

        # 寫完導回行程列表（沿用原本帶 user_id 的路徑）
        uid = Leader_id if Leader_id else ""
        return RedirectResponse(url=f"/trips?user_id={uid}", status_code=303)

    except Exception as e:
        return templates.TemplateResponse(
            "preference.html",
            {
                "request": request,
                "user_id": Leader_id,
                "error": f"儲存失敗：{str(e)}"
            }
        )
