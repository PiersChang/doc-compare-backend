from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import RedirectResponse, PlainTextResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
import sqlite3, hashlib, jwt, httpx, os, json, secrets, string, hmac, urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Doc Compare API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── 設定 ──────────────────────────────────────────────
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
JWT_SECRET       = os.getenv("JWT_SECRET", "change-this-secret-in-production")
FREE_LIMIT       = int(os.getenv("FREE_LIMIT", "3"))
DB_PATH          = os.getenv("DB_PATH", "doc_compare.db")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_SECRET    = os.getenv("PAYPAL_SECRET", "")
PAYPAL_PLAN_ID   = os.getenv("PAYPAL_PLAN_ID", "")
PAYPAL_MODE      = os.getenv("PAYPAL_MODE", "sandbox")
PAYPAL_BASE      = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
FRONTEND_URL     = os.getenv("FRONTEND_URL", "http://localhost:3000")
REFERRAL_BONUS   = int(os.getenv("REFERRAL_BONUS", "3"))   # 邀請雙方各得幾次

# ── SMTP 寄信設定（忘記密碼用）────────────────────────
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# ── 綠界 ECPay 設定 ───────────────────────────────────
ECPAY_MERCHANT_ID   = os.getenv("ECPAY_MERCHANT_ID", "2000132")       # 測試商店代號
ECPAY_HASH_KEY      = os.getenv("ECPAY_HASH_KEY", "5294y06JbISpM5x9")  # 測試 HashKey
ECPAY_HASH_IV       = os.getenv("ECPAY_HASH_IV", "v77hoKGq4kWxNNIS")   # 測試 HashIV
ECPAY_MODE          = os.getenv("ECPAY_MODE", "test")                  # test 或 production
ECPAY_BASE          = "https://payment-stage.ecpay.com.tw" if ECPAY_MODE == "test" else "https://payment.ecpay.com.tw"

# 點數包對應台幣價格（1 USD ≈ 32 TWD，可自行調整）
ECPAY_CREDIT_PRICES = {
    "pack_20":  "96",    # TWD $96  ≈ USD $3
    "pack_50":  "192",   # TWD $192 ≈ USD $6
    "pack_100": "320",   # TWD $320 ≈ USD $10
}
ECPAY_SUB_PRICE = "192"  # 訂閱每月 TWD $192 ≈ USD $6
security         = HTTPBearer()

# 點數包定義
CREDIT_PACKAGES = [
    {"id": "pack_20",  "credits": 20,  "price": "3.00",  "price_twd": "96",  "label": "20 次",  "popular": False},
    {"id": "pack_50",  "credits": 50,  "price": "6.00",  "price_twd": "192", "label": "50 次",  "popular": True},
    {"id": "pack_100", "credits": 100, "price": "10.00", "price_twd": "320", "label": "100 次", "popular": False},
]

# ── 資料庫 ────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)

try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = True
except ImportError:
    HAS_PG = False

@contextmanager
def get_db():
    if DATABASE_URL and HAS_PG:
        # Railway PostgreSQL
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        # 本地 SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

def db_execute(conn, sql, params=()):
    """統一 SQLite 和 PostgreSQL 的 ? 和 %s 差異"""
    if DATABASE_URL and HAS_PG:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur

def db_executescript(conn, script):
    """只用於初始化，PostgreSQL 版本逐句執行"""
    if DATABASE_URL and HAS_PG:
        cur = conn.cursor()
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            try:
                cur.execute(stmt)
            except Exception:
                pass
    else:
        conn.executescript(script)

