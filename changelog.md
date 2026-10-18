# Changelog

All notable changes to **Telegram Ultra Mini** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.7] - 2026-10-18

### Fixed
- **Missing `os` Import in Playlist Handler:**
  - Added `import os` in `bot/handlers/playlist.py`, resolving the `NameError: name 'os' is not defined` crash during pipelined download cleanup.
- **Resilient Direct-to-User Delivery Fallback:**
  - Implemented `_send_direct_to_user` in `bot/storage.py`: when `bot.send_audio`/`bot.send_video` to `STORAGE_CHANNEL_ID` raises `TelegramBadRequest` (e.g. `Bad Request: chat not found`) or `TelegramForbiddenError` (bot not added as administrator to the channel), the bot automatically routes delivery directly to the user's chat.
  - Guarantees batch playlist and single-track downloads succeed without pausing or failing even if the storage channel is misconfigured, private, or inaccessible.
  - Preserves zero-disk footprint by immediately unlinking local temp audio/video files in both direct and channel pathways.
- **Automated Fallback Test Suite:**
  - Added `test_upload_and_cache_direct_user_fallback` to `test_playlist_integration.py` validating simulated `chat not found` exceptions, direct-to-user fallback routing, and instant disk unlinking.

## [1.5.6] - 2026-10-18

### Added
- **Pipelined Concurrent Download & Upload Architecture:**
  - Implemented asynchronous producer-consumer pipelining: while track $N$ is uploading to the private Telegram storage channel, track $N+1$ downloads concurrently in the background.
  - Slashes total batch execution time by eliminating dead time between sequential downloads and uploads.
- **Zero-Disk Immediate Local File Purge:**
  - Local audio/video files are now deleted immediately upon successful upload to the storage channel, before DB record save and user `copy_message` delivery.
  - Ensures local server disk footprint never exceeds 1–2 tracks (~10–20 MB total) across any playlist batch size (25, 50, 100, or All).
- **Proactive Stale Temp File Sweeper:**
  - Added `Downloader.clean_stale_files(240)` automatically sweeping orphan `.raw`, `.part`, or temporary chunk files older than 4 minutes before every download.

## [1.5.5] - 2026-10-18

### Added
- **Live In-Memory Web Logs Console (`/logs`):**
  - Added `MemoryLogHandler` capturing up to 1000 application log records in an in-memory ring buffer.
  - Added public `/logs` web endpoint displaying a dark-themed responsive terminal console with real-time state, auto-refresh, and raw text export (`/logs?format=text`).
  - Added `POST /logs/clear` endpoint.
- **Telegram Admin Logs Viewer (`/logs`):**
  - Added `/logs [limit]` admin command to view live system logs and tracebacks directly within Telegram chat.
  - Added interactive `[ View Logs ]` button to the `/admin` control panel.
- **Supervised Bot Polling Loop & Auto-Reconnect:**
  - Replaced un-supervised polling task with `polling_supervisor()` that automatically recovers from network disconnects or transient conflicts.
  - Preserved pending updates (`drop_pending_updates=False`) so user commands sent during server boot or deployment are never dropped.
- **Render Free-Tier Container Keepalive:**
  - Added background keepalive loop pinging the public `/health` endpoint every 9 minutes, preventing Render free-tier containers from spinning down to sleep.
- **Parallelized Playlist Pre-Scan:**
  - Optimized playlist pre-flight library analysis with `asyncio.gather`, executing up to 100 cache lookups in parallel in <500ms instead of 20s.

## [1.5.4] - 2026-10-18

### Fixed
- **Timeout Guards & Graceful Deployment Shutdown Handling:**
  - Added per-track download timeout guard (`120s`) in `run_playlist_download` ensuring stuck downloads or socket hangs never freeze batch execution.
  - Added subprocess execution timeout guards in `Downloader` (`45s` for ffmpeg transcode and `90s` for yt-dlp fallback).
  - Added active session notification in `bot/main.py` lifespan shutdown: when Render terminates a worker during a deployment or server restart, active in-flight playlist sessions immediately update their status message informing the user of the deployment and prompting them to resume seamlessly.

