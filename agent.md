# Agent Guidelines: Telegram Ultra Mini

## Principles & Guardrails
1. **Never Hardcode Endpoints:**
   Always reference `settings.YTSP_API_BASE_URL` or environment variables for remote services.
2. **Prioritize the API Over Local Extractors:**
   As instructed, the Stream Extractor API (`ytsp-api.pgwiz.cloud`) is the primary extraction engine. `yt-dlp` is only an emergency fallback if the remote API fails.
3. **Storage Channel Warehousing:**
   Whenever delivering a track:
   - Check `storage_manager.deliver_cached(bot, user_chat_id, track_id, quality)` first.
   - If not cached, download, upload to storage channel via `storage_manager.upload_and_cache`, record message ID in Neon DB, and deliver via `bot.copy_message`.
4. **Markdown Documentation Rule:**
   Do NOT create new `.md` files unless explicitly requested. Only maintain:
   - `readme.md`
   - `changelog.md`
   - `memory.md`
   - `agent.md`
5. **Git Synchronization:**
   Dual-remote setup is configured:
   - Remote 1: `https://github.com/pgwiz/telegram-ultra-mini.git` (Public)
   - Remote 2: `https://github.com/WiPTechg/telegram-ultra-mini.git` (Private)
   Pushing to `origin` automatically pushes to both remotes.
6. **Database Architecture & Multi-Bot Namespacing:**
   - Multi-Backend: Support MongoDB Atlas (primary), Neon PostgreSQL, and SQLite seamlessly via `DATABASE_URL`.
   - Multi-Bot Isolation: Multiple bots sharing a MongoDB cluster MUST use separated database namespaces automatically derived from bot token ID (`bot_<bot_id>`) unless overridden in URI path or `MONGO_DB_NAME`.
   - Neon PostgreSQL Resilience: When using Postgres, preserve `statement_cache_size=0` in `asyncpg` for Neon transaction pooling (`-pooler`), cold-start exponential backoff on connection, and auto-retry on query disconnects.
7. **Render Entrypoint Compatibility:**
   Preserve `your_application/wsgi.py` and `wsgi.py` adapters to support Render's default `gunicorn your_application.wsgi` start command.
