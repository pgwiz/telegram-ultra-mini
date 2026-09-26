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
2. **Neon Serverless PostgreSQL:**
   - URI: Configured via `DATABASE_URL`.
   - Driver: `asyncpg` with connection pooling.
   - Scale-to-Zero & Cold-Start Handling:
     - PgBouncer compatibility: `statement_cache_size=0` on `create_pool` to avoid prepared statement conflicts on Neon `-pooler` endpoints.
     - Cold-start wakeup backoff: 5 retries with exponential backoff (1.5s, 3s, 6s, 12s, 24s) allowing suspended compute nodes to spin up.
     - Query-level retry: `_execute_with_retry` automatically catches `ConnectionResetError` or `CannotConnectNowError`, re-establishes the pool, and re-executes.
     - Keep-alive ping loop: Optional background task executes `SELECT 1;` every 240 seconds to prevent compute scale-to-zero when desired.
   - Tables:
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
- **Emergency Fallback:** If `ytsp-api` is unreachable and `ENABLE_API_FALLBACK=true`, falls back to local `yt-dlp`.

## Repositories
- Public: `https://github.com/pgwiz/telegram-ultra-mini.git`
- Private: `https://github.com/WiPTechg/telegram-ultra-mini.git`
- Git remotes configured to push to both targets simultaneously.
