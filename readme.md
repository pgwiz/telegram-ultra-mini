# Telegram Ultra Mini ⚡

> Ultra-fast, lightweight pure-Python Telegram bot with cloud channel warehousing and stream extraction.

[![Public Repo](https://img.shields.io/badge/GitHub-pgwiz%2Ftelegram--ultra--mini-blue.svg)](https://github.com/pgwiz/telegram-ultra-mini)
[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Aiogram Version](https://img.shields.io/badge/aiogram-3.x-green.svg)](https://docs.aiogram.dev/)
[![Database](https://img.shields.io/badge/Database-Neon%20PostgreSQL-00e599.svg)](https://neon.tech/)

---

## 🚀 Key Highlights

- **Pure Python, Zero Rust:** Built on `aiogram 3.x` and `asyncpg` for maximum simplicity, maintainability, and low memory consumption.
- **Cloud Channel Storage Warehousing:** Every downloaded track is forwarded/uploaded to a private Telegram storage channel, with message IDs tracked in Neon PostgreSQL. Re-requested tracks are delivered in milliseconds via Telegram's `copy_message` without re-downloading or consuming server bandwidth.
- **Keyless Stream Extractor API:** Deep integration with `https://ytsp-api.pgwiz.cloud` for YouTube & Spotify stream extraction, direct playable links, and Spotify embeds.
- **Two-Tier Smart Caching:** High-speed in-memory TTLCache (RAM) paired with persistent Neon PostgreSQL tables (7-day metadata TTL, 24-hour search TTL) to avoid redundant API recalls.
- **Fallback Safety Net:** Automatic emergency fallback to local `yt-dlp` if the remote streaming API is unreachable.
- **Interactive UI & Search:** 1-click downloads directly from inline search results (`/search`) and quality selection modals (`/da`).

---

## 🛠️ Architecture

```
                          Telegram User
                                │
                                ▼
                       ┌─────────────────┐
                       │  Aiogram 3 Bot  │
                       └────────┬────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
    ┌─────────────────┐┌─────────────────┐┌─────────────────┐
    │  Neon Postgres  ││ Storage Channel ││  Stream API     │
    │  (asyncpg pool) ││ (copy_message)  ││ (ytsp-api.cloud)│
    └─────────────────┘└─────────────────┘└─────────────────┘
             │                  │                  │
             └──────────┬───────┴──────────────────┘
                        ▼
            Instant <200ms Delivery
```

---

## 📋 Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` | Welcome message and bot introduction |
| `/help` | Detailed help and feature guide |
| `/download <url>` | Download audio from YouTube or Spotify |
| `/da <url>` | Download with quality selector (`audio_high`, `audio`, `saver`) |
| `/search <query>` | Search tracks with 1-click inline download buttons |
| `/playlist <url>` | Batch download playlist tracks with live progress |
| `/history` | View your recent download history |
| `/ping` | Health check with database latency and RAM cache size |
| `/chatid` | Display your Telegram chat ID |
| `/stats` | Admin system metrics and channel storage count |

> 💡 **Auto Link Detection:** Users can simply paste any YouTube or Spotify link directly into the chat. The bot detects and processes it automatically.

---

## ⚙️ Environment Configuration

Copy `.env.example` to `.env`:

```env
# --- Telegram Bot ---
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
ADMIN_CHAT_ID=your_telegram_user_id

# --- Storage Channel ---
STORAGE_CHANNEL_ID=-100xxxxxxxxxx

# --- Stream Extractor API ---
YTSP_API_BASE_URL=https://ytsp-api.pgwiz.cloud
YTSP_API_TIMEOUT=30
ENABLE_API_FALLBACK=true

# --- Neon PostgreSQL ---
DATABASE_URL=postgresql://neondb_owner:password@ep-...neon.tech/neondb?sslmode=require

# --- Telethon / MTProto (Optional for files >50MB) ---
MPROTO=false
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=
MTPROTO_SESSION_PATH=./sessions/hermes_session
```

---

## 🚀 Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/pgwiz/telegram-ultra-mini.git
   cd telegram-ultra-mini
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .\.venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the bot:**
   ```bash
   python -m bot.main
   ```

---

## ☁️ Deploying to Render

Render runs Gunicorn web services natively. Telegram Ultra Mini includes built-in WSGI/ASGI adapters:

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:**
  ```bash
  gunicorn your_application.wsgi
  ```
  *(Alternatively: `gunicorn wsgi:app` or `uvicorn bot.main:app --host 0.0.0.0 --port $PORT`)*

The FastAPI server serves Render's HTTP health checks on `/health` and `/` while running the Telegram bot polling process in the background.

---

## ⚡ Neon PostgreSQL Cold-Start Resilience

Neon serverless computes automatically scale to zero after idle periods. Telegram Ultra Mini handles this natively:
- **PgBouncer Pooling:** Uses `statement_cache_size=0` on `asyncpg` to prevent prepared statement errors on pooled connection strings.
- **Wakeup Retry Loop:** Retries connection creation up to 5 times with exponential backoff while suspended Neon computes resume.
- **Query Re-execution:** Automatically catches transient connection reset errors during scale-up and re-executes queries seamlessly.
- **Optional Keep-Alive:** Pings the database every 240 seconds (`ENABLE_DB_KEEPALIVE=true`) to keep the compute active if desired.

---

## 🔄 Dual Git Sync (Public + Private)

The repository is configured with dual remotes:
- **Public:** `https://github.com/pgwiz/telegram-ultra-mini.git`
- **Private:** `https://github.com/WiPTechg/telegram-ultra-mini.git`

Pushing to `origin` updates both targets simultaneously:
```bash
git push origin main
```

---

## 📄 License

MIT
