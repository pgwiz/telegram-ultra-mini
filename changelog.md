# Changelog

All notable changes to **Telegram Ultra Mini** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-26

### Added
- **Neon Cold-Start & Scale-to-Zero Resilience:** Added exponential backoff connection retries (up to 5 attempts), query-level transient disconnect recovery, and optional background keepalive pings.
- **PgBouncer Pooling Support:** Explicitly set `statement_cache_size=0` on `asyncpg.create_pool` to prevent prepared statement errors on Neon's `-pooler` transaction pooling.
- **Render Production Entrypoint:** Added `wsgi.py` and `your_application/wsgi.py` using `a2wsgi.ASGIMiddleware` around FastAPI to natively support Render's default `gunicorn your_application.wsgi` start command while running aiogram polling in lifespan.
- **Dependency Fix:** Added `gunicorn>=23.0.0` to `requirements.txt` to resolve Render deploy failure (`gunicorn: command not found`).

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
