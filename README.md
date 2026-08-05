# SlowBot — Telegram slowloris bot

Telegram bot that accepts a target IP and saturates it with slow HTTP
connections (slowloris). Job state is stored in MongoDB so attacks survive
bot restarts. Deploys on Render.

## Commands
- Send an IP (`203.0.113.10` or `203.0.113.10:8080`) — start attack
- `/stop` — stop the current attack
- `/status` — list active attacks
- `/clean` — purge stopped jobs
- `/start` — help

## Environment variables
| Var          | Required | Description                          |
|--------------|----------|--------------------------------------|
| BOT_TOKEN    | yes      | Telegram bot token from @BotFather   |
| MONGO_URI    | yes      | MongoDB connection string            |
| MONGO_DB     | no       | Database name (default: slowbot)     |
| OWNER_IDS    | no       | Comma-separated Telegram user IDs; empty = open |
| DEFAULT_PORT | no       | Port used when none given (80)       |
| DEFAULT_CONN | no       | Max concurrent connections (250)     |

## Local run
```bash
pip install -r requirements.txt
export $(cat .env.example | xargs)   # or set real values
python main.py