## [1.5.3] - 2026-10-17

### Added
- **Pre-Flight Library Scan & Zero Duplicate Downloads:**
  - Added `find_cached_track` in `bot/database.py` and `StorageManager.find_cached` supporting instant lookups by `track_id` or case-insensitive `(title, artist)` across MongoDB, PostgreSQL, and SQLite.
  - Playlist selection menu now inspects available tracks and displays `📦 Already in Library: {count} tracks` before the user makes a choice.
  - Batch download loop now performs a pre-flight library analysis:
    - Displays upfront breakdown: `📦 Already in Library: X tracks (instant delivery)` and `📥 New to Download: Y tracks`.
    - Delivers all existing tracks from Telegram storage channel first via `copy_message` with zero re-downloading.
    - If all selected tracks already exist in library, finishes immediately with 0 downloads required.
    - Downloads only the tracks that are missing, displaying progress `📥 Downloading [idx/Y]` for the remaining items.
    - Final completion summary details tracks delivered from library vs. newly downloaded.

## [1.5.2] - 2026-10-17

### Fixed
- **Playlist Batch Execution Crash (`AttributeError: 'PlaylistSession' object has no attribute 'chat'`):**
  - Resolved runtime crash in `run_playlist_download` where `storage_manager.deliver_cached` and `storage_manager.upload_and_cache` referenced `session.chat.id` instead of `session.chat_id`.
  - Added backward-compatible `@property def chat` on `PlaylistSession` returning a proxy object with `.id` to prevent attribute lookups from crashing.
  - Added `exc_info=True` to the playlist error handler for complete traceback visibility in production logs.
