"""
Telegram HTTP Request Bot
--------------------------
- User sends a URL or IPv4 address
- Bot asks: how many requests? (max 100)
- Bot asks: interval in seconds? (max 3600)
- Bot shows inline button: [▶ Start Sending]
- After start: shows [📊 Stats] button to check progress
- Stats shows: sent, remaining, target URL, interval
- MongoDB stores all sessions
- Webhook mode on Render with self-ping every 10 min
"""

import asyncio
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

import aiohttp
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN   = os.environ["BOT_TOKEN"]
MONGO_URI   = os.environ["MONGO_URI"]
DB_NAME     = os.environ.get("MONGO_DB_NAME", "reqbot")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT        = int(os.environ.get("PORT", "8080"))

MAX_REQUESTS = 100
MAX_SECONDS  = 3600

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("reqbot")

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db           = mongo_client[DB_NAME]
sessions_col = db["sessions"]   # one doc per user conversation session

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
URL_RE  = re.compile(
    r"^(https?://)"                          # scheme required
    r"(\d{1,3}\.){3}\d{1,3}"                # IP-based URL
    r"(:\d+)?(/.*)?$"                        # optional port + path
    r"|"
    r"^(https?://)?"                         # scheme optional for hostnames
    r"([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}"       # domain
    r"(:\d+)?(/.*)?$",
    re.IGNORECASE,
)
IPV4_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$"
)


def is_valid_target(text: str) -> bool:
    """Accept plain IPv4 addresses or any URL (http/https)."""
    text = text.strip()
    if IPV4_RE.match(text):
        # Validate each octet
        parts = text.split(".")
        return all(0 <= int(p) <= 255 for p in parts)
    return bool(URL_RE.match(text))


def normalize_url(text: str) -> str:
    """Add http:// prefix if the target is a bare IP or domain."""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "http://" + text
    return text

# ---------------------------------------------------------------------------
# Conversation state keys (stored in context.user_data)
# ---------------------------------------------------------------------------
STATE_IDLE          = "idle"
STATE_WAIT_COUNT    = "wait_count"
STATE_WAIT_INTERVAL = "wait_interval"

# ---------------------------------------------------------------------------
# Session helpers (MongoDB)
# ---------------------------------------------------------------------------

def upsert_session(user_id: int, data: dict):
    sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {**data, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def get_session(user_id: int) -> dict | None:
    return sessions_col.find_one({"user_id": user_id})


def update_session(user_id: int, data: dict):
    sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {**data, "updated_at": datetime.now(timezone.utc)}},
    )

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def start_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶ Start Sending", callback_data=f"start:{user_id}"),
    ]])


def stats_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Stats", callback_data=f"stats:{user_id}"),
    ]])

# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_IDLE
    await update.message.reply_text(
        "👋 *HTTP Request Bot*\n\n"
        "Send me a *URL* or *IPv4 address* and I'll hammer it with requests.\n\n"
        "📌 *How to use:*\n"
        "1️⃣ Send a URL or IPv4 address\n"
        "2️⃣ Tell me how many requests _(max 100)_\n"
        "3️⃣ Tell me the interval in seconds _(max 3600)_\n"
        "4️⃣ Hit *▶ Start Sending*\n"
        "5️⃣ Tap *📊 Stats* any time to check progress",
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text  = (update.message.text or "").strip()
    state = context.user_data.get("state", STATE_IDLE)
    uid   = update.effective_user.id

    # ── Step 1: receive target URL / IP ─────────────────────────────────────
    if state == STATE_IDLE:
        if not is_valid_target(text):
            await update.message.reply_text(
                "❌ That doesn't look like a valid URL or IPv4 address.\n"
                "Please send something like:\n"
                "• `https://example.com`\n"
                "• `192.168.1.1`\n"
                "• `http://10.0.0.1:8080/path`",
                parse_mode="Markdown",
            )
            return

        url = normalize_url(text)
        upsert_session(uid, {"target_url": url, "user_id": uid})
        context.user_data["target_url"] = url
        context.user_data["state"]      = STATE_WAIT_COUNT

        await update.message.reply_text(
            f"✅ Target set: `{url}`\n\n"
            f"How many requests should I send? _(1 – {MAX_REQUESTS})_",
            parse_mode="Markdown",
        )
        return

    # ── Step 2: receive request count ───────────────────────────────────────
    if state == STATE_WAIT_COUNT:
        if not text.isdigit():
            await update.message.reply_text(
                f"⚠️ Please send a whole number between 1 and {MAX_REQUESTS}."
            )
            return

        count = int(text)
        if not (1 <= count <= MAX_REQUESTS):
            await update.message.reply_text(
                f"⚠️ Number must be between 1 and {MAX_REQUESTS}. Try again."
            )
            return

        update_session(uid, {"total_requests": count})
        context.user_data["total_requests"] = count
        context.user_data["state"]          = STATE_WAIT_INTERVAL

        await update.message.reply_text(
            f"✅ Requests set: *{count}*\n\n"
            f"In how many seconds should all requests be spread? _(1 – {MAX_SECONDS})_\n"
            f"_(The bot sends one request every `total_seconds ÷ count` seconds)_",
            parse_mode="Markdown",
        )
        return

    # ── Step 3: receive total duration ──────────────────────────────────────
    if state == STATE_WAIT_INTERVAL:
        if not text.isdigit():
            await update.message.reply_text(
                f"⚠️ Please send a whole number between 1 and {MAX_SECONDS}."
            )
            return

        duration = int(text)
        if not (1 <= duration <= MAX_SECONDS):
            await update.message.reply_text(
                f"⚠️ Duration must be between 1 and {MAX_SECONDS} seconds. Try again."
            )
            return

        total   = context.user_data.get("total_requests", 1)
        url     = context.user_data.get("target_url", "")
        interval = round(duration / total, 2)   # seconds between each request

        update_session(uid, {
            "duration_seconds": duration,
            "interval_seconds": interval,
            "sent_requests":    0,
            "status":           "ready",
        })
        context.user_data["duration_seconds"] = duration
        context.user_data["interval_seconds"] = interval
        context.user_data["state"]            = STATE_IDLE

        await update.message.reply_text(
            f"🎯 *Ready to fire!*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"📦 *Requests:* {total}\n"
            f"⏱ *Spread over:* {duration}s "
            f"_(~{interval}s between each)_\n\n"
            f"Press *▶ Start Sending* to begin.",
            parse_mode="Markdown",
            reply_markup=start_keyboard(uid),
        )
        return

    # Fallback — user typed something unexpected
    await update.message.reply_text(
        "📌 Send me a URL or IPv4 address to get started, or use /start."
    )

# ---------------------------------------------------------------------------
# Callback handler (inline buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data or ""
    await query.answer()

    # ── ▶ Start Sending ─────────────────────────────────────────────────────
    if data.startswith("start:"):
        uid = update.effective_user.id
        session = get_session(uid)

        if not session or session.get("status") not in ("ready",):
            await query.edit_message_text(
                "⚠️ No session found. Please send a URL first."
            )
            return

        if session.get("status") == "running":
            await query.answer("Already running!", show_alert=True)
            return

        # Mark as running
        update_session(uid, {"status": "running", "sent_requests": 0})

        url      = session["target_url"]
        total    = session["total_requests"]
        interval = session["interval_seconds"]

        await query.edit_message_text(
            f"🚀 *Started!*\n\n"
            f"🌐 Target: `{url}`\n"
            f"📦 Sending *{total}* requests "
            f"every *{interval}s*…\n\n"
            f"Tap *📊 Stats* to check progress.",
            parse_mode="Markdown",
            reply_markup=stats_keyboard(uid),
        )

        # Kick off the background task
        asyncio.create_task(
            send_requests_task(
                bot=context.bot,
                uid=uid,
                chat_id=update.effective_chat.id,
                url=url,
                total=total,
                interval=interval,
            )
        )
        return

    # ── 📊 Stats ─────────────────────────────────────────────────────────────
    if data.startswith("stats:"):
        uid     = update.effective_user.id
        session = get_session(uid)

        if not session:
            await query.answer("No active session found.", show_alert=True)
            return

        sent      = session.get("sent_requests", 0)
        total     = session.get("total_requests", 0)
        remaining = max(0, total - sent)
        status    = session.get("status", "unknown")
        url       = session.get("target_url", "N/A")
        interval  = session.get("interval_seconds", "N/A")

        status_icon = {
            "ready":    "🟡 Ready",
            "running":  "🟢 Running",
            "done":     "✅ Done",
            "error":    "🔴 Error",
        }.get(status, f"❓ {status}")

        await query.answer(
            f"Sent: {sent} | Remaining: {remaining}",
            show_alert=False,
        )

        keyboard = stats_keyboard(uid) if status == "running" else None

        await query.edit_message_text(
            f"📊 *Session Stats*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"⏱ *Interval:* {interval}s between requests\n"
            f"📤 *Sent:* {sent} / {total}\n"
            f"📬 *Remaining:* {remaining}\n"
            f"🔄 *Status:* {status_icon}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

# ---------------------------------------------------------------------------
# Background task: send HTTP requests
# ---------------------------------------------------------------------------

async def send_requests_task(
    bot,
    uid: int,
    chat_id: int,
    url: str,
    total: int,
    interval: float,
):
    """Sends `total` GET requests to `url` with `interval` seconds between each.
    Updates MongoDB after every request. Notifies the user on completion."""
    sent    = 0
    errors  = 0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    ) as session:
        for i in range(total):
            try:
                async with session.get(url, ssl=False) as resp:
                    status_code = resp.status
                    logger.info("Request %d/%d → %s [%d]", i + 1, total, url, status_code)
            except Exception as e:
                errors += 1
                logger.warning("Request %d/%d failed: %s", i + 1, total, e)

            sent += 1
            update_session(uid, {"sent_requests": sent})

            # Wait before the next request (skip sleep after the last one)
            if i < total - 1:
                await asyncio.sleep(interval)

    # All done
    final_status = "done" if errors == 0 else "done_with_errors"
    update_session(uid, {"status": "done", "sent_requests": sent})

    summary = (
        f"✅ *All done!*\n\n"
        f"🌐 Target: `{url}`\n"
        f"📦 Sent: *{sent}* / {total} requests\n"
    )
    if errors:
        summary += f"⚠️ Errors: *{errors}*\n"
    summary += "\nSend another URL to run a new session."

    try:
        await bot.send_message(chat_id, summary, parse_mode="Markdown")
    except Exception as e:
        logger.error("Could not notify user %d on completion: %s", uid, e)

# ---------------------------------------------------------------------------
# Self-ping (keep Render free tier alive)
# ---------------------------------------------------------------------------

_self_ping_url: str | None = None


async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    """Pings this service's own URL every 10 min to prevent Render cold starts."""
    if not _self_ping_url:
        return
    try:
        req = urllib.request.Request(_self_ping_url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Self-ping OK (%s)", resp.status)
    except urllib.error.HTTPError as e:
        logger.info("Self-ping HTTP %s (service alive)", e.code)
    except Exception:
        logger.exception("Self-ping failed")

# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)

# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )
    app.add_error_handler(on_error)

    return app

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _self_ping_url

    app = build_app()

    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    on_render  = os.environ.get("RENDER", "") == "true"
    webhook_target = WEBHOOK_URL or render_url

    if on_render and not webhook_target:
        raise RuntimeError(
            "Running on Render but WEBHOOK_URL / RENDER_EXTERNAL_URL is not set. "
            "Add WEBHOOK_URL in your Render environment variables."
        )

    if webhook_target:
        webhook_base     = webhook_target.rstrip("/")
        if not webhook_base.startswith(("http://", "https://")):
            webhook_base = f"https://{webhook_base}"
        full_webhook_url = f"{webhook_base}/{BOT_TOKEN}"

        logger.info("Webhook mode — port %s", PORT)
        logger.info("Webhook URL: %s", full_webhook_url)

        # Self-ping every 10 minutes (Render idles after ~15 min)
        _self_ping_url = webhook_base
        app.job_queue.run_repeating(self_ping, interval=600, first=60)

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logger.info("Polling mode (no WEBHOOK_URL / RENDER_EXTERNAL_URL detected)")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
