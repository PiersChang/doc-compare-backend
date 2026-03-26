# Doc Compare — 文件比對工具

## 專案結構

```
Doc-Compare/
├── main.py          # FastAPI 後端
├── index.html       # 前端介面
├── requirements.txt # Python 套件
├── .env.example     # 環境變數範本
└── .gitignore
```

## 本地開發

### 1. 安裝套件

```bash
cd E:\SideProject\Chrome-Extension\Doc-Compare
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. 設定環境變數

```bash
copy .env.example .env
# 用編輯器打開 .env，填入你的 OPENAI_API_KEY 和 JWT_SECRET
```

### 3. 啟動後端

```bash
uvicorn main:app --reload --port 8000
```

後端跑起來後：
- API 文件：http://localhost:8000/docs
- Health check：http://localhost:8000/health

### 4. 開啟前端

直接用瀏覽器開啟 `index.html`，或：

```bash
# 用 Python 起一個靜態檔案伺服器
python -m http.server 3000
# 然後打開 http://localhost:3000
```

## 部署到雲端 (Railway)

1. 推到 GitHub
2. 到 [railway.app](https://railway.app) 新增專案，連結 GitHub repo
3. 設定環境變數（OPENAI_API_KEY、JWT_SECRET）
4. Railway 自動部署，取得公開 URL
5. 把 `index.html` 裡的 `API_BASE` 改成 Railway URL

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | /auth/register | 註冊 |
| POST | /auth/login | 登入 |
| GET  | /auth/me | 取得用戶資訊與用量 |
| POST | /analyze | 執行文件比對 |
| GET  | /health | 健康檢查 |

## 變現設定

在 `.env` 調整免費額度：

```
FREE_LIMIT=5   # 免費用戶每月 5 次
```

付費功能（升級 plan）目前預留入口，可接 Stripe 或綠界實作。
