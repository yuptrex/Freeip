# 🤖 Telegram HTTP Request Bot

Send a URL or IPv4 address → set request count & duration → fire — with live stats via inline buttons.

---

## ✨ Features

| Feature | Details |
|---|---|
| **URL / IPv4 input** | Validates bare IPs (`192.168.1.1`) and full URLs (`https://example.com/path`) |
| **Request count** | 1 – 100 |
| **Duration** | 1 – 3600 seconds (interval = duration ÷ count) |
| **▶ Start Sending** | Inline button kicks off async background task |
| **📊 Refresh Stats** | Live progress bar + success / error breakdown |
| **/cancel** | Resets conversation or stops a running session |
| **MongoDB** | Every session persisted; survives restarts |
| **Webhook** | Full webhook mode on Render |
| **Self-ping** | Pings itself every 10 min to prevent Render free-tier cold starts |

---

## 🗂 File Structure

```
request-bot/
├── main.py            ← all bot logic (single file)
├── requirements.txt
├── Procfile           ← web: python main.py
├── render.yaml        ← Render service definition
├── .env.example       ← local dev template
├── .gitignore
└── README.md
```

---

## 🚀 Deploy to Render (step-by-step)

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` → follow prompts
3. Copy the **BOT_TOKEN**

### 2. Set up MongoDB Atlas (free tier)

1. Go to [mongodb.com/atlas](https://mongodb.com/atlas) → create free cluster
2. **Database Access** → Add user (readWrite role)
3. **Network Access** → Add IP `0.0.0.0/0` (allow all — required for Render)
4. **Connect** → Drivers → copy the connection string
5. Replace `<password>` with your DB user's password

### 3. Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

### 4. Create Render Web Service

1. Go to [render.com](https://render.com) → **New +** → **Web Service**
2. Connect your GitHub repo
3. Render auto-detects `render.yaml` — confirm settings
4. Go to **Environment** tab and add these variables:

| Key | Value |
|---|---|
| `BOT_TOKEN` | From BotFather |
| `MONGO_URI` | Atlas connection string (with password filled in) |
| `MONGO_DB_NAME` | `reqbot` |
| `WEBHOOK_URL` | Your Render URL e.g. `https://tg-request-bot.onrender.com` |
| `PORT` | `8080` |

5. Click **Save Changes** then **Deploy**

> ✅ `RENDER=true` and `RENDER_EXTERNAL_URL` are set automatically by Render.  
> The bot uses `WEBHOOK_URL` (or falls back to `RENDER_EXTERNAL_URL`) to register the Telegram webhook.

### 5. Verify

- Open your bot in Telegram and send `/start`
- Send a URL like `https://httpbin.org/get`
- Enter `10` requests, `60` seconds
- Tap **▶ Start Sending**
- Tap **📊 Refresh Stats** to watch live progress

---

## ⚠️ Common Deploy Errors

### `KeyError: 'MONGO_URI'` or `KeyError: 'BOT_TOKEN'`
→ The env var is not set in Render. Go to your service → **Environment** → add the missing variable → **Save & Deploy**.

### Bot doesn't respond after deploy
→ Check that `WEBHOOK_URL` matches your actual Render URL exactly (no trailing slash, correct `https://`).

---

## 🧪 Local Development (polling mode)

```bash
git clone https://github.com/YOUR_USER/YOUR_REPO.git
cd request-bot

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env: fill in BOT_TOKEN and MONGO_URI
# Leave WEBHOOK_URL blank → polling mode

python main.py
```

---

## 🔄 Self-Ping Logic

Render's free tier spins down a web service after ~15 minutes of no HTTP traffic.  
The bot schedules a job via `python-telegram-bot`'s `JobQueue` that pings its own URL **every 10 minutes**, which resets the idle timer without any external service.

```
startup → 60s → first ping → every 600s → ping → ...
```

---

## 🗄 MongoDB Schema

Collection: `sessions`

```json
{
  "user_id":          123456789,
  "target_url":       "https://example.com",
  "total_requests":   10,
  "duration_seconds": 60,
  "interval_seconds": 6.0,
  "sent_requests":    7,
  "success_count":    6,
  "error_count":      1,
  "status":           "running",
  "updated_at":       "2025-01-01T12:00:00Z"
}
```

One document per user — overwritten on each new session.

`status` values: `configuring` → `ready` → `running` → `done` | `cancelled`

---

## ⚙️ Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Telegram Bot API token |
| `MONGO_URI` | ✅ | — | MongoDB connection string |
| `MONGO_DB_NAME` | ❌ | `reqbot` | MongoDB database name |
| `WEBHOOK_URL` | ✅ on Render | — | Public HTTPS URL of this service |
| `PORT` | ❌ | `8080` | Port for the webhook server |
