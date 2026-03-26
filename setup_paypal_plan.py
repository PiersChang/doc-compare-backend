"""
執行一次即可：幫你在 PayPal 建立產品 + 訂閱方案
用法：python setup_paypal_plan.py
"""
import httpx, os, sys
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
SECRET    = os.getenv("PAYPAL_SECRET", "")
MODE      = os.getenv("PAYPAL_MODE", "sandbox")
BASE      = "https://api-m.sandbox.paypal.com" if MODE == "sandbox" else "https://api-m.paypal.com"

# ── 可以修改這裡 ──────────────────────────────────────
PRODUCT_NAME  = "文件比對工具 Pro"
PLAN_NAME     = "月繳方案"
PRICE         = "5.00"       # 美金（PayPal 不支援台幣，建議用 USD）
CURRENCY      = "USD"
# ─────────────────────────────────────────────────────

if not CLIENT_ID or not SECRET:
    print("❌ 請先設定 .env 裡的 PAYPAL_CLIENT_ID 和 PAYPAL_SECRET")
    sys.exit(1)

def get_token():
    r = httpx.post(
        f"{BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"}
    )
    r.raise_for_status()
    return r.json()["access_token"]

def create_product(token):
    r = httpx.post(
        f"{BASE}/v1/catalogs/products",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "name": PRODUCT_NAME,
            "description": "AI 文件比對服務 - 無限次使用",
            "type": "SERVICE",
            "category": "SOFTWARE"
        }
    )
    r.raise_for_status()
    product_id = r.json()["id"]
    print(f"✓ 產品建立成功：{product_id}")
    return product_id

def create_plan(token, product_id):
    r = httpx.post(
        f"{BASE}/v1/billing/plans",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "product_id": product_id,
            "name": PLAN_NAME,
            "description": f"每月 {PRICE} {CURRENCY}，無限次文件比對",
            "status": "ACTIVE",
            "billing_cycles": [
                {
                    "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,   # 0 = 無限期，直到取消
                    "pricing_scheme": {
                        "fixed_price": {"value": PRICE, "currency_code": CURRENCY}
                    }
                }
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "setup_fee": {"value": "0", "currency_code": CURRENCY},
                "setup_fee_failure_action": "CONTINUE",
                "payment_failure_threshold": 3
            }
        }
    )
    r.raise_for_status()
    plan_id = r.json()["id"]
    return plan_id

if __name__ == "__main__":
    print(f"🔧 連線到 PayPal ({MODE})...\n")
    try:
        token      = get_token()
        product_id = create_product(token)
        plan_id    = create_plan(token, product_id)

        print(f"✓ 訂閱方案建立成功！\n")
        print("=" * 50)
        print(f"請把以下這行加到你的 .env 檔：")
        print(f"\nPAYPAL_PLAN_ID={plan_id}\n")
        print("=" * 50)
        print(f"\n方案詳情：")
        print(f"  名稱：{PLAN_NAME}")
        print(f"  金額：{PRICE} {CURRENCY} / 月")
        print(f"  模式：{MODE}")
    except httpx.HTTPStatusError as e:
        print(f"❌ API 錯誤：{e.response.status_code}")
        print(e.response.text)
