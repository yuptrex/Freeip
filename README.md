# Keepalive Ping Bot

A Telegram bot for keeping your own server/service warm. Instead of one
command with arguments, it asks you three questions one at a time:

```
You:  /start
Bot:  📡 Send me the link or IPv4 address you want to keep alive.
You:  https://myapp.onrender.com
Bot:  How many requests should I send?
You:  50
Bot:  Over how many seconds should those be spread?
You:  10
Bot:  📡 Sending 50 request(s) to https://myapp.onrender.com/ over 10s…
Bot:  ✅ Done — sent 50 request(s) ... [per-request results]
```

The bot spaces the requests evenly across the window you gave it, sends
them, and reports the result of each, then stops.

A bare IPv4 address (e.g. `203.0.113.10`) is pinged over plain HTTP on
port 80 by default (`http://203.0.113.10/`).

`/cancel` abandons a conversation in progress. Only one job runs per chat
at a time — start a new one (`/start`) once the current one finishes.

## Limits

To keep this a lightweight keepalive tool rather than a load generator:

| Limit | Default | Env var |
|---|---|---|
| Max requests per job | 100 | `MAX_REQUESTS_PER_JOB` |
| Max window (seconds) | 3600 (1 hour) | `MAX_WINDOW_SECONDS` |

Adjust these in Render's environment variables if you need a different
ceiling.

## Architecture

Single aiohttp server on one Render Web Service. Telegram delivers
updates via webhook (`/webhook/<BOT_TOKEN>`) rather than polling, since
polling can silently die in the background while the web server still
answers health checks.

- `GET /health` — health check, also used by the bot's own self-ping loop
- `POST /webhook/<BOT_TOKEN>` — Telegram webhook endpoint

A background task pings the service's own `/health` every 10 minutes
(`SELF_PING_INTERVAL_SECONDS`) so Render's free tier doesn't spin this
bot itself down between uses. This is separate from — and doesn't
interact with — the ping jobs you request via chat.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Your bot token from [@BotFather](https://t.me/BotFather) |
| `BASE_URL` | Yes | Public URL of this deployed service, e.g. `https://pinger-bot.onrender.com` — no trailing slash |
| `WEBHOOK_URL` | No | Defaults to `BASE_URL` |
| `SELF_PING_INTERVAL_SECONDS` | No | Default `600` (10 min) |
| `MAX_REQUESTS_PER_JOB` | No | Default `100` |
| `MAX_WINDOW_SECONDS` | No | Default `3600` |
| `PORT` | No | Render sets this automatically |

See `.env.example` for local development.

## Deploying to Render

1. Push this project to a GitHub repo.
2. On Render: **New → Web Service** → connect the repo (or **New →
   Blueprint** to pick up `render.yaml` automatically).
   - Environment: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
3. Add `BOT_TOKEN`. Leave `BASE_URL` as a placeholder for the first
   deploy — Render assigns your `.onrender.com` URL during that deploy.
4. Once deployed, copy the exact URL from the service dashboard, set it
   as `BASE_URL` (no trailing slash), and redeploy. This also registers
   the Telegram webhook automatically on startup.
5. Message your bot with `/start` on Telegram to test.
