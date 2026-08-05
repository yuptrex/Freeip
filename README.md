# HTTP Request Bot v2

A high-concurrency Telegram bot that fires browser-style HTTP requests at a target URL.

## What's New in v2

| Feature | v1 | v2 |
|---|---|---|
| Proxy / IP rotation | ❌ | ✅ Round-robin (http/https/socks4/socks5) |
| SSL handling | Hard `ssl=False` | ✅ `auto` mode (verified → fallback) |
| POST support | ❌ GET only | ✅ GET / POST / Mixed |
| Runtime proxy update | ❌ | ✅ `/setproxies` command |
| Runtime method switch | ❌ | ✅ `/setmethod` command |
| Accept-Language rotation | ❌ | ✅ |
| Referer rotation | ❌ | ✅ |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Telegram bot token |
| `MONGO_URI` | *(required)* | MongoDB connection string |
| `MONGO_DB_NAME` | `reqbot` | Database name |
| `WEBHOOK_URL` | *(blank = polling)* | Public URL for webhook mode |
| `PORT` | `8080` | Port to listen on |
| `PROXY_LIST` | *(blank = direct)* | Comma-separated proxies |
| `REQUEST_METHOD` | `get` | `get`, `post`, or `mixed` |
| `POST_BODY` | *(blank)* | Body sent with POST requests |
| `POST_CONTENT_TYPE` | `application/x-www-form-urlencoded` | Content-Type for POST |
| `SSL_MODE` | `auto` | `auto`, `true`, or `false` |

## Proxy Format

```
http://user:pass@1.2.3.4:8080
https://proxy.example.com:3128
socks4://1.2.3.4:1080
socks5://user:pass@1.2.3.4:1080
```

Set via `PROXY_LIST` env var (comma-separated) or the `/setproxies` command at runtime.

## Commands

- `/start` — show status and instructions
- `/setproxies` — paste new proxy list at runtime
- `/setmethod get|post|mixed` — switch request method at runtime
- `/cancel` — stop current session

## Deploying on Render

1. Push this repo to GitHub
2. Create a new **Web Service** on Render
3. Set all required environment variables
4. Set `PROXY_LIST` to your proxies for IP rotation
5. Deploy
