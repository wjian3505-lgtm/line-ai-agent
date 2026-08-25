# LINE AI Agent 🤖

一個跑在 LINE 聊天室裡、會「思考」的 AI 個人助理。你只要用自然語言跟它說話，它會自動判斷該做什麼：

- 🗓️ **行程 / 待辦**：「幫我記下明天下午三點跟客戶開會」、「我這週有什麼行程？」
- 💰 **記帳**：「午餐花了 150」、「這個月花了多少？」
- 📝 **筆記備忘**：「幫我記一下 Wi-Fi 密碼是 abc123」、「幫我找剛剛記的密碼」
- 📈 **台股查詢**：「台積電現在多少？」、「幫我整理今天台股收盤」

技術組成：**FastAPI** + **LINE Messaging API (SDK v3)** + **Google Gemini（function calling）** + **Fugle 富果行情** + **SQLAlchemy**（本地 SQLite / 雲端 PostgreSQL）。

---

## 運作原理

```
LINE 使用者訊息
      │
      ▼
FastAPI /webhook ──► Gemini AI Agent（帶著一組工具）
                          │  自動 function calling
                          ├─ 行程 / 記帳 / 筆記 → 資料庫
                          └─ 股市查詢 → Fugle 富果 API
                          │
                          ▼
                    回覆文字 ──► LINE 使用者
```

Gemini 會依你的話決定要不要呼叫工具、呼叫哪一個、帶什麼參數，執行後再把結果組成自然的中文回覆。

---

## 一、事前準備（申請金鑰）

1. **LINE**：到 [LINE Developers Console](https://developers.line.biz/) 建立一個 **Messaging API** 頻道，取得 `Channel access token` 與 `Channel secret`。
2. **Gemini**：到 [Google AI Studio](https://aistudio.google.com/app/apikey) 取得 `GEMINI_API_KEY`。
3. **Fugle 富果**：到 [Fugle Developer](https://developer.fugle.tw/) 申請行情 API 金鑰。

---

## 二、本地開發

```bash
# 1. 建立虛擬環境並安裝套件
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt

# 2. 設定環境變數
copy .env.example .env   # Windows
# cp .env.example .env    # macOS/Linux
# 然後編輯 .env 填入你的金鑰

# 3. 啟動服務
uvicorn app.main:app --reload --port 8080
```

啟動後打開 <http://localhost:8080/> 應該看到 `{"status":"ok"}`。
本地未設定 `DATABASE_URL` 時會自動在 `data/app.db` 建立 SQLite 資料庫。

### 讓 LINE 連到你的本機（ngrok）

LINE 需要一個公開網址才能把訊息送進來。開發階段用 [ngrok](https://ngrok.com/)：

```bash
ngrok http 8080
```

把 ngrok 給的 `https://xxxx.ngrok-free.app` 加上 `/webhook`，填到 LINE 頻道的 **Webhook URL**，並開啟 *Use webhook*。之後用手機加好友、傳訊息即可測試。

---

## 三、部署到 Zeabur

1. 把這個專案推到 GitHub。
2. 在 [Zeabur](https://zeabur.com/) 建立專案，選擇你的 repo（會自動偵測 `Dockerfile`）。
3. 在同一個 Zeabur 專案裡新增一個 **PostgreSQL** 服務。
4. 到 Python 服務的 **Variables** 設定以下環境變數：
   - `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_CHANNEL_SECRET`
   - `GEMINI_API_KEY`（可選 `GEMINI_MODEL`）
   - `FUGLE_API_KEY`、`STOCK_WATCHLIST`
   - `DATABASE_URL`：填 Zeabur PostgreSQL 提供的連線字串，並把開頭改成 `postgresql+psycopg2://`
5. 部署完成後，把 Zeabur 給的網域 `https://xxx.zeabur.app/webhook` 設回 LINE 的 Webhook URL。

> 為什麼要用 PostgreSQL？Zeabur 容器的檔案系統是暫存的，重新部署會清空，SQLite 檔案會遺失，所以正式環境請改用 PostgreSQL。

### （可選）每日收盤自動推播

服務提供 `POST /tasks/daily-summary` 端點。設定 `DAILY_PUSH_USER_ID`（你的 LINE user id）與可選的 `TASK_SECRET` 後，用 Zeabur 的排程 / 外部 Cron 在每天收盤後呼叫：

```
POST https://xxx.zeabur.app/tasks/daily-summary?secret=你的TASK_SECRET
```

---

## 四、環境變數一覽

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✅ | LINE Messaging API token |
| `LINE_CHANNEL_SECRET` | ✅ | LINE 頻道 secret |
| `GEMINI_API_KEY` | ✅ | Google Gemini 金鑰 |
| `GEMINI_MODEL` | ✖ | 預設 `gemini-2.5-flash` |
| `FUGLE_API_KEY` | ✅（股市功能） | Fugle 富果行情金鑰 |
| `STOCK_WATCHLIST` | ✖ | 收盤整理的自選股，逗號分隔，預設 `2330,2454,2317` |
| `DATABASE_URL` | ✖ | 留空用 SQLite；雲端填 PostgreSQL 連線字串 |
| `DAILY_PUSH_USER_ID` | ✖ | 每日推播對象 |
| `TASK_SECRET` | ✖ | 保護排程端點的密鑰 |

---

## 專案結構

```
line-ai-agent/
├── app/
│   ├── main.py     # FastAPI + LINE Webhook 進入點
│   ├── agent.py    # Gemini AI Agent（function calling）
│   ├── tools.py    # Agent 可呼叫的工具（行程/記帳/筆記/股市）
│   ├── stock.py    # Fugle 富果行情存取
│   ├── models.py   # SQLAlchemy 資料模型
│   ├── db.py       # 資料庫連線與 Session
│   └── config.py   # 環境變數設定
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```
