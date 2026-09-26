"""Database management supporting Neon PostgreSQL (asyncpg) and SQLite (aiosqlite)."""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import asyncpg
import aiosqlite
from bot.config import settings

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.is_postgres = False
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.sqlite_conn: Optional[aiosqlite.Connection] = None
        self.sqlite_path: str = "./hermes.db"

    @property
    def is_connected(self) -> bool:
        return (self.pg_pool is not None) if self.is_postgres else (self.sqlite_conn is not None)

    async def connect(self) -> None:
        """Initialize connection pool/connection and apply schema migrations."""
        if self.is_connected:
            return

        db_url = settings.DATABASE_URL or ""
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            self.is_postgres = True
            logger.info("Connecting to Neon PostgreSQL...")
            self.pg_pool = await asyncpg.create_pool(
                dsn=db_url,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.DB_POOL_MAX_SIZE,
                timeout=20.0
            )
            await self._migrate_postgres()
            logger.info("Neon PostgreSQL connected and schema migrated.")
        else:
            self.is_postgres = False
            # Resolve SQLite path
            if db_url.startswith("sqlite:///"):
                self.sqlite_path = db_url.replace("sqlite:///", "")
            elif settings.DATABASE_PATH:
                self.sqlite_path = settings.DATABASE_PATH
            else:
                self.sqlite_path = "./hermes.db"

            logger.info(f"Connecting to SQLite database: {self.sqlite_path}")
            self.sqlite_conn = await aiosqlite.connect(self.sqlite_path, check_same_thread=False)
            self.sqlite_conn.row_factory = aiosqlite.Row
            await self.sqlite_conn.execute("PRAGMA journal_mode = WAL;")
            await self.sqlite_conn.execute("PRAGMA busy_timeout = 10000;")
            await self.sqlite_conn.commit()
            await self._migrate_sqlite()
            logger.info("SQLite connected and schema migrated.")

    async def disconnect(self) -> None:
        """Close connection or pool."""
        if self.is_postgres and self.pg_pool:
            await self.pg_pool.close()
            self.pg_pool = None
            logger.info("Neon PostgreSQL connection closed.")
        elif self.sqlite_conn:
            await self.sqlite_conn.close()
            self.sqlite_conn = None
            logger.info("SQLite connection closed.")

    # ── Migrations ──────────────────────────────────────────────────────────

    async def _migrate_postgres(self) -> None:
        """Run PostgreSQL migrations."""
        async with self.pg_pool.acquire() as conn:
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

    async def _migrate_sqlite(self) -> None:
        """Run SQLite migrations."""
        await self.sqlite_conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_admin BOOLEAN DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_active DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channel_storage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id TEXT NOT NULL,
                quality TEXT NOT NULL,
                channel_msg_id INTEGER NOT NULL,
                telegram_file_id TEXT,
                title TEXT,
                artist TEXT,
                duration_secs INTEGER,
                file_size_bytes INTEGER,
                source TEXT DEFAULT 'api',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (track_id, quality)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_storage_track ON channel_storage(track_id);
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                expires_at DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at);
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                track_id TEXT NOT NULL,
                title TEXT NOT NULL,
                quality TEXT NOT NULL,
                channel_msg_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_download_history_user ON download_history(user_chat_id);
            CREATE TABLE IF NOT EXISTS rate_limits (
                user_chat_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 1,
                window_start DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_chat_id, action)
            );
        """)
        await self.sqlite_conn.commit()

    # ── Channel Storage Operations ──────────────────────────────────────────

    async def get_cached_track(self, track_id: str, quality: str) -> Optional[Dict[str, Any]]:
        """Lookup stored channel message by track_id and quality."""
        if not self.is_connected:
            return None

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            async with self.sqlite_conn.execute(
                """
                SELECT id, track_id, quality, channel_msg_id, telegram_file_id,
                       title, artist, duration_secs, file_size_bytes, source, created_at
                FROM channel_storage
                WHERE track_id = ? AND quality = ?
                """,
                (track_id, quality)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

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
        if not self.is_connected:
            return

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            await self.sqlite_conn.execute(
                """
                INSERT INTO channel_storage (
                    track_id, quality, channel_msg_id, telegram_file_id,
                    title, artist, duration_secs, file_size_bytes, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (track_id, quality) DO UPDATE SET
                    channel_msg_id = excluded.channel_msg_id,
                    telegram_file_id = COALESCE(excluded.telegram_file_id, channel_storage.telegram_file_id),
                    title = COALESCE(excluded.title, channel_storage.title),
                    artist = COALESCE(excluded.artist, channel_storage.artist),
                    duration_secs = COALESCE(excluded.duration_secs, channel_storage.duration_secs),
                    file_size_bytes = COALESCE(excluded.file_size_bytes, channel_storage.file_size_bytes),
                    source = excluded.source;
                """,
                (track_id, quality, channel_msg_id, telegram_file_id,
                 title, artist, duration_secs, file_size_bytes, source)
            )
            await self.sqlite_conn.commit()

    # ── Persistent API Cache Operations ────────────────────────────────────

    async def get_api_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve unexpired cached API response."""
        if not self.is_connected:
            return None

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT response_json FROM api_cache WHERE cache_key = $1 AND expires_at > NOW()",
                    cache_key
                )
                if row:
                    data = row["response_json"]
                    return json.loads(data) if isinstance(data, str) else data
                return None
        else:
            now_iso = datetime.utcnow().isoformat()
            async with self.sqlite_conn.execute(
                "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, now_iso)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    data = row["response_json"]
                    return json.loads(data) if isinstance(data, str) else data
                return None

    async def set_api_cache(self, cache_key: str, data: Dict[str, Any], ttl_seconds: int) -> None:
        """Store API response in database cache with TTL."""
        if not self.is_connected:
            return

        expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
        data_json = json.dumps(data)

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            await self.sqlite_conn.execute(
                """
                INSERT INTO api_cache (cache_key, response_json, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    expires_at = excluded.expires_at;
                """,
                (cache_key, data_json, expires_at.isoformat())
            )
            await self.sqlite_conn.commit()

    # ── User & History Operations ──────────────────────────────────────────

    async def upsert_user(self, chat_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        """Record or update user activity."""
        if not self.is_connected:
            return

        is_admin = (chat_id == settings.ADMIN_CHAT_ID)
        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            now_iso = datetime.utcnow().isoformat()
            await self.sqlite_conn.execute(
                """
                INSERT INTO users (chat_id, username, first_name, is_admin, last_active)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_active = excluded.last_active;
                """,
                (chat_id, username, first_name, 1 if is_admin else 0, now_iso)
            )
            await self.sqlite_conn.commit()

    async def add_history(self, user_chat_id: int, track_id: str, title: str, quality: str, channel_msg_id: int) -> None:
        """Log download to user history."""
        if not self.is_connected:
            return

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO download_history (user_chat_id, track_id, title, quality, channel_msg_id)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_chat_id, track_id, title, quality, channel_msg_id
                )
        else:
            await self.sqlite_conn.execute(
                """
                INSERT INTO download_history (user_chat_id, track_id, title, quality, channel_msg_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_chat_id, track_id, title, quality, channel_msg_id)
            )
            await self.sqlite_conn.commit()

    async def get_user_history(self, user_chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve recent downloads for a user."""
        if not self.is_connected:
            return []

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            async with self.sqlite_conn.execute(
                """
                SELECT track_id, title, quality, channel_msg_id, created_at
                FROM download_history
                WHERE user_chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_chat_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # ── Rate Limiting ──────────────────────────────────────────────────────

    async def check_rate_limit(self, user_chat_id: int, action: str, limit: int = 10, window_secs: int = 60) -> bool:
        """Return True if under rate limit, False if rate limited."""
        if user_chat_id == settings.ADMIN_CHAT_ID:
            return True

        if not self.is_connected:
            return True

        now = datetime.utcnow()
        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
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
        else:
            async with self.sqlite_conn.execute(
                "SELECT attempt_count, window_start FROM rate_limits WHERE user_chat_id = ? AND action = ?",
                (user_chat_id, action)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await self.sqlite_conn.execute(
                        "INSERT INTO rate_limits (user_chat_id, action, attempt_count, window_start) VALUES (?, ?, 1, ?)",
                        (user_chat_id, action, now.isoformat())
                    )
                    await self.sqlite_conn.commit()
                    return True

                count = row["attempt_count"]
                start_str = row["window_start"]
                try:
                    start = datetime.fromisoformat(start_str)
                except Exception:
                    start = now

                if (now - start).total_seconds() > window_secs:
                    await self.sqlite_conn.execute(
                        "UPDATE rate_limits SET attempt_count = 1, window_start = ? WHERE user_chat_id = ? AND action = ?",
                        (now.isoformat(), user_chat_id, action)
                    )
                    await self.sqlite_conn.commit()
                    return True

                if count >= limit:
                    return False

                await self.sqlite_conn.execute(
                    "UPDATE rate_limits SET attempt_count = attempt_count + 1 WHERE user_chat_id = ? AND action = ?",
                    (user_chat_id, action)
                )
                await self.sqlite_conn.commit()
                return True

    async def get_stats(self) -> Dict[str, Any]:
        """Get aggregate system statistics for admin."""
        if not self.is_connected:
            return {"status": "disconnected"}

        if self.is_postgres:
            async with self.pg_pool.acquire() as conn:
                user_count = await conn.fetchval("SELECT COUNT(*) FROM users;")
                cached_tracks = await conn.fetchval("SELECT COUNT(*) FROM channel_storage;")
                total_downloads = await conn.fetchval("SELECT COUNT(*) FROM download_history;")
                return {
                    "backend": "PostgreSQL (Neon)",
                    "total_users": user_count or 0,
                    "cached_tracks": cached_tracks or 0,
                    "total_downloads": total_downloads or 0
                }
        else:
            async with self.sqlite_conn.execute("SELECT COUNT(*) FROM users;") as cur:
                user_count = (await cur.fetchone())[0]
            async with self.sqlite_conn.execute("SELECT COUNT(*) FROM channel_storage;") as cur:
                cached_tracks = (await cur.fetchone())[0]
            async with self.sqlite_conn.execute("SELECT COUNT(*) FROM download_history;") as cur:
                total_downloads = (await cur.fetchone())[0]
            return {
                "backend": "SQLite",
                "total_users": user_count or 0,
                "cached_tracks": cached_tracks or 0,
                "total_downloads": total_downloads or 0
            }


db = Database()