def init_db():
    if DATABASE_URL and HAS_PG:
        print(f"[DB] Using PostgreSQL: {DATABASE_URL[:40]}...", flush=True)
    else:
        reason = "psycopg2 not installed" if DATABASE_URL else "DATABASE_URL not set"
        print(f"[DB] Using SQLite ({reason})", flush=True)

    with get_db() as db:
        is_pg = bool(DATABASE_URL and HAS_PG)
        serial = "SERIAL" if is_pg else "INTEGER"
        db_executescript(db, f"""
            CREATE TABLE IF NOT EXISTS users (
                id                     {serial} PRIMARY KEY,
                email                  TEXT    UNIQUE NOT NULL,
                password               TEXT    NOT NULL,
                plan                   TEXT    NOT NULL DEFAULT 'free',
                credits                INTEGER NOT NULL DEFAULT 0,
                referral_code          TEXT    UNIQUE,
                referred_by            INTEGER,
                paypal_subscription_id TEXT,
                plan_expires_at        TEXT,
                created_at             TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id         {serial} PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                used_at    TEXT    NOT NULL,
                source     TEXT    NOT NULL DEFAULT 'free'
            );
            CREATE TABLE IF NOT EXISTS credit_orders (
                id              {serial} PRIMARY KEY,
                user_id         INTEGER NOT NULL,
                package_id      TEXT    NOT NULL,
                credits         INTEGER NOT NULL,
                amount          TEXT    NOT NULL,
                paypal_order_id TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id         {serial} PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                token      TEXT    UNIQUE NOT NULL,
                expires_at TEXT    NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
            );
        """)
        # PostgreSQL 用 IF NOT EXISTS 避免「欄位已存在」導致 transaction 中斷
        for table, col, col_def in [
            ("users",     "credits",       "INTEGER NOT NULL DEFAULT 0"),
            ("users",     "referral_code", "TEXT"),
            ("users",     "referred_by",   "INTEGER"),
            ("usage_log", "source",        "TEXT NOT NULL DEFAULT 'free'"),
        ]:
            if is_pg:
                sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_def}"
                db_execute(db, sql)
            else:
                try:
                    db_execute(db, f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                except Exception:
                    pass

init_db()

# ── 工具函式 ──────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(user_id: int, email: str) -> str:
    payload = {"sub": str(user_id), "email": email, "exp": datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        return {"id": int(payload["sub"]), "email": payload["email"]}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已過期，請重新登入")
    except Exception:
        raise HTTPException(401, "無效的 Token")

def gen_referral_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(8))

def get_monthly_usage(db, user_id: int) -> int:
    first_day = datetime.now().strftime("%Y-%m-01")
    row = db_execute(db, 
        "SELECT COUNT(*) as cnt FROM usage_log WHERE user_id=? AND used_at>=? AND source='free'",
        (user_id, first_day)
    ).fetchone()
    return row["cnt"]

# ── PayPal 工具 ───────────────────────────────────────
async def get_paypal_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            data={"grant_type": "client_credentials"}
        )
        if resp.status_code != 200:
            raise HTTPException(502, "PayPal 驗證失敗")
        return resp.json()["access_token"]

