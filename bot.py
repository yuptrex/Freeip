"""
Self-Ping Keepalive Bot (conversational)
-----------------------------------------
Telegram utility bot for keeping your own server warm. Walks the user
through three steps in plain conversation, one message at a time:

    Bot: Send me the link or IPv4 address to ping.
    User: https://myapp.onrender.com
    Bot: How many requests should I send?
    User: 50
    Bot: Over how many seconds?
    User: 10
    Bot: [sends 50 requests spread across 10 seconds, then reports results]

No slash-command arguments, no <angle-bracket> syntax to remember - just
answer each question as it's asked. /cancel abandons a conversation in
progress.

- count is capped at MAX_REQUESTS_PER_JOB and seconds at
  MAX_WINDOW_SECONDS (see Config) to keep this a lightweight keepalive
  tool, not a load generator.
- Only one job runs per chat at a time.

Run mode: single aiohttp web server bound to $PORT (Render "Web Service").
Telegram delivers updates via webhook to /webhook/<BOT_TOKEN> on that same
server, rather than the bot polling Telegram in a background task -
polling can silently die while the web server still answers health
checks, so webhook mode is more reliable on Render's free tier.

Requires the WEBHOOK_URL env var (this service's own public HTTPS URL,
e.g. https://yourbot.onrender.com) so Telegram knows where to send
updates.

Self-ping: this service pings its own /health endpoint every 10 minutes
so Render's free tier doesn't spin it down between uses.
"""

import asyncio
import ipaddress
import logging
import os
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]

# The public base URL of THIS Render service, e.g. https://mybot.onrender.com
BASE_URL = os.environ["BASE_URL"].rstrip("/")

# Webhook target - falls back to BASE_URL if not set separately.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", BASE_URL).rstrip("/")

PORT = int(os.environ.get("PORT", "8080"))

# How often this service pings its own /health to avoid Render free-tier
# cold starts.
SELF_PING_INTERVAL_SECONDS = int(os.environ.get("SELF_PING_INTERVAL_SECONDS", "600"))  # 10 min

# Guardrails on user-requested jobs, so this stays a keepalive tool and
# not an accidental load generator / flooding tool.
MAX_REQUESTS_PER_JOB = int(os.environ.get("MAX_REQUESTS_PER_JOB", "10000"))
MAX_WINDOW_SECONDS = int(os.environ.get("MAX_WINDOW_SECONDS", "3600"))  # 1 hour
REQUEST_TIMEOUT_SECONDS = 10

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pingerbot")

# chat_id -> asyncio.Task, so we can prevent overlapping jobs per chat.
active_jobs: dict[int, asyncio.Task] = {}

# Conversation states.
ASK_TARGET, ASK_COUNT, ASK_SECONDS = range(3)


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------
def normalize_target(text: str) -> str | None:
    """Turns a bare IPv4 address or a URL into a fetchable http(s) URL.
    Returns None if it's neither."""
    text = text.strip()

    # Bare IPv4 address -> default to plain HTTP on that address.
    try:
        ipaddress.IPv4Address(text)
        return f"http://{text}/"
    except ValueError:
        pass

    # URL with scheme.
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text

    # Bare domain with a path but no scheme, e.g. "example.com" - default
    # to https.
    parsed_guess = urlparse(f"https://{text}")
    if parsed_guess.netloc:
        return f"https://{text}"

    return None


