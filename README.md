# 🤖 Telegram HTTP Request Bot

Send HTTP requests to any URL or IPv4 address via Telegram. Tracks sent/errors in real-time with an inline Stats button.

---

## ✨ Features

- ✅ Accepts URLs **and** raw IPv4 addresses (e.g. `192.168.1.1:8080/path`)
- ✅ Up to **100 requests**, spaced up to **3600 seconds** apart
- ✅ Inline **Start Sending** button
- ✅ Inline **Stats** button → shows sent, errors, remaining, progress bar
- ✅ Completion notification when all requests finish
- ✅ **MongoDB** stores sessions & jobs (survives restarts)
- ✅ **Webhook** mode (no polling)
- ✅ **Self-ping every 10 minutes** to prevent Render cold starts

---

## 🚀 Deployment Guide

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow prompts
3. Copy the **BOT_TOKEN**

---

### 2. Set up MongoDB Atlas (free tier)

1. Go to [cloud.mongodb.com](https://cloud.mongodb.com) → create free cluster
2. Create a database user (username + password)
3. Whitelist IP `0.0.0.0/0` (allow all — required for Render)
4. Click **Connect → Drivers** → copy the connection string
5. Replace `<password>` with your DB user's password
6. Your URI looks like:
   ```
   mongodb+srv://myuser:mypassword@cluster0.abcde.mongodb.net/requestbot?retryWrites=true&w=majority
   ```

---

### 3. Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
gh repo create telegram-request-bot --public --push
# or: git remote add origin https://github.com/YOU/telegram-request-bot.git && git push -u origin main
```

---

### 4. Deploy on Render

1. Go to [render.com](https://render.com) → **New → Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Build Command:** `npm install`
   - **Start Command:** `npm start`
   - **Plan:** Free
4. Add **Environment Variables** (in Render dashboard → Environment tab):

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | your Telegram bot token |
| `MONGODB_URI` | your MongoDB Atlas URI |
| `WEBHOOK_URL` | `https://your-app-name.onrender.com` |

5. Click **Deploy** — wait for "Live" status
6. Copy your Render URL (e.g. `https://telegram-request-bot.onrender.com`)
7. Update `WEBHOOK_URL` env var with the exact Render URL

---

### 5. Register the Webhook

After deploy, open in browser or run:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://your-app.onrender.com/bot<BOT_TOKEN>"
```

Expected response:
```json
{"ok":true,"result":true,"description":"Webhook was set"}
```

✅ Done! Your bot is live.

---

## 💬 Bot Usage Flow

```
User:  https://example.com           (or 1.2.3.4:8080)
Bot:   ✅ Target set. How many requests? (max 100)

User:  50
Bot:   ✅ Requests: 50. Interval in seconds? (max 3600)

User:  5
Bot:   📋 Summary ... [🚀 Start Sending]

User clicks: 🚀 Start Sending
Bot:   🚀 Sending started!   [📊 Stats]

User clicks: 📊 Stats
Bot:   📊 Job Stats
       ██████░░░░ 60%
       ✅ Sent: 30
       ❌ Errors: 0
       ⏳ Remaining: 20
       ...

Bot (auto): 🎉 All done! ██████████ 100% ...
```

---

## 📁 Project Structure

```
telegram-request-bot/
├── bot.js              # Main bot logic
├── models/
│   ├── Session.js      # Per-user conversation state
│   └── Job.js          # Request job tracking
├── package.json
├── render.yaml         # Render deployment config
├── .env.example        # Environment variable template
├── .gitignore
└── README.md
```

---

## 🔧 Local Development

```bash
npm install
cp .env.example .env
# fill in .env values

# For local dev, use ngrok to expose localhost:
npx ngrok http 3000
# Then set WEBHOOK_URL=https://xxxx.ngrok.io in .env

npm run dev
```

---

## ⚙️ Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `MONGODB_URI` | MongoDB Atlas connection string |
| `WEBHOOK_URL` | Public HTTPS URL of your Render service |
| `PORT` | Port (Render sets this automatically) |

---

## 🛡️ Limits

| Setting | Limit |
|---------|-------|
| Max requests | 100 |
| Max interval | 3600 seconds (1 hour) |
| Request timeout | 10 seconds per request |
| Self-ping interval | Every 10 minutes |