async def get_paypal_subscription(subscription_id: str) -> dict:
    token = await get_paypal_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PAYPAL_BASE}/v1/billing/subscriptions/{subscription_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        return resp.json()

async def capture_paypal_order(order_id: str) -> dict:
    token = await get_paypal_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        return resp.json()

# ── Schema ────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email:        str
    password:     str
    referral_code: str = ""

class LoginRequest(BaseModel):
    email:    str
    password: str

class AnalyzeRequest(BaseModel):
    doc_a: str
    doc_b: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

# ── 路由：Auth ────────────────────────────────────────
@app.post("/auth/register")
def register(req: RegisterRequest):
    with get_db() as db:
        existing = db_execute(db, "SELECT id FROM users WHERE email=?", (req.email,)).fetchone()
        if existing:
            raise HTTPException(400, "此 Email 已被註冊")

        # 驗證邀請碼
        referrer_id = None
        if req.referral_code:
            referrer = db_execute(db, 
                "SELECT id FROM users WHERE referral_code=?", (req.referral_code.upper(),)
            ).fetchone()
            if not referrer:
                raise HTTPException(400, "邀請碼無效")
            referrer_id = referrer["id"]

        # 建立新用戶，附上邀請碼
        my_code = gen_referral_code()
        # 確保不重複
        while db_execute(db, "SELECT id FROM users WHERE referral_code=?", (my_code,)).fetchone():
            my_code = gen_referral_code()

        cur = db_execute(db,
            "INSERT INTO users (email, password, plan, credits, referral_code, referred_by, created_at) VALUES (?,?,?,?,?,?,?) RETURNING id" if (DATABASE_URL and HAS_PG) else
            "INSERT INTO users (email, password, plan, credits, referral_code, referred_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (req.email, hash_password(req.password), "free", 0, my_code, referrer_id, datetime.now().isoformat())
        )
        if DATABASE_URL and HAS_PG:
            user_id = cur.fetchone()["id"]
        else:
            user_id = db_execute(db, "SELECT last_insert_rowid()").fetchone()[0]

        # 雙方各得 REFERRAL_BONUS 次點數
        if referrer_id:
            db_execute(db, "UPDATE users SET credits=credits+? WHERE id=?", (REFERRAL_BONUS, referrer_id))
            db_execute(db, "UPDATE users SET credits=credits+? WHERE id=?", (REFERRAL_BONUS, user_id))

    return {"token": make_token(user_id, req.email), "plan": "free"}

@app.post("/auth/login")
def login(req: LoginRequest):
    with get_db() as db:
        user = db_execute(db, 
            "SELECT id, plan FROM users WHERE email=? AND password=?",
            (req.email, hash_password(req.password))
        ).fetchone()
        if not user:
            raise HTTPException(401, "Email 或密碼錯誤")
    return {"token": make_token(user["id"], req.email), "plan": user["plan"]}

def send_reset_email(email: str, reset_url: str):
    """發送密碼重設信，若 SMTP 未設定則印到 log"""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print(f"[Password Reset] SMTP 未設定，reset URL for {email}: {reset_url}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "文件比對工具 - 密碼重設"
        msg["From"]    = SMTP_USER
        msg["To"]      = email
        body = f"""<html><body>
<p>您好，</p>
<p>我們收到了您的密碼重設請求。請點擊以下連結重設您的密碼（連結 30 分鐘後失效）：</p>
<p><a href="{reset_url}">{reset_url}</a></p>
<p>如果您沒有發出此請求，請忽略此郵件。</p>
<p>— 文件比對工具團隊</p>
</body></html>"""
        msg.attach(MIMEText(body, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, email, msg.as_string())
    except Exception as e:
        print(f"[Password Reset] 寄信失敗: {e}，reset URL: {reset_url}")

@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    with get_db() as db:
        user = db_execute(db, "SELECT id FROM users WHERE email=?", (req.email,)).fetchone()
        if user:
            reset_token = secrets.token_urlsafe(32)
            expires_at  = (datetime.now() + timedelta(minutes=30)).isoformat()
            # 清除同帳號舊 token
            try:
                db_execute(db, "DELETE FROM password_reset_tokens WHERE user_id=?", (user["id"],))
            except Exception:
                pass
            db_execute(db,
                "INSERT INTO password_reset_tokens (user_id, token, expires_at, used) VALUES (?,?,?,0)",
                (user["id"], reset_token, expires_at)
            )
            reset_url = f"{FRONTEND_URL}?reset_token={reset_token}"
            send_reset_email(req.email, reset_url)
    # 無論 email 存不存在，都回傳同樣訊息（避免洩露帳號是否存在）
    return {"message": "如果此 Email 已註冊，您將收到重設密碼的郵件"}

@app.post("/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    if len(req.new_password) < 6:
        raise HTTPException(400, "密碼至少 6 個字元")
    with get_db() as db:
        row = db_execute(db,
            "SELECT * FROM password_reset_tokens WHERE token=? AND used=0",
            (req.token,)
        ).fetchone()
        if not row:
            raise HTTPException(400, "重設連結無效或已使用")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            raise HTTPException(400, "重設連結已過期，請重新申請")
        db_execute(db, "UPDATE users SET password=? WHERE id=?",
                   (hash_password(req.new_password), row["user_id"]))
        db_execute(db, "UPDATE password_reset_tokens SET used=1 WHERE token=?", (req.token,))
    return {"message": "密碼重設成功，請重新登入"}

@app.get("/auth/me")
def me(current_user: dict = Depends(verify_token)):
    with get_db() as db:
        user = db_execute(db,
            "SELECT plan, credits, referral_code FROM users WHERE id=?", (current_user["id"],)
        ).fetchone()
        if not user:
            raise HTTPException(404, "使用者不存在")
        usage = get_monthly_usage(db, current_user["id"])
    return {
        "email":         current_user["email"],
        "plan":          user["plan"],
        "credits":       user["credits"],
        "referral_code": user["referral_code"],
        "usage":         usage,
        "free_limit":    FREE_LIMIT,
        "remaining":     max(0, FREE_LIMIT - usage) if user["plan"] == "free" else 999
    }

# ── 路由：點數包 ──────────────────────────────────────
@app.get("/credits/packages")
def list_packages():
    return CREDIT_PACKAGES

@app.post("/credits/create-order-url")
async def create_credit_order_url(request: Request, current_user: dict = Depends(verify_token)):
    """建立點數包訂單並回傳 PayPal 跳轉連結（不需要 SDK）"""
    body       = await request.json()
    package_id = body.get("package_id")
    package    = next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)
    if not package:
        raise HTTPException(400, "無效的點數包")

    token = await get_paypal_token()
    return_url = f"{FRONTEND_URL}?credits_package={package_id}"
    cancel_url = f"{FRONTEND_URL}?paypal_cancelled=1"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": package["price"]},
                    "description": f"文件比對工具 {package['label']} 點數包"
                }],
                "application_context": {
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                    "brand_name": "文件比對工具",
                    "user_action": "PAY_NOW"
                }
            }
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"建立 PayPal 訂單失敗：{resp.text}")

    data = resp.json()
    order_id    = data["id"]
    approve_url = next(
        (link["href"] for link in data.get("links", []) if link["rel"] == "approve"),
        None
    )
    if not approve_url:
        raise HTTPException(502, "找不到 PayPal 付款連結")

    with get_db() as db:
        db_execute(db,
            "INSERT INTO credit_orders (user_id, package_id, credits, amount, paypal_order_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (current_user["id"], package_id, package["credits"], package["price"], order_id, "pending", datetime.now().isoformat())
        )

    return {"order_id": order_id, "approve_url": approve_url, "package": package}

