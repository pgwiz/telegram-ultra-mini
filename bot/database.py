"""Neon Serverless PostgreSQL Database Manager with Cold-Start Resilience.

Designed specifically for Neon Lakebase Postgres:
- Uses asyncpg with statement_cache_size=0 for PgBouncer pooled compatibility.
- Automatic exponential backoff retries on cold-start compute resume.
- Transient connection error recovery on query execution.
- Optional keep-alive heartbeat to prevent idle scale-to-zero during active periods.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import asyncpg
from bot.config import settings

logger = logging.getLogger(__name__)

# Transient errors commonly encountered during Neon scale-to-zero compute wake-up
NEON_TRANSIENT_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.InterfaceError,
    ConnectionResetError,
    ConnectionRefusedError,
    asyncio.TimeoutError,
    OSError
)


class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self.pool is not None

    async def connect(self) -> None:
        """
        Connect to Neon PostgreSQL with automatic exponential backoff
        to accommodate scale-to-zero cold starts.
        """
        async with self._lock:
            if self.pool is not None:
                return

            db_url = settings.DATABASE_URL
            if not db_url or not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
                raise ValueError(f"Invalid DATABASE_URL for Neon PostgreSQL: {db_url}")

            max_retries = 5
            base_delay = 1.5

            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"Connecting to Neon PostgreSQL (attempt {attempt}/{max_retries})...")
                    # statement_cache_size=0 is REQUIRED for PgBouncer / Neon pooled connections
                    self.pool = await asyncpg.create_pool(
                        dsn=db_url,
                        min_size=settings.DB_POOL_MIN_SIZE,
                        max_size=settings.DB_POOL_MAX_SIZE,
                        timeout=30.0,
                        command_timeout=30.0,
                        statement_cache_size=0,
                        max_inactive_connection_lifetime=300.0
                    )
                    
                    # Verify compute is active
                    async with self.pool.acquire() as conn:
                        await conn.fetchval("SELECT 1;")

                    logger.info("Neon PostgreSQL connected and active.")
                    await self._migrate()
                    
                    # Start optional background keepalive if configured
                    if getattr(settings, "ENABLE_NEON_KEEPALIVE", False) and not self._keepalive_task:
                        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                    return

                except NEON_TRANSIENT_ERRORS as e:
                    logger.warning(f"Neon cold-start / wake-up delay on attempt {attempt}: {e}")
                    if attempt < max_retries:
                        sleep_time = base_delay * (2 ** (attempt - 1))
                        logger.info(f"Waiting {sleep_time:.1f}s for Neon compute to spin up...")
                        await asyncio.sleep(sleep_time)
                    else:
                        logger.error("Failed to connect to Neon PostgreSQL after retries.")
                        raise

    async def disconnect(self) -> None:
        """Gracefully cancel keepalive and close connection pool."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        if self.pool:
            await self.pool.close()
            self.pool = None
            logger.info("Neon PostgreSQL connection pool closed.")

    async def _keepalive_loop(self) -> None:
        """Lightweight background heartbeat every 4m to prevent cold start during active use."""
        try:
            while True:
                await asyncio.sleep(240)  # Every 4 minutes (Neon default idle sleep is 5m)
                if self.pool:
                    try:
                        async with self.pool.acquire() as conn:
                            await conn.fetchval("SELECT 1;")
                        logger.debug("Neon keepalive ping successful.")
                    except Exception as e:
                        logger.debug(f"Neon keepalive ping skipped: {e}")
        except asyncio.CancelledError:
            pass

    async def _execute_with_retry(self, callback):
        """Execute a database operation with auto-retry on cold-start disconnect."""
        retries = 2
        for attempt in range(1, retries + 1):
            if not self.pool:
                await self.connect()
            try:
                async with self.pool.acquire() as conn:
                    return await callback(conn)
            except NEON_TRANSIENT_ERRORS as e:
                logger.warning(f"Transient DB error during query (attempt {attempt}): {e}")
                if attempt < retries:
                    # Invalidate pool and reconnect if connection dropped
                    if self.pool:
                        try:
                            await self.pool.close()
                        except Exception:
                            pass
                        self.pool = None
                    await asyncio.sleep(1.0)
                    await self.connect()
                else:
                    raise

    # ── Migrations ──────────────────────────────────────────────────────────

    async def _migrate(self) -> None:
        """Run idempotent schema migrations."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_active TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS channel_storage (
                    id SERIAL PRIMARY KEY,
                    track_id TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    channel_msg_id BIGINT NOT NULL,
                    telegram_file_id TEXT,
                    title TEXT,
                    artist TEXT,
                    duration_secs INT,
                    file_size_bytes BIGINT,
                    source TEXT DEFAULT 'api',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT uq_track_quality UNIQUE (track_id, quality)
                );
                CREATE INDEX IF NOT EXISTS idx_channel_storage_track ON channel_storage(track_id);

                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_json JSONB NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at);

                CREATE TABLE IF NOT EXISTS download_history (
                    id SERIAL PRIMARY KEY,
                    user_chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    track_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    channel_msg_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_download_history_user ON download_history(user_chat_id);

                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_chat_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    attempt_count INT DEFAULT 1,
                    window_start TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_chat_id, action)
                );
            """)

    # ── Channel Storage Operations ──────────────────────────────────────────

    async def get_cached_track(self, track_id: str, quality: str) -> Optional[Dict[str, Any]]:
        """Lookup stored channel message by track_id and quality with cold-start retry."""
        async def op(conn):
            row = await conn.fetchrow(
                """
                SELECT id, track_id, quality, channel_msg_id, telegram_file_id,
                       title, artist, duration_secs, file_size_bytes, source, created_at
                FROM channel_storage
                WHERE track_id = $1 AND quality = $2
                """,
                track_id, quality
            )
            return dict(row) if row else None

        return await self._execute_with_retry(op)

    async def save_cached_track(
        self,
        track_id: str,
        quality: str,
        channel_msg_id: int,
        telegram_file_id: Optional[str] = None,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        duration_secs: Optional[int] = None,
        file_size_bytes: Optional[int] = None,
        source: str = "api"
    ) -> None:
        """Store or update track channel message ID and metadata."""
        async def op(conn):
            await conn.execute(
                """
                INSERT INTO channel_storage (
                    track_id, quality, channel_msg_id, telegram_file_id,
                    title, artist, duration_secs, file_size_bytes, source
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (track_id, quality) DO UPDATE SET
                    channel_msg_id = EXCLUDED.channel_msg_id,
                    telegram_file_id = COALESCE(EXCLUDED.telegram_file_id, channel_storage.telegram_file_id),
                    title = COALESCE(EXCLUDED.title, channel_storage.title),
                    artist = COALESCE(EXCLUDED.artist, channel_storage.artist),
                    duration_secs = COALESCE(EXCLUDED.duration_secs, channel_storage.duration_secs),
                    file_size_bytes = COALESCE(EXCLUDED.file_size_bytes, channel_storage.file_size_bytes),
                    source = EXCLUDED.source;
                """,
                track_id, quality, channel_msg_id, telegram_file_id,
                title, artist, duration_secs, file_size_bytes, source
            )

        await self._execute_with_retry(op)

    # ── Persistent API Cache Operations ────────────────────────────────────

    async def get_api_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve unexpired cached API response."""
        async def op(conn):
            row = await conn.fetchrow(
                """
                SELECT response_json FROM api_cache
                WHERE cache_key = $1 AND expires_at > NOW()
                """,
                cache_key
            )
            if row:
                data = row["response_json"]
                return json.loads(data) if isinstance(data, str) else data
            return None

        return await self._execute_with_retry(op)

    async def set_api_cache(self, cache_key: str, data: Dict[str, Any], ttl_seconds: int) -> None:
        """Store API response in database cache with TTL."""
        expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
        data_json = json.dumps(data)

        async def op(conn):
            await conn.execute(
                """
                INSERT INTO api_cache (cache_key, response_json, expires_at)
                VALUES ($1, $2::jsonb, $3)
                ON CONFLICT (cache_key) DO UPDATE SET
                    response_json = EXCLUDED.response_json,
                    expires_at = EXCLUDED.expires_at;
                """,
                cache_key, data_json, expires_at
            )

        await self._execute_with_retry(op)

    # ── User & History Operations ──────────────────────────────────────────

    async def upsert_user(self, chat_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        """Record or update user activity."""
        is_admin = (chat_id == settings.ADMIN_CHAT_ID)

        async def op(conn):
            await conn.execute(
                """
                INSERT INTO users (chat_id, username, first_name, is_admin, last_active)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_active = NOW();
                """,
                chat_id, username, first_name, is_admin
            )

        await self._execute_with_retry(op)

    async def add_history(self, user_chat_id: int, track_id: str, title: str, quality: str, channel_msg_id: int) -> None:
        """Log download to user history."""
        async def op(conn):
            await conn.execute(
                """
                INSERT INTO download_history (user_chat_id, track_id, title, quality, channel_msg_id)
                VALUES ($1, $2, $3, $4, $5)
                """,
                user_chat_id, track_id, title, quality, channel_msg_id
            )

        await self._execute_with_retry(op)

    async def get_user_history(self, user_chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve recent downloads for a user."""
        async def op(conn):
            rows = await conn.fetch(
                """
                SELECT track_id, title, quality, channel_msg_id, created_at
                FROM download_history
                WHERE user_chat_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                user_chat_id, limit
            )
            return [dict(r) for r in rows]

        return await self._execute_with_retry(op)

    # ── Rate Limiting ──────────────────────────────────────────────────────

    async def check_rate_limit(self, user_chat_id: int, action: str, limit: int = 10, window_secs: int = 60) -> bool:
        """Return True if under rate limit, False if rate limited."""
        if user_chat_id == settings.ADMIN_CHAT_ID:
            return True

        now = datetime.utcnow()

        async def op(conn):
            row = await conn.fetchrow(
                "SELECT attempt_count, window_start FROM rate_limits WHERE user_chat_id = $1 AND action = $2",
                user_chat_id, action
            )
            if not row:
                await conn.execute(
                    "INSERT INTO rate_limits (user_chat_id, action, attempt_count, window_start) VALUES ($1, $2, 1, $3)",
                    user_chat_id, action, now
                )
                return True

            count = row["attempt_count"]
            start = row["window_start"].replace(tzinfo=None) if hasattr(row["window_start"], "tzinfo") else row["window_start"]
            if (now - start).total_seconds() > window_secs:
                await conn.execute(
                    "UPDATE rate_limits SET attempt_count = 1, window_start = $3 WHERE user_chat_id = $1 AND action = $2",
                    user_chat_id, action, now
                )
                return True

            if count >= limit:
                return False

            await conn.execute(
                "UPDATE rate_limits SET attempt_count = attempt_count + 1 WHERE user_chat_id = $1 AND action = $2",
                user_chat_id, action
            )
            return True

        return await self._execute_with_retry(op)

    async def get_stats(self) -> Dict[str, Any]:
        """Get aggregate system statistics for admin."""
        async def op(conn):
            user_count = await conn.fetchval("SELECT COUNT(*) FROM users;")
            cached_tracks = await conn.fetchval("SELECT COUNT(*) FROM channel_storage;")
            total_downloads = await conn.fetchval("SELECT COUNT(*) FROM download_history;")
            return {
                "backend": "PostgreSQL (Neon Lakebase)",
                "total_users": user_count or 0,
                "cached_tracks": cached_tracks or 0,
                "total_downloads": total_downloads or 0
            }

        return await self._execute_with_retry(op)


db = Database()
