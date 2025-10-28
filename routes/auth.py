from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from bson import ObjectId
from datetime import datetime, date
import bcrypt

router = APIRouter()

# ================== DB helpers ==================
def users_col(request: Request):
    return request.app.state.db["users"]

# ================== Security ====================
def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

# ============== Index on startup =================
async def ensure_user_indexes(app):
    col = app.state.db["users"]
    await col.create_index("username", unique=True, name="uniq_username")
    await col.create_index("email", unique=True, name="uniq_email")

# ============== Request helpers =================
async def _try_get_json(request: Request):
    try:
        data = await request.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None

async def _try_get_form(request: Request):
    try:
        form = await request.form()
        return {k: form.get(k) for k in form.keys()}
    except Exception:
        return None

def _is_form_request(request: Request) -> bool:
    ctype = (request.headers.get("content-type") or "").lower()
    return ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data")

# ============== Serialization ===================
def _iso(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v

def _to_str(v):
    return str(v) if isinstance(v, ObjectId) else v

def _serialize_user(user: dict) -> dict:
    """將 Mongo 物件轉成可 JSON 序列化的乾淨結構"""
    # pendingRequests: [{ fromUserId: ObjectId/str, timestamp: datetime/... }]
    pending = []
    for x in user.get("pendingRequests", []):
        if not isinstance(x, dict):
            continue
        pending.append({
            "fromUserId": _to_str(x.get("fromUserId")),
            "timestamp": _iso(x.get("timestamp")),
        })

    return {
        "_id": str(user["_id"]),
        "username": user.get("username", ""),
        "email": user.get("email", ""),
        "mbti": user.get("mbti", ""),
        "birthday": user.get("birthday", ""),
        "phoneNumber": user.get("phoneNumber", ""),
        "bio": user.get("bio", ""),
        "avatarUrl": user.get("avatarUrl", ""),
        "friends": [_to_str(x) for x in user.get("friends", [])],
        "sentRequests": [_to_str(x) for x in user.get("sentRequests", [])],
        "pendingRequests": pending,
    }

# ================== Routes ======================

@router.post("/register")
async def register(request: Request):
    col = users_col(request)
    # 表單優先；否則就 JSON
    data = await _try_get_form(request) if _is_form_request(request) else (await _try_get_json(request) or await _try_get_form(request))

    # 基本驗證
    for k in ["username", "email", "password"]:
        if not data or not (data.get(k) or "").strip():
            if _is_form_request(request):
                tpl = request.app.state.templates
                return tpl.TemplateResponse("register.html", {"request": request, "error": f"缺少必要欄位：{k}"}, status_code=400)
            return JSONResponse({"ok": False, "message": f"缺少必要欄位：{k}"}, status_code=400)

    username = data["username"].strip()
    email = data["email"].strip().lower()
    password = data["password"]

    # 唯一性
    exists = await col.find_one({"$or": [{"username": username}, {"email": email}]})
    if exists:
        if _is_form_request(request):
            tpl = request.app.state.templates
            return tpl.TemplateResponse("register.html", {"request": request, "error": "使用者名稱或 Email 已被註冊"}, status_code=400)
        return JSONResponse({"ok": False, "message": "使用者名稱或 Email 已被註冊"}, status_code=400)

    doc = {
        "username": username,
        "email": email,
        "password": hash_password(password),
        "mbti": (data.get("mbti") or "").strip(),
        "birthday": (data.get("birthday") or "").strip(),        # 依你的 DB：字串
        "phoneNumber": (data.get("phoneNumber") or "").strip(),   # 駝峰
        "bio": (data.get("bio") or "").strip(),
        "avatarUrl": (data.get("avatarUrl") or "https://avatars.dicebear.com/api/micah/default.svg").strip(),
        "pendingRequests": [],
        "friends": [],
        "sentRequests": [],
    }

    res = await col.insert_one(doc)

    # 表單：註冊成功 → 回登入頁
    if _is_form_request(request):
        return RedirectResponse(url="/login", status_code=303)

    # API：回 JSON（注意不要用 doc 直接回，避免非序列化欄位）
    user = await col.find_one({"_id": res.inserted_id})
    return JSONResponse({"ok": True, "message": "註冊成功", "user": _serialize_user(user)})

@router.post("/login")
async def login(request: Request):
    col = users_col(request)
    data = await _try_get_form(request) if _is_form_request(request) else (await _try_get_json(request) or await _try_get_form(request))

    acct = (data.get("username") or "").strip() if data else ""
    pwd = data.get("password") or "" if data else ""

    if not acct or not pwd:
        if _is_form_request(request):
            tpl = request.app.state.templates
            return tpl.TemplateResponse("login.html", {"request": request, "error": "缺少帳號或密碼"}, status_code=400)
        return JSONResponse({"ok": False, "message": "缺少帳號或密碼"}, status_code=400)

    user = await col.find_one({"$or": [{"username": acct}, {"email": acct.lower()}]})
    if not user or not verify_password(pwd, user.get("password", "")):
        if _is_form_request(request):
            tpl = request.app.state.templates
            return tpl.TemplateResponse("login.html", {"request": request, "error": "帳號或密碼錯誤"}, status_code=401)
        return JSONResponse({"ok": False, "message": "帳號或密碼錯誤"}, status_code=401)

    # 表單：成功 → 設 cookie 並導到 /trips
    if _is_form_request(request):
        resp = RedirectResponse(url="/trips", status_code=303)
        resp.set_cookie("user_id", str(user["_id"]), httponly=True, samesite="lax")
        return resp

    # API：回 JSON（用序列化後 user）
    resp = JSONResponse({"ok": True, "message": "登入成功", "user": _serialize_user(user)})
    resp.set_cookie("user_id", str(user["_id"]), httponly=True, samesite="lax")
    return resp

@router.get("/me")
async def me(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return JSONResponse({"ok": False, "message": "未登入"}, status_code=401)

    try:
        oid = ObjectId(user_id)
    except Exception:
        return JSONResponse({"ok": False, "message": "無效的 user_id"}, status_code=400)

    col = users_col(request)
    user = await col.find_one({"_id": oid})
    if not user:
        return JSONResponse({"ok": False, "message": "找不到使用者"}, status_code=404)

    return {"ok": True, "user": _serialize_user(user)}

@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("user_id")
    return resp