@app.post("/credits/create-order")
async def create_credit_order(request: Request, current_user: dict = Depends(verify_token)):
    body       = await request.json()
    package_id = body.get("package_id")
    package    = next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)
    if not package:
        raise HTTPException(400, "無效的點數包")

    token = await get_paypal_token()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {"currency_code": "USD", "value": package["price"]},
                    "description": f"文件比對工具 {package['label']} 點數包"
                }]
            }
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(502, "建立 PayPal 訂單失敗")

    order_id = resp.json()["id"]
    with get_db() as db:
        db_execute(db, 
            "INSERT INTO credit_orders (user_id, package_id, credits, amount, paypal_order_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (current_user["id"], package_id, package["credits"], package["price"], order_id, "pending", datetime.now().isoformat())
        )
    return {"order_id": order_id, "package": package}

@app.post("/credits/capture-order")
async def capture_credit_order(request: Request, current_user: dict = Depends(verify_token)):
    body     = await request.json()
    order_id = body.get("order_id")
    if not order_id:
        raise HTTPException(400, "缺少 order_id")

    with get_db() as db:
        order = db_execute(db, 
            "SELECT * FROM credit_orders WHERE paypal_order_id=? AND user_id=? AND status='pending'",
            (order_id, current_user["id"])
        ).fetchone()
        if not order:
            raise HTTPException(404, "訂單不存在或已處理")

        # 向 PayPal 確認付款
        result = await capture_paypal_order(order_id)
        if result.get("status") != "COMPLETED":
            raise HTTPException(402, f"付款未完成：{result.get('status')}")

        # 加點數
        db_execute(db, "UPDATE credit_orders SET status='completed' WHERE paypal_order_id=?", (order_id,))
        db_execute(db, "UPDATE users SET credits=credits+? WHERE id=?", (order["credits"], current_user["id"]))

        new_credits = db_execute(db, "SELECT credits FROM users WHERE id=?", (current_user["id"],)).fetchone()["credits"]

    return {"success": True, "credits_added": order["credits"], "total_credits": new_credits}

# ── 路由：邀請碼 ──────────────────────────────────────
@app.get("/referral/stats")
def referral_stats(current_user: dict = Depends(verify_token)):
    with get_db() as db:
        user = db_execute(db, "SELECT referral_code FROM users WHERE id=?", (current_user["id"],)).fetchone()
        count = db_execute(db, 
            "SELECT COUNT(*) as cnt FROM users WHERE referred_by=?", (current_user["id"],)
        ).fetchone()["cnt"]
    return {
        "referral_code":  user["referral_code"],
        "referral_link":  f"{FRONTEND_URL}?ref={user['referral_code']}",
        "total_referred": count,
        "bonus_per_ref":  REFERRAL_BONUS,
    }

