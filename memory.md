# Project Memory: Telegram Ultra Mini

## Overview
- **Project Name:** Telegram Ultra Mini
- **Architecture:** Pure-Python (Async) Telegram Bot powered by `aiogram 3.x` with channel warehousing and serverless Neon PostgreSQL.
- **Source Codebase:** Ported and simplified from `telegram-ultra` (Rust + Python worker + Node UI).
- **Core Philosophy:** Minimal memory footprint, no Rust compilation dependencies, zero duplicate downloads via Telegram storage channel message copying (`copy_message`), smart caching (RAM + PostgreSQL), keyless Spotify/YouTube extraction.

## External Services & Endpoints
1. **Stream Extractor API:**
   - Base URL: `https://ytsp-api.pgwiz.cloud` (configurable via `YTSP_API_BASE_URL` env variable, never hardcoded).
   - Endpoints:
     - `/stream/:videoId?quality=...`: Metadata & playable stream URL.
     - `/get?ytl=...&quality=...`: Direct stream extraction for YouTube and Spotify URLs.
     - `/api/search/spotify?query=...`: Fast music catalog search with matched YouTube videoId.
     - `/api/spotify/playlist/:id`: Keyless Spotify playlist extraction.
     - `/api/youtube/playlist`: YouTube playlist extraction.
     - `/stream/play`: Audio proxy stream.
2. **MongoDB Atlas (Primary) & Tri-Backend Database Architecture:**
   - URI: Configured via `DATABASE_URL` (or alias `MONGO_URL`).
   - Driver: `motor` (asynchronous) with `pymongo` and `dnspython`.
   - Multi-Bot Database Isolation:
     - Allows multiple bots to connect to the same MongoDB cluster with zero collection conflicts.
     - Auto-derivation priority:
       1. Path in URI (`mongodb+srv://.../dbname?retryWrites=true`)
       2. `MONGO_DB_NAME` setting/env override
       3. Bot token ID extracted from `TELEGRAM_BOT_TOKEN` or `TELOXIDE_TOKEN` (e.g. `bot_8908464066` or `bot_8126033137`)
       4. Fallback default: `telegram_ultra`
     - Auto-indexing on startup for `users` (`chat_id` unique), `channel_storage` (`track_id` + `quality` unique), `api_cache` (`cache_key` unique, `expires_at`), `download_history`, and `rate_limits`.
   - Migration Tool: `migrate_pg_to_mongo.py` transfers records from Neon PostgreSQL to MongoDB Atlas with 100% row match verification.
   - Fallback Compatibility: Full backward compatibility is preserved for Neon Serverless PostgreSQL (`asyncpg`) and SQLite (`aiosqlite`).
   - Collections / Tables:
     - `users`: Registered users and admin status.
     - `channel_storage`: Keyed by `(track_id, quality)`, stores `channel_msg_id`, `telegram_file_id`, and metadata.
     - `api_cache`: Persistent JSON cache for metadata (7-day TTL) and search results (24-hour TTL).
     - `download_history`: User download logs.
     - `rate_limits`: Per-user rate limiting.
3. **Render Deployment & WSGI Compatibility:**
   - Entrypoint: `wsgi.py` and `your_application/wsgi.py` wrap the FastAPI app in `a2wsgi.ASGIMiddleware(app)`.
   - Runs cleanly under Render's default command: `gunicorn your_application.wsgi` or standard `uvicorn bot.main:app`.
   - Aiogram polling runs as a background task within FastAPI lifespan with `/` and `/health` HTTP endpoints responding for Render health checks.
4. **Storage Channel Warehousing:**
   - Channel ID: Configured via `STORAGE_CHANNEL_ID`.
   - Bot uploads every downloaded track to this private channel.
   - Saves `channel_msg_id` in Neon PostgreSQL.
   - Any future request for the same track and quality is delivered in <200ms using `bot.copy_message(user_chat_id, STORAGE_CHANNEL_ID, channel_msg_id)` without re-downloading or consuming server bandwidth.

## Download & Stream Packaging Strategy
- **Audio Integrity Guarantee:**
  - Requests for Audio (`audio_high` 320k, `audio` 192k, `saver` 64k) prioritize `POST /download` to retrieve pre-packaged, ID3-tagged `audio/mpeg` MP3 files.
  - If streaming from `/stream/play` is used and the stream is an MP4 ISO container, it is converted via FFmpeg (`-vn -c:a libmp3lame -b:a <bitrate>`) to a verified MP3, or kept with a proper `.m4a` AAC container so Telegram audio players never encounter malformed container mismatches.
- **Video Packaging:**
  - Requests for Video (`720p` HD, `360p` SD) stream genuine MP4 video files and upload via `bot.send_video(supports_streaming=True)`.
  - Default downloads remain Audio; Video is only triggered when explicitly requested via `/video`, `/dv`, or chosen in the interactive format selector.
- **Two-Step Interactive Search UI:**
  - Searching outputs numbered track buttons (1 button per song).
  - Tapping a song edits the message into an Audio vs. Video format selector with a `⬅️ Back to Search Results` button.
- **Force Re-downloading:**
  - Appending `force` to commands (`/download <url> force`, `/video <url> force`, `/da <url> force`) or clicking `[ ⚡ Force Re-download ]` in the UI bypasses storage cache.
  - The freshly downloaded media updates the `channel_storage` row in Neon DB on conflict (`ON CONFLICT (track_id, quality) DO UPDATE SET channel_msg_id = ...`).
- **Admin Commands & System Maintenance:**
  - `/delete <#track_id>` / `/del <#track_id>`: Purges track records from Neon DB and deletes messages from the private Telegram storage channel.
  - `/cleanup`: Purges leftover temp files in `./downloads`, clears expired `api_cache` entries from Neon DB, and flushes RAM cache.
  - `/admin`: Interactive Control Panel dashboard.
  - Role-based `/help`: Appends administrative commands only for users matching `ADMIN_CHAT_ID` or flagged as admin in `users` table.
- **Interactive Playlist Batch Sizing & Smart Mode:**
  - When a playlist link is received (`/playlist <url>`, `/da <url>`, or direct link), the bot inspects the metadata and presents a batch sizing menu:
    - $\le 25$ tracks: `Download All`, `Smart Mode`, `Cancel`.
    - $> 25$ tracks: `25`, `50`, `100`, `All (100+)`, `Smart Mode`, and `Cancel`.
  - In Smart Mode, any track failure pauses the batch and prompts user: `[ 🔄 Retry Track ]`, `[ ⏭️ Skip Track ]`, `[ 🛑 Stop Batch ]`.
  - Supported with Bot API 9.4 semantic button styles (`success`, `primary`, `danger`) and emoji color badges.
  - Spotify playlists support 100+ tracks via keyless embed parsing and on-the-fly stream resolution.
  - Rate limiting & FloodWait resilience: Consecutive audio uploads and status edits enforce 2.0s pacing and exponential backoff retry loops on `TelegramRetryAfter` exceptions to prevent Telegram flood-control stalls.
- **Emergency Fallback:** If `ytsp-api` is unreachable and `ENABLE_API_FALLBACK=true`, falls back to local `yt-dlp`.

## Repositories
- Public: `https://github.com/pgwiz/telegram-ultra-mini.git`
- Private: `https://github.com/WiPTechg/telegram-ultra-mini.git`
- Git remotes configured to push to both targets simultaneously.