# ---------------------------------------------------------------------------
# Ping job
# ---------------------------------------------------------------------------
async def run_ping_job(
    session: aiohttp.ClientSession,
    chat_id: int,
    target_url: str,
    count: int,
    window_seconds: int,
    bot,
):
    """Fires `count` GET requests spread evenly across `window_seconds`,
    then stops. Reports a short summary line per request and a final
    summary."""
    interval = window_seconds / (count - 1) if count > 1 else 0
    results = []

    for i in range(count):
        start = time.monotonic()
        try:
            async with session.get(
                target_url,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                elapsed_ms = round((time.monotonic() - start) * 1000)
                results.append(f"#{i + 1} → {resp.status} ({elapsed_ms}ms)")
        except asyncio.TimeoutError:
            results.append(f"#{i + 1} → timed out")
        except Exception as e:
            results.append(f"#{i + 1} → error: {e}")

        if i < count - 1:
            await asyncio.sleep(interval)

    # Keep the message readable even for larger counts.
    shown = results if len(results) <= 20 else results[:20] + [f"… and {len(results) - 20} more"]
    summary = "\n".join(shown)

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ Done — sent {count} request(s) to `{target_url}` over {window_seconds}s:\n\n{summary}",
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to send completion message to chat_id=%s", chat_id)

    active_jobs.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Conversation: link -> count -> seconds -> run
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_jobs and not active_jobs[chat_id].done():
        await update.message.reply_text("A job is already running for this chat — wait for it to finish.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📡 Send me the link or IPv4 address you want to keep alive.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_TARGET


async def receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    target_url = normalize_target(text)
    if not target_url:
        await update.message.reply_text(
            "That doesn't look like a valid link or IPv4 address. Try again — "
            "e.g. https://myapp.onrender.com or 203.0.113.10"
        )
        return ASK_TARGET

    context.user_data["target_url"] = target_url
    await update.message.reply_text("How many requests should I send?")
    return ASK_COUNT


async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a whole number, e.g. 50")
        return ASK_COUNT

    count = int(text)
    if count < 1 or count > MAX_REQUESTS_PER_JOB:
        await update.message.reply_text(f"Count must be between 1 and {MAX_REQUESTS_PER_JOB}. Try again.")
        return ASK_COUNT

    context.user_data["count"] = count
    await update.message.reply_text("Over how many seconds should those be spread?")
    return ASK_SECONDS


async def receive_seconds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a whole number of seconds, e.g. 10")
        return ASK_SECONDS

    window_seconds = int(text)
    if window_seconds < 0 or window_seconds > MAX_WINDOW_SECONDS:
        await update.message.reply_text(f"Seconds must be between 0 and {MAX_WINDOW_SECONDS}. Try again.")
        return ASK_SECONDS

    chat_id = update.effective_chat.id
    target_url = context.user_data["target_url"]
    count = context.user_data["count"]

    if chat_id in active_jobs and not active_jobs[chat_id].done():
        await update.message.reply_text("A job is already running for this chat — wait for it to finish.")
        return ConversationHandler.END

    await update.message.reply_text(
        f"📡 Sending {count} request(s) to `{target_url}` over {window_seconds}s…",
        parse_mode="Markdown",
    )

    session: aiohttp.ClientSession = context.application.bot_data["http_session"]
    task = asyncio.create_task(
        run_ping_job(session, chat_id, target_url, count, window_seconds, context.bot)
    )
    active_jobs[chat_id] = task

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception while processing update: %s", update, exc_info=context.error)


# ---------------------------------------------------------------------------
# Web server (health check + Telegram webhook + self-ping)
# ---------------------------------------------------------------------------
async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def handle_telegram_webhook(request: web.Request) -> web.Response:
    application: Application = request.app["telegram_app"]
    try:
        data = await request.json()
    except Exception:
        logger.exception("Failed to parse incoming Telegram webhook payload")
        return web.Response(status=400)

    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(status=200)


async def self_ping_loop(app: web.Application):
    session: aiohttp.ClientSession = app["http_session"]
    url = f"{BASE_URL}/health"
    while True:
        await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                logger.info("Self-ping %s -> %s", url, resp.status)
        except Exception:
            logger.warning("Self-ping to %s failed", url, exc_info=True)


async def start_self_ping(app: web.Application):
    app["self_ping_task"] = asyncio.create_task(self_ping_loop(app))


async def stop_self_ping(app: web.Application):
    task = app.get("self_ping_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def on_startup(app: web.Application):
    application: Application = app["telegram_app"]
    application.bot_data["http_session"] = app["http_session"]
    await application.initialize()
    await application.start()

    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
    await application.bot.set_webhook(
        url=full_webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    logger.info("Webhook registered: %s", full_webhook_url)


async def on_cleanup(app: web.Application):
    application: Application = app["telegram_app"]
    try:
        await application.bot.delete_webhook()
    except Exception:
        logger.exception("Failed to delete webhook on shutdown")
    await application.stop()
    await application.shutdown()


def build_telegram_app() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={
            ASK_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target)],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_count)],
            ASK_SECONDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_seconds)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    application.add_handler(conv_handler)
    application.add_error_handler(on_error)
    return application


def build_web_app() -> web.Application:
    app = web.Application()
    app["telegram_app"] = build_telegram_app()

    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post(f"/webhook/{BOT_TOKEN}", handle_telegram_webhook)

    async def init_http_session(app: web.Application):
        app["http_session"] = aiohttp.ClientSession()

    async def close_http_session(app: web.Application):
        await app["http_session"].close()

    app.on_startup.append(init_http_session)
    app.on_startup.append(on_startup)
    app.on_startup.append(start_self_ping)
    app.on_cleanup.append(stop_self_ping)
    app.on_cleanup.append(on_cleanup)
    app.on_cleanup.append(close_http_session)

    return app


def main():
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