# ── 路由：PayPal 訂閱 ──────────────────────────────────
@app.get("/paypal/plans")
def get_plan_info():
    return {"client_id": PAYPAL_CLIENT_ID, "plan_id": PAYPAL_PLAN_ID, "mode": PAYPAL_MODE}

@app.post("/paypal/create-subscription-url")
async def create_subscription_url(current_user: dict = Depends(verify_token)):
    """後端建立訂閱連結，前端直接跳轉，不需要 SDK"""
    token = await get_paypal_token()
    return_url = f"{FRONTEND_URL}?paypal_return=subscription"
    cancel_url = f"{FRONTEND_URL}?paypal_cancelled=1"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{PAYPAL_BASE}/v1/billing/subscriptions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "plan_id": PAYPAL_PLAN_ID,
                "application_context": {
                    "brand_name":          "文件比對工具",
                    "locale":              "zh-TW",
                    "shipping_preference": "NO_SHIPPING",
                    "user_action":         "SUBSCRIBE_NOW",
                    "return_url":          return_url,
                    "cancel_url":          cancel_url
                }
            }
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"建立訂閱失敗：{resp.text}")

    data = resp.json()
    # 找到 approve 連結
    approve_url = next(
        (link["href"] for link in data.get("links", []) if link["rel"] == "approve"),
        None
    )
    if not approve_url:
        raise HTTPException(502, "找不到 PayPal 訂閱連結")

    return {"approve_url": approve_url, "subscription_id": data["id"]}

@app.post("/paypal/activate")
async def activate_subscription(request: Request, current_user: dict = Depends(verify_token)):
    body = await request.json()
    subscription_id = body.get("subscription_id")
    if not subscription_id:
        raise HTTPException(400, "缺少 subscription_id")
    sub    = await get_paypal_subscription(subscription_id)
    status = sub.get("status", "")
    if status not in ("ACTIVE", "APPROVED"):
        raise HTTPException(400, f"訂閱狀態異常：{status}")
    with get_db() as db:
        db_execute(db, 
            "UPDATE users SET plan=?, paypal_subscription_id=? WHERE id=?",
            ("pro", subscription_id, current_user["id"])
        )
    return {"success": True, "plan": "pro"}

@app.post("/paypal/webhook")
async def paypal_webhook(request: Request):
    body       = await request.json()
    event_type = body.get("event_type", "")
    if event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"):
        sid = body.get("resource", {}).get("id")
        if sid:
            with get_db() as db:
                db_execute(db, "UPDATE users SET plan='free', paypal_subscription_id=NULL WHERE paypal_subscription_id=?", (sid,))
    elif event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        sid = body.get("resource", {}).get("id")
        if sid:
            with get_db() as db:
                db_execute(db, "UPDATE users SET plan='free' WHERE paypal_subscription_id=?", (sid,))
    return {"received": True}

# ── ECPay 工具函式 ────────────────────────────────────
def ecpay_check_mac(params: dict) -> str:
    """產生綠界 CheckMacValue（依官方 Python SDK 演算法）"""
    filtered = {k: v for k, v in params.items() if k != "CheckMacValue"}
    keys = sorted(filtered.keys())
    parts = [f"HashKey={ECPAY_HASH_KEY}"]
    for k in keys:
        parts.append(f"{k}={filtered[k]}")
    parts.append(f"HashIV={ECPAY_HASH_IV}")
    raw = "&".join(parts)
    encoded = urllib.parse.quote_plus(raw).lower()
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest().upper()

def ecpay_verify_mac(params: dict) -> bool:
    """驗證綠界回傳的 CheckMacValue"""
    expected = ecpay_check_mac(params)
    return hmac.compare_digest(expected, params.get("CheckMacValue", ""))