- **Upload & Storage Hardening:**
  - Added duration coercion in `StorageManager.upload_and_cache` guaranteeing integer second values (safely converting Spotify millisecond durations).
  - Added regex filename sanitization for `FSInputFile` to strip forbidden characters (`/`, `\`, `"`, etc.).
  - Added numeric string sanitization in `Settings.resolve_bot_token` for `STORAGE_CHANNEL_ID` and `ADMIN_CHAT_ID`.
  - Added detailed exception rendering in user-facing status messages for immediate error transparency.

## [1.5.1] - 2026-10-17

### Added
- **Interactive Playlist Batch Sizing & Pre-flight Inspection:**
  - Before starting any download, the bot inspects playlist metadata (title, author, total track count) and presents an interactive batch selector:
    - If playlist has $\le 25$ tracks: `[ Download All ]`, `[ Smart Mode ]`, `[ Cancel ]`.
    - If playlist has $> 25$ tracks: `[ 25 Tracks ]`, `[ 50 Tracks ]`, `[ 100 Tracks ]`, `[ All (100+) ]`, `[ Smart Mode ]`, and `[ Cancel ]`.
- **Resilient Smart Mode (Pause on Error):**
  - Download state machine that automatically pauses on track failure and prompts the user with action choices:
    - `[ Retry Track ]`: re-attempts extraction/download for the failed song (up to 3 retries).
    - `[ Skip Track ]`: gracefully skips the track and proceeds to the next song in the batch.
    - `[ Stop Batch ]`: halts the download loop immediately and outputs a summary of delivered vs. skipped tracks.
- **Telegram Bot API 9.4 Clean Button Styles:**
  - Integrated native `style` parameter on `InlineKeyboardButton` (`"success"`, `"primary"`, `"danger"`) with sleek, text-first labels without colored circle or stop emojis.
- **Enhanced 100+ Track Playlist Extraction:**
  - Integrated keyless Spotify embed extraction returning up to 100 tracks in 200ms with on-the-fly YouTube stream resolution for tracks beyond the initial 25.
  - Implemented asynchronous flat-playlist fallback extraction for YouTube playlists.
- **Telegram FloodWait & Rate Limit Resiliency:**
  - Added dedicated `TelegramRetryAfter` handlers on `bot.copy_message`, `storage_manager.upload_and_cache`, and status message edits with automatic exponential backoff sleeping.
  - Increased inter-track execution pacing from 1.0s to 2.0s to prevent Telegram Flood control triggers when processing consecutive audio uploads in large batches.

## [1.5.0] - 2026-10-17

### Added
- **MongoDB Atlas Integration & Multi-Database Namespacing:** Full native asynchronous MongoDB support powered by `motor` and `dnspython`. Supports multi-bot database isolation sharing the same cluster:
  - If a database name is specified in URI path (`mongodb+srv://.../dbname`), it is used.
  - If `MONGO_DB_NAME` is configured, it is used.
  - If omitted, the database name is automatically derived from the bot token ID (e.g. `bot_8908464066` or `bot_8126033137`), preventing collection collisions across multiple bot instances on the same cluster.
  - Tri-backend compatibility: automatic detection and full backward compatibility with MongoDB Atlas, Neon PostgreSQL, and SQLite.
  - Automatic startup index creation for unique constraints and fast lookups (`users`, `channel_storage`, `api_cache`, `download_history`, `rate_limits`).
- **PostgreSQL to MongoDB Migration CLI (`migrate_pg_to_mongo.py`):**
  - Standalone migration utility that copies all records (`users`, `channel_storage`, `download_history`, `api_cache`, `rate_limits`) from Neon PostgreSQL to MongoDB with automatic data transformation.
  - Verified 100% row match verification and duplicate-prevention on repeated runs.
  - Supports CLI flags: `--dry-run`, `--drop-target`, `--pg-url`, `--mongo-url`, `--db-name`.
- **Configuration Updates:** Added `MONGO_URL` and `MONGO_DB_NAME` settings in `bot/config.py` with quote-stripping and fallback to `DATABASE_URL`.

## [1.4.1] - 2026-10-11

### Fixed
- **Bot Token Validation & Sanitization:** Added `resolve_bot_token` model validator in `Settings` that automatically strips whitespace and accidental quotes from `TELEGRAM_BOT_TOKEN`, and cleanly falls back to `TELOXIDE_TOKEN` if defined.
- **Pre-Flight Authentication Guard:** Added proactive `bot.get_me()` credentials verification in `bot/main.py` lifespan to cleanly log invalid or revoked tokens and prevent infinite polling error loops when Telegram responds with HTTP 401 Unauthorized.

## [1.4.0] - 2026-09-26

### Added
- **Admin Track Purge (`/delete` & `/del`):** Admin-only command to delete cached tracks from both the Neon database (`channel_storage`) and the private Telegram storage channel. Accepts `#<track_id>` (e.g., copied directly from the song caption `#kJQP7kiw5Fk`), `<track_id>`, or by simply replying to any audio/video message with `/delete`.
- **Force Re-downloading (`force` argument & button):** Users can force a fresh re-download bypassing the storage cache via commands (e.g., `/download <url> force`, `/video <url> force`, `/da <url> force`, `https://... force`) and via an interactive `⚡ Force Re-download` button in the track format menu. The database reference (`channel_msg_id`) is seamlessly updated to the newly uploaded media.
- **Admin System Cleanup (`/cleanup`):** Dedicated admin command that purges orphaned local temp files in `./downloads`, deletes expired `api_cache` entries from Neon PostgreSQL, and resets the RAM cache, returning a detailed summary of freed disk space and purged records.
- **Admin Control Panel (`/admin`):** Interactive dashboard with instant buttons for real-time stats (`/stats`), one-click system cleanup (`/cleanup`), and RAM cache resetting (`/cache_clear`).
- **Dynamic Role-Based Help Menu:** `/help` automatically detects admin status and appends the `👑 Admin Control Panel` command reference for authorized administrators while keeping the interface clean for regular users.

## [1.3.0] - 2026-09-26

### Added
- **Two-Step Interactive Search UI:** Search results now render clean, uncluttered numbered buttons (1 button per track). Clicking a track edits the message into a format selector (Audio High 320k, Normal 192k, Saver 64k, Video 720p HD, Video 360p SD) with a `⬅️ Back to Search Results` button.
- **Search Session Memory:** Added search session caching in two-tier cache to allow smooth back-and-forth navigation between track selection and format choosing without re-fetching or flickering.

### Fixed
- **Audio Integrity & Malformed Playback Prevention:** Eliminated the container mismatch bug where raw MP4 streams were saved as `.mp3` and caused Telegram audio players to fail or report corrupted files. Audio requests now prioritize the pre-packaged `audio/mpeg` MP3 with complete ID3 tags from `/download`, and stream fallbacks transcode to pure MP3 (320k/192k/64k) via FFmpeg or save cleanly as `.m4a` AAC.
- **Explicit Audio vs. Video Stream Separation:** Default downloads remain strictly audio (`audio_high` 320k); video is only downloaded when explicitly triggered via `/video`, `/dv`, or chosen from the interactive format selector.

## [1.2.0] - 2026-09-26

### Added
- **Automatic Plain-Text Search:** Sending regular text (not a link, not a command) directly triggers a music catalog search with 1-click download buttons, matching Hermes behavior.
- **Native MP4 Video Downloads:** Added full support for video downloads via `ytsp-api.pgwiz.cloud` (`720p` HD and `360p` SD presets) streamed directly as MP4.
- **Video Commands & Format Selectors:** Added `/video <url>` and `/dv <url>`, expanded `/da` quality selector with HD (720p) and SD (360p) options, and added dedicated video buttons to search results.
- **Video Channel Warehousing:** Extended cloud channel archiving to handle video messages using `bot.send_video(supports_streaming=True)` with instant user delivery via `copy_message`.

## [1.1.0] - 2026-09-26

### Added
- **Neon Cold-Start & Scale-to-Zero Resilience:** Added exponential backoff connection retries (up to 5 attempts), query-level transient disconnect recovery, and optional background keepalive pings.
- **PgBouncer Pooling Support:** Explicitly set `statement_cache_size=0` on `asyncpg.create_pool` to prevent prepared statement errors on Neon's `-pooler` transaction pooling.
- **Render Production Entrypoint:** Added `wsgi.py` and `your_application/wsgi.py` using `a2wsgi.ASGIMiddleware` around FastAPI to natively support Render's default `gunicorn your_application.wsgi` start command while running aiogram polling in lifespan.
- **Dependency Fix:** Added `gunicorn>=23.0.0` to `requirements.txt` to resolve Render deploy failure (`gunicorn: command not found`).
- **Database Pool & Cache Schema Alignment:** Added `pool` alias property to `Database` and updated `api_cache` to use `response_json` column matching Neon's schema, resolving `AttributeError: 'Database' object has no attribute 'pool'`.

## [1.0.0] - 2026-09-26

### Added
- **Pure-Python Architecture:** Replaced Rust (Teloxide/Axum/Sqlx) backend from `telegram-ultra` with an asynchronous Python engine powered by `aiogram 3.x`.
- **Channel Storage Warehousing:** Implemented permanent track archiving to a dedicated Telegram storage channel with `message_id` indexing in Neon PostgreSQL. Cached tracks deliver via `copy_message` in <200ms with zero server bandwidth.
- **Neon Serverless PostgreSQL:** Integrated `asyncpg` connection pool with auto-migrations for `users`, `channel_storage`, `api_cache`, `download_history`, and `rate_limits`.
- **Stream Extractor API Client:** Built high-speed REST client for `https://ytsp-api.pgwiz.cloud` supporting `/stream/:videoId`, `/get`, `/api/search/spotify`, `/api/spotify/playlist`, and `/api/youtube/playlist`.
- **Two-Tier Caching:** Implemented in-memory `TTLCache` (RAM) with persistent Neon PostgreSQL cache (7-day metadata TTL, 24-hour search TTL).
- **Fallback Engine:** Dual-mode downloader featuring Primary ytsp-api streaming with an emergency `yt-dlp` fallback.
- **Commands & Handlers:** Added `/start`, `/help`, `/download`, `/da`, `/search`, `/playlist`, `/history`, `/ping`, `/chatid`, and `/stats`.
- **Dual GitHub Sync:** Configured Git remotes for dual synchronization to `pgwiz/telegram-ultra-mini` (Public) and `WiPTechg/telegram-ultra-mini` (Private).