# ── 路由：ECPay 點數包 ────────────────────────────────
@app.post("/ecpay/create-order")
async def ecpay_create_order(request: Request, current_user: dict = Depends(verify_token)):
    body       = await request.json()
    package_id = body.get("package_id")
    package    = next((p for p in CREDIT_PACKAGES if p["id"] == package_id), None)
    if not package:
        raise HTTPException(400, "無效的點數包")

    tweak  = secrets.token_hex(2).upper()
    trade_no = f"DC{datetime.now().strftime('%Y%m%d%H%M%S')}{tweak}"
    amount = ECPAY_CREDIT_PRICES.get(package_id, "96")

    # 存訂單到 DB（以 trade_no 作為識別）
    with get_db() as db:
        db_execute(db,
            "INSERT INTO credit_orders (user_id, package_id, credits, amount, paypal_order_id, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (current_user["id"], package_id, package["credits"], amount, trade_no, "pending", datetime.now().isoformat())
        )

    return_url = f"{FRONTEND_URL}?ecpay_return=credits"
    notify_url = f"{os.getenv('BACKEND_URL', 'https://doc-compare-backend-production.up.railway.app')}/ecpay/notify-credits"

    params = {
        "MerchantID":        ECPAY_MERCHANT_ID,
        "MerchantTradeNo":   trade_no,
        "MerchantTradeDate": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "PaymentType":       "aio",
        "TotalAmount":       amount,
        "TradeDesc":         "文件比對點數包",
        "ItemName":          f"文件比對工具 {package['label']} 點數包",
        "ReturnURL":         notify_url,
        "ClientBackURL":     return_url,
        "ChoosePayment":     "Credit",
        "EncryptType":       "1",
    }
    params["CheckMacValue"] = ecpay_check_mac(params)

    # 回傳 form action 和參數讓前端自動送出
    return {
        "action": f"{ECPAY_BASE}/Cashier/AioCheckOut/V5",
        "params": params,
        "trade_no": trade_no
    }

@app.post("/ecpay/notify-credits")
async def ecpay_notify_credits(request: Request):
    """綠界付款完成後的 Server 通知（非同步）"""
    form = await request.form()
    data = dict(form)

    if not ecpay_verify_mac(data):
        return PlainTextResponse("0|ErrorMessage")

    trade_no = data.get("MerchantTradeNo", "")
    rtn_code = data.get("RtnCode", "")

    if rtn_code == "1":  # 付款成功
        try:
            with get_db() as db:
                order = db_execute(db,
                    "SELECT * FROM credit_orders WHERE paypal_order_id=? AND status='pending'",
                    (trade_no,)
                ).fetchone()
                if order:
                    db_execute(db, "UPDATE credit_orders SET status='completed' WHERE paypal_order_id=?", (trade_no,))
                    db_execute(db, "UPDATE users SET credits=credits+? WHERE id=?", (order["credits"], order["user_id"]))
        except Exception as e:
            print(f"[ECPay notify] DB error: {e}")

    return PlainTextResponse("1|OK")

@app.get("/ecpay/order-status")
async def ecpay_order_status(trade_no: str, current_user: dict = Depends(verify_token)):
    """前端輪詢訂單狀態"""
    with get_db() as db:
        order = db_execute(db,
            "SELECT status, credits FROM credit_orders WHERE paypal_order_id=? AND user_id=?",
            (trade_no, current_user["id"])
        ).fetchone()
    if not order:
        raise HTTPException(404, "訂單不存在")
    return {"status": order["status"], "credits": order["credits"]}

# ── 路由：ECPay 訂閱（定期定額）────────────────────────
@app.post("/ecpay/create-subscription")
async def ecpay_create_subscription(request: Request, current_user: dict = Depends(verify_token)):
    """
    綠界定期定額（PeriodCredit）
    注意：正式申請後需開通「定期定額」功能
    """
    trade_no = f"DS{datetime.now().strftime('%Y%m%d%H%M%S')}{secrets.token_hex(2).upper()}"
    notify_url = f"{os.getenv('BACKEND_URL', 'https://doc-compare-backend-production.up.railway.app')}/ecpay/notify-subscription"
    return_url = f"{FRONTEND_URL}?ecpay_return=subscription"

    params = {
        "MerchantID":          ECPAY_MERCHANT_ID,
        "MerchantTradeNo":     trade_no,
        "MerchantTradeDate":   datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "PaymentType":         "aio",
        "TotalAmount":         ECPAY_SUB_PRICE,
        "TradeDesc":           "文件比對無限訂閱",
        "ItemName":            "文件比對工具 無限版月訂閱",
        "ReturnURL":           notify_url,
        "ClientBackURL":       return_url,
        "ChoosePayment":       "Credit",
        "EncryptType":         "1",
        "PeriodAmount":        ECPAY_SUB_PRICE,
        "PeriodType":          "M",   # 每月
        "Frequency":           "1",
        "ExecTimes":           "12",  # 最多 12 個月，可調整
        "PeriodReturnURL":     notify_url,
    }
    params["CheckMacValue"] = ecpay_check_mac(params)

    # 儲存訂閱記錄
    with get_db() as db:
        db_execute(db,
            "UPDATE users SET paypal_subscription_id=? WHERE id=?",
            (trade_no, current_user["id"])
        )

    return {
        "action": f"{ECPAY_BASE}/Cashier/AioCheckOut/V5",
        "params": params,
        "trade_no": trade_no
    }

@app.post("/ecpay/notify-subscription")
async def ecpay_notify_subscription(request: Request):
    """綠界訂閱付款通知"""
    form = await request.form()
    data = dict(form)

    if not ecpay_verify_mac(data):
        return PlainTextResponse("0|ErrorMessage")

    rtn_code    = data.get("RtnCode", "")
    trade_no    = data.get("MerchantTradeNo", "")

    if rtn_code == "1":
        with get_db() as db:
            # 首次付款或定期扣款成功 → 升級/維持 pro
            db_execute(db,
                "UPDATE users SET plan='pro' WHERE paypal_subscription_id=?",
                (trade_no,)
            )

    return PlainTextResponse("1|OK")

# ── 路由：分析 ────────────────────────────────────────
@app.post("/analyze")
async def analyze(req: AnalyzeRequest, current_user: dict = Depends(verify_token)):
    with get_db() as db:
        user = db_execute(db, "SELECT plan, credits FROM users WHERE id=?", (current_user["id"],)).fetchone()

        # 決定使用哪個額度
        if user["plan"] == "pro":
            source = "pro"
        elif user["credits"] > 0:
            source = "credits"   # 優先扣點數
        else:
            usage = get_monthly_usage(db, current_user["id"])
            if usage >= FREE_LIMIT:
                raise HTTPException(429, "免費額度與點數均已用完，請購買點數或升級付費方案")
            source = "free"

        if not OPENAI_API_KEY:
            raise HTTPException(500, "伺服器未設定 OPENAI_API_KEY")

        prompt = f"""你是一位專業文件比對助手，請比較以下兩版文件，以繁體中文回應。
只回傳 JSON，不要 markdown、不要說明文字。

格式：
{{
  "added": 數字,
  "removed": 數字,
  "changed": 數字,
  "risk_level": "低或中或高",
  "risk_score": 1到10的整數,
  "summary": "一句話摘要",
  "suggestions": ["建議事項1", "建議事項2"],
  "details": [
    {{
      "type": "新增或刪除或修改",
      "item": "條款名稱",
      "description": "具體變更說明",
      "risk": "風險提示，無則空字串",
      "text_a": "版本A的原文片段，最多100字，無則空字串",
      "text_b": "版本B的原文片段，最多100字，無則空字串"
    }}
  ]
}}

【版本A（舊版）】
{req.doc_a}

【版本B（新版）】
{req.doc_b}"""

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": "gpt-4o-mini", "max_tokens": 2000,
                      "messages": [{"role": "user", "content": prompt}]}
            )

        if resp.status_code != 200:
            raise HTTPException(502, f"OpenAI 回應錯誤: {resp.text}")

        result_text = resp.json()["choices"][0]["message"]["content"]
        result      = json.loads(result_text.replace("```json", "").replace("```", "").strip())

        # 扣額度
        if source == "credits":
            db_execute(db, "UPDATE users SET credits=credits-1 WHERE id=?", (current_user["id"],))
        db_execute(db, "INSERT INTO usage_log (user_id, used_at, source) VALUES (?,?,?)",
                   (current_user["id"], datetime.now().isoformat(), source))

        result["locked"]       = False
        result["locked_count"] = 0
        result["source"]       = source

    return result

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
