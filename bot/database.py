"""Database Manager with Multi-Backend Support: MongoDB Atlas, Neon PostgreSQL & SQLite Fallback.

Features:
- MongoDB Atlas / Cluster (Primary / Migrated):
  * motor asynchronous driver with connection pooling.
  * Multi-bot database namespace isolation:
    - Custom database name from URI path (e.g. mongodb+srv://.../dbname?...)
    - Or MONGO_DB_NAME env setting
    - Or auto-derived from bot token ID (e.g. bot_8908464066 or bot_8126033137)
  * Automatic index creation for fast lookups & unique constraints.
  * Direct support for channel_storage, users, download_history, api_cache, rate_limits.
- Neon Lakebase PostgreSQL:
  * asyncpg connection pooling with statement_cache_size=0 for PgBouncer compatibility.
  * Cold-start scale-to-zero exponential backoff connection retry loop.
  * Automatic query-level reconnect and retry on transient connection drops.
  * Optional background keep-alive ping loop.
- SQLite (Fallback / Local):
  * aiosqlite asynchronous connection with WAL mode enabled.
  * Seamless fallback when DATABASE_URL is 'sqlite:///...' or unconfigured.
"""

import asyncio
import json
import logging
import os
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import aiosqlite
import asyncpg

try:
    import motor.motor_asyncio
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    HAS_MOTOR = True
except ImportError:
    HAS_MOTOR = False
    motor = None

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
        self.mongo_client: Optional[Any] = None
        self.mongo_db: Optional[Any] = None
        self._active_db_name: Optional[str] = None
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.sqlite_conn: Optional[aiosqlite.Connection] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_mongo(self) -> bool:
        """Return True if DATABASE_URL or MONGO_URL specifies MongoDB."""
        db_url = settings.DATABASE_URL or getattr(settings, "MONGO_URL", "") or ""
        return db_url.startswith("mongodb://") or db_url.startswith("mongodb+srv://")

    @property
    def is_postgres(self) -> bool:
        """Return True if DATABASE_URL specifies PostgreSQL / Neon."""
        db_url = settings.DATABASE_URL or ""
        return db_url.startswith("postgresql://") or db_url.startswith("postgres://")

    @property
    def is_sqlite(self) -> bool:
        """Return True if database is local SQLite."""
        return not self.is_mongo and not self.is_postgres

    @property
    def is_connected(self) -> bool:
        return self.mongo_db is not None or self.pg_pool is not None or self.sqlite_conn is not None

    @property
    def pool(self) -> Optional[asyncpg.Pool]:
        """Backward-compatible alias for pg_pool."""
        return self.pg_pool

    @property
    def active_database_name(self) -> Optional[str]:
        """Return the active database name (e.g. for MongoDB)."""
        if self.is_mongo and self.mongo_db is not None:
            return self.mongo_db.name
        return self._active_db_name

    @staticmethod
    def resolve_mongo_db_name(uri: str = "", configured_db_name: Optional[str] = None, bot_token: Optional[str] = None) -> str:
        """Resolve MongoDB database name.
        
        Resolution priority:
        1. Database name in URI path (e.g. mongodb+srv://.../dbname?retryWrites=true)
        2. Configured MONGO_DB_NAME env / setting
        3. Bot token ID (e.g. bot_8908464066 or bot_8126033137)
        4. Fallback default: 'telegram_ultra'
        """
        # 1. URI path check
        if uri:
            try:
                parsed = urllib.parse.urlparse(uri)
                path_part = parsed.path.lstrip("/").strip()
                if path_part and not path_part.startswith("?"):
                    name = path_part.split("/")[0].split("?")[0].strip()
                    if name:
                        return name
            except Exception as e:
                logger.warning(f"Failed parsing MongoDB URI path: {e}")

        # 2. Configured MONGO_DB_NAME
        if configured_db_name and str(configured_db_name).strip():
            return str(configured_db_name).strip()

        # 3. Derive from bot token (ID before the colon)
        if bot_token and ":" in bot_token:
            bot_id = bot_token.split(":")[0].strip()
            if bot_id:
                return f"bot_{bot_id}"

        return "telegram_ultra"

    async def connect(self) -> None:
        """Connect to MongoDB, Neon PostgreSQL, or SQLite based on configuration."""
        async with self._lock:
            if self.is_connected:
                return

            db_url = settings.DATABASE_URL or getattr(settings, "MONGO_URL", "") or ""

            # ── 1. Connect to MongoDB ──
            if self.is_mongo:
                if not HAS_MOTOR:
                    raise ImportError("motor package is required for MongoDB support. Install with: pip install motor dnspython")

                bot_tok = settings.TELEGRAM_BOT_TOKEN or getattr(settings, "TELOXIDE_TOKEN", None)
                db_name = self.resolve_mongo_db_name(
                    uri=db_url,
                    configured_db_name=getattr(settings, "MONGO_DB_NAME", None),
                    bot_token=bot_tok
                )
                self._active_db_name = db_name

                logger.info(f"Connecting to MongoDB cluster (target database: '{db_name}')...")
                max_retries = 3
                for attempt in range(1, max_retries + 1):
                    try:
                        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                            db_url,
                            serverSelectionTimeoutMS=10000,
                            connectTimeoutMS=10000,
                            socketTimeoutMS=30000,
                            maxPoolSize=getattr(settings, "DB_POOL_MAX_SIZE", 10),
                            minPoolSize=getattr(settings, "DB_POOL_MIN_SIZE", 2)
                        )
                        # Verify connectivity
                        await self.mongo_client.admin.command("ping")
                        self.mongo_db = self.mongo_client[db_name]
                        logger.info(f"MongoDB connected successfully to database: '{db_name}'.")
                        await self._migrate()
                        return
                    except Exception as e:
                        logger.warning(f"MongoDB connection attempt {attempt} failed: {e}")
                        if attempt < max_retries:
                            await asyncio.sleep(2.0)
                        else:
                            logger.error(f"Failed to connect to MongoDB after {max_retries} attempts.")
                            raise

            # ── 2. Connect to Neon PostgreSQL ──
            elif self.is_postgres:
                max_retries = 5
                base_delay = 1.5

                for attempt in range(1, max_retries + 1):
                    try:
                        logger.info(f"Connecting to Neon PostgreSQL (attempt {attempt}/{max_retries})...")
                        self.pg_pool = await asyncpg.create_pool(
                            dsn=db_url,
                            min_size=settings.DB_POOL_MIN_SIZE,
                            max_size=settings.DB_POOL_MAX_SIZE,
                            timeout=30.0,
                            command_timeout=30.0,
                            statement_cache_size=0,
                            max_inactive_connection_lifetime=300.0
                        )

                        # Verify compute is awake
                        async with self.pg_pool.acquire() as conn:
                            await conn.fetchval("SELECT 1;")

                        logger.info("Neon PostgreSQL connected and active.")
                        await self._migrate()

                        # Start background keepalive if configured
                        if getattr(settings, "ENABLE_NEON_KEEPALIVE", False) and not self._keepalive_task:
                            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
                        return

                    except NEON_TRANSIENT_ERRORS as e:
                        logger.warning(f"Neon cold-start delay on attempt {attempt}: {e}")
                        if attempt < max_retries:
                            sleep_time = base_delay * (2 ** (attempt - 1))
                            logger.info(f"Waiting {sleep_time:.1f}s for Neon compute to spin up...")
                            await asyncio.sleep(sleep_time)
                        else:
                            logger.error("Failed to connect to Neon PostgreSQL after retries.")
                            raise

            # ── 3. Connect to SQLite (Fallback) ──
            else:
                db_path = settings.DATABASE_PATH
                if db_url.startswith("sqlite:///"):
                    db_path = db_url.replace("sqlite:///", "")
                elif db_url.startswith("sqlite://"):
                    db_path = db_url.replace("sqlite://", "")

                if not db_path:
                    db_path = "./hermes.db"

                logger.info(f"Connecting to SQLite database at {db_path}...")
                dir_name = os.path.dirname(os.path.abspath(db_path))
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)

                self.sqlite_conn = await aiosqlite.connect(db_path)
                self.sqlite_conn.row_factory = aiosqlite.Row
                await self.sqlite_conn.execute("PRAGMA journal_mode=WAL;")
                await self.sqlite_conn.execute("PRAGMA synchronous=NORMAL;")
                logger.info("SQLite database connected in WAL mode.")
                await self._migrate()

    async def disconnect(self) -> None:
        """Gracefully release database resources."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        if self.mongo_client:
            self.mongo_client.close()
            self.mongo_client = None
            self.mongo_db = None
            logger.info("MongoDB connection closed.")

        if self.pg_pool:
            await self.pg_pool.close()
            self.pg_pool = None
            logger.info("Neon PostgreSQL pool closed.")

        if self.sqlite_conn:
            await self.sqlite_conn.close()
            self.sqlite_conn = None
            logger.info("SQLite connection closed.")

    async def _keepalive_loop(self) -> None:
        """Lightweight background heartbeat every 4m to prevent Neon cold start during active use."""
        try:
            while True:
                await asyncio.sleep(240)
                if self.pg_pool:
                    try:
                        async with self.pg_pool.acquire() as conn:
                            await conn.fetchval("SELECT 1;")
                        logger.debug("Neon keepalive ping successful.")
                    except Exception as e:
                        logger.debug(f"Neon keepalive ping skipped: {e}")
        except asyncio.CancelledError:
            pass

    async def _execute_pg_with_retry(self, callback):
        """Execute a PostgreSQL query with auto-retry on transient cold-start disconnect."""
        retries = 2
        for attempt in range(1, retries + 1):
            if not self.pg_pool:
                await self.connect()
            try:
                async with self.pg_pool.acquire() as conn:
                    return await callback(conn)
            except NEON_TRANSIENT_ERRORS as e:
                logger.warning(f"Transient DB error during query (attempt {attempt}): {e}")
                if attempt < retries:
                    if self.pg_pool:
                        try:
                            await self.pg_pool.close()
                        except Exception:
                            pass
                        self.pg_pool = None
                    await asyncio.sleep(1.0)
                    await self.connect()
                else:
                    raise

    async def _migrate(self) -> None:
        """Run schema migrations / ensure indexes for active backend."""
        # ── MongoDB Indexes ──
        if self.is_mongo:
            logger.info(f"Ensuring MongoDB indexes on database '{self.mongo_db.name}'...")
            await self.mongo_db.users.create_index("chat_id", unique=True)
            await self.mongo_db.channel_storage.create_index(
                [("track_id", 1), ("quality", 1)],
                unique=True
            )
            await self.mongo_db.channel_storage.create_index("track_id")
            await self.mongo_db.api_cache.create_index("cache_key", unique=True)
            await self.mongo_db.api_cache.create_index("expires_at")
            await self.mongo_db.download_history.create_index("user_chat_id")
            await self.mongo_db.download_history.create_index([("user_chat_id", 1), ("created_at", -1)])
            await self.mongo_db.rate_limits.create_index(
                [("user_chat_id", 1), ("action", 1)],
                unique=True
            )
            logger.info("MongoDB indexes verified successfully.")

        # ── PostgreSQL Migrations ──
        elif self.is_postgres:
            async def _run(conn):
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        chat_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        is_admin BOOLEAN DEFAULT FALSE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        last_active TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS channel_storage (
                        id BIGSERIAL PRIMARY KEY,
                        track_id TEXT NOT NULL,
                        quality TEXT NOT NULL,
                        channel_msg_id BIGINT NOT NULL,
                        telegram_file_id TEXT,
                        title TEXT,
                        artist TEXT,
                        duration_secs INTEGER DEFAULT 0,
                        file_size_bytes BIGINT DEFAULT 0,
                        source TEXT DEFAULT 'youtube',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        CONSTRAINT unique_track_quality UNIQUE(track_id, quality)
                    );

                    CREATE TABLE IF NOT EXISTS api_cache (
                        cache_key TEXT PRIMARY KEY,
                        response_json JSONB,
                        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                    ALTER TABLE api_cache ADD COLUMN IF NOT EXISTS response_json JSONB;

                    CREATE TABLE IF NOT EXISTS download_history (
                        id BIGSERIAL PRIMARY KEY,
                        user_chat_id BIGINT NOT NULL,
                        track_id TEXT NOT NULL,
                        title TEXT,
                        quality TEXT,
                        channel_msg_id BIGINT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );

                    CREATE TABLE IF NOT EXISTS rate_limits (
                        user_chat_id BIGINT NOT NULL,
                        action TEXT NOT NULL,
                        attempt_count INTEGER DEFAULT 1,
                        window_start TIMESTAMP WITH TIME ZONE NOT NULL,
                        PRIMARY KEY (user_chat_id, action)
                    );

                    CREATE INDEX IF NOT EXISTS idx_channel_storage_track ON channel_storage(track_id);
                    CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_history_user ON download_history(user_chat_id);
                """)
            await self._execute_pg_with_retry(_run)
            logger.info("Neon PostgreSQL schema migrations applied successfully.")

        # ── SQLite Migrations ──
        else:
            await self.sqlite_conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_admin INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now')),
                    last_active TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS channel_storage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    channel_msg_id INTEGER NOT NULL,
                    telegram_file_id TEXT,
                    title TEXT,
                    artist TEXT,
                    duration_secs INTEGER DEFAULT 0,
                    file_size_bytes INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'youtube',
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(track_id, quality)
                );

                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS download_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_chat_id INTEGER NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT,
                    quality TEXT,
                    channel_msg_id INTEGER,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS rate_limits (
                    user_chat_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    attempt_count INTEGER DEFAULT 1,
                    window_start TEXT NOT NULL,
                    PRIMARY KEY (user_chat_id, action)
                );

                CREATE INDEX IF NOT EXISTS idx_channel_storage_track ON channel_storage(track_id);
                CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at);
                CREATE INDEX IF NOT EXISTS idx_history_user ON download_history(user_chat_id);
            """)
            await self.sqlite_conn.commit()
            logger.info("SQLite schema migrations applied successfully.")

    # ── Channel Storage (Cached Tracks) ───────────────────────────────────

    async def get_cached_track(self, track_id: str, quality: str = "audio") -> Optional[Dict[str, Any]]:
        """Retrieve stored message ID for instant Telegram delivery."""
        if not self.is_connected:
            return None

        if self.is_mongo:
            doc = await self.mongo_db.channel_storage.find_one(
                {"track_id": track_id, "quality": quality},
                {
                    "_id": 0,
                    "channel_msg_id": 1,
                    "telegram_file_id": 1,
                    "title": 1,
                    "artist": 1,
                    "duration_secs": 1,
                    "file_size_bytes": 1
                }
            )
            return dict(doc) if doc else None
        elif self.is_postgres:
            async def _run(conn):
                row = await conn.fetchrow(
                    """
                    SELECT channel_msg_id, telegram_file_id, title, artist, duration_secs, file_size_bytes
                    FROM channel_storage
                    WHERE track_id = $1 AND quality = $2
                    LIMIT 1;
                    """,
                    track_id, quality
                )
                return dict(row) if row else None
            return await self._execute_pg_with_retry(_run)
        else:
            async with self.sqlite_conn.execute(
                """
                SELECT channel_msg_id, telegram_file_id, title, artist, duration_secs, file_size_bytes
                FROM channel_storage
                WHERE track_id = ? AND quality = ?
                LIMIT 1;
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
        duration_secs: int = 0,
        file_size_bytes: int = 0,
        source: str = "youtube"
    ) -> None:
        """Index a newly forwarded audio track in channel storage."""
        if not self.is_connected:
            return

        if self.is_mongo:
            now = datetime.utcnow()
            await self.mongo_db.channel_storage.update_one(
                {"track_id": track_id, "quality": quality},
                {
                    "$set": {
                        "channel_msg_id": channel_msg_id,
                        "telegram_file_id": telegram_file_id,
                        "title": title,
                        "artist": artist,
                        "duration_secs": duration_secs,
                        "file_size_bytes": file_size_bytes,
                        "source": source
                    },
                    "$setOnInsert": {
                        "track_id": track_id,
                        "quality": quality,
                        "created_at": now
                    }
                },
                upsert=True
            )
        elif self.is_postgres:
            async def _run(conn):
                await conn.execute(
                    """
                    INSERT INTO channel_storage (
                        track_id, quality, channel_msg_id, telegram_file_id,
                        title, artist, duration_secs, file_size_bytes, source
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (track_id, quality) DO UPDATE SET
                        channel_msg_id = EXCLUDED.channel_msg_id,
                        telegram_file_id = EXCLUDED.telegram_file_id,
                        duration_secs = EXCLUDED.duration_secs,
                        file_size_bytes = EXCLUDED.file_size_bytes;
                    """,
                    track_id, quality, channel_msg_id, telegram_file_id,
                    title, artist, duration_secs, file_size_bytes, source
                )
            await self._execute_pg_with_retry(_run)
        else:
            await self.sqlite_conn.execute(
                """
                INSERT INTO channel_storage (
                    track_id, quality, channel_msg_id, telegram_file_id,
                    title, artist, duration_secs, file_size_bytes, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (track_id, quality) DO UPDATE SET
                    channel_msg_id = excluded.channel_msg_id,
                    telegram_file_id = excluded.telegram_file_id,
                    duration_secs = excluded.duration_secs,
                    file_size_bytes = excluded.file_size_bytes;
                """,
                (track_id, quality, channel_msg_id, telegram_file_id,
                 title, artist, duration_secs, file_size_bytes, source)
            )
            await self.sqlite_conn.commit()

    async def delete_cached_track(self, track_id: str, quality: Optional[str] = None) -> List[int]:
        """Delete cached track records from channel_storage, returning list of channel_msg_ids."""
        if not self.is_connected:
            return []

        clean_id = track_id.lstrip("#").strip()

        if self.is_mongo:
            query = {"track_id": clean_id}
            if quality:
                query["quality"] = quality
            cursor = self.mongo_db.channel_storage.find(query, {"channel_msg_id": 1})
            msg_ids = []
            async for doc in cursor:
                if doc.get("channel_msg_id"):
                    msg_ids.append(int(doc["channel_msg_id"]))
            if msg_ids:
                await self.mongo_db.channel_storage.delete_many(query)
            return msg_ids
        elif self.is_postgres:
            async def _run(conn):
                if quality:
                    rows = await conn.fetch(
                        "SELECT channel_msg_id FROM channel_storage WHERE track_id = $1 AND quality = $2;",
                        clean_id, quality
                    )
                    await conn.execute(
                        "DELETE FROM channel_storage WHERE track_id = $1 AND quality = $2;",
                        clean_id, quality
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT channel_msg_id FROM channel_storage WHERE track_id = $1;",
                        clean_id
                    )
                    await conn.execute(
                        "DELETE FROM channel_storage WHERE track_id = $1;",
                        clean_id
                    )
                return [r["channel_msg_id"] for r in rows if r.get("channel_msg_id")]
            return await self._execute_pg_with_retry(_run)
        else:
            if quality:
                async with self.sqlite_conn.execute(
                    "SELECT channel_msg_id FROM channel_storage WHERE track_id = ? AND quality = ?;",
                    (clean_id, quality)
                ) as cursor:
                    rows = await cursor.fetchall()
                await self.sqlite_conn.execute(
                    "DELETE FROM channel_storage WHERE track_id = ? AND quality = ?;",
                    (clean_id, quality)
                )
            else:
                async with self.sqlite_conn.execute(
                    "SELECT channel_msg_id FROM channel_storage WHERE track_id = ?;",
                    (clean_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
                await self.sqlite_conn.execute(
                    "DELETE FROM channel_storage WHERE track_id = ?;",
                    (clean_id,)
                )
            await self.sqlite_conn.commit()
            return [r[0] for r in rows if r[0]]

    # ── API Response Caching ──────────────────────────────────────────────

    async def get_api_cache(self, cache_key: str) -> Optional[Any]:
        """Fetch unexpired cached API response."""
        if not self.is_connected:
            return None

        if self.is_mongo:
            now = datetime.utcnow()
            doc = await self.mongo_db.api_cache.find_one(
                {"cache_key": cache_key, "expires_at": {"$gt": now}}
            )
            if not doc:
                return None
            val = doc.get("response_json")
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except Exception:
                    return val
            return val
        elif self.is_postgres:
            async def _run(conn):
                row = await conn.fetchrow(
                    "SELECT response_json FROM api_cache WHERE cache_key = $1 AND expires_at > NOW();",
                    cache_key
                )
                if not row:
                    return None
                val = row["response_json"]
                return json.loads(val) if isinstance(val, str) else val
            return await self._execute_pg_with_retry(_run)
        else:
            now_iso = datetime.utcnow().isoformat()
            async with self.sqlite_conn.execute(
                "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?;",
                (cache_key, now_iso)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                try:
                    return json.loads(row["response_json"])
                except Exception:
                    return None

    async def set_api_cache(self, cache_key: str, data: Any, ttl_seconds: int = 86400) -> None:
        """Store API response with TTL."""
        if not self.is_connected:
            return

        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)

        if self.is_mongo:
            await self.mongo_db.api_cache.update_one(
                {"cache_key": cache_key},
                {
                    "$set": {
                        "cache_key": cache_key,
                        "response_json": data,
                        "expires_at": expires_at,
                        "updated_at": now
                    },
                    "$setOnInsert": {
                        "created_at": now
                    }
                },
                upsert=True
            )
        elif self.is_postgres:
            data_json = json.dumps(data) if not isinstance(data, str) else data
            async def _run(conn):
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
            await self._execute_pg_with_retry(_run)
        else:
            data_str = json.dumps(data)
            await self.sqlite_conn.execute(
                """
                INSERT INTO api_cache (cache_key, response_json, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    expires_at = excluded.expires_at;
                """,
                (cache_key, data_str, expires_at.isoformat())
            )
            await self.sqlite_conn.commit()

    # ── User Management ───────────────────────────────────────────────────

    async def upsert_user(self, chat_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        """Register or update a user upon interaction."""
        if not self.is_connected:
            return

        is_admin = (chat_id == settings.ADMIN_CHAT_ID)

        if self.is_mongo:
            now = datetime.utcnow()
            set_fields = {
                "chat_id": chat_id,
                "username": username,
                "first_name": first_name,
                "last_active": now
            }
            set_on_insert: Dict[str, Any] = {"created_at": now}

            if is_admin:
                set_fields["is_admin"] = True
            else:
                set_on_insert["is_admin"] = False

            await self.mongo_db.users.update_one(
                {"chat_id": chat_id},
                {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert
                },
                upsert=True
            )
        elif self.is_postgres:
            async def _run(conn):
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
            await self._execute_pg_with_retry(_run)
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

    # ── Download History ──────────────────────────────────────────────────

    async def add_history(self, user_chat_id: int, track_id: str, title: str, quality: str, channel_msg_id: int) -> None:
        """Log download to user history."""
        if not self.is_connected:
            return

        if self.is_mongo:
            await self.mongo_db.download_history.insert_one({
                "user_chat_id": user_chat_id,
                "track_id": track_id,
                "title": title,
                "quality": quality,
                "channel_msg_id": channel_msg_id,
                "created_at": datetime.utcnow()
            })
        elif self.is_postgres:
            async def _run(conn):
                await conn.execute(
                    """
                    INSERT INTO download_history (user_chat_id, track_id, title, quality, channel_msg_id)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    user_chat_id, track_id, title, quality, channel_msg_id
                )
            await self._execute_pg_with_retry(_run)
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

        if self.is_mongo:
            cursor = self.mongo_db.download_history.find(
                {"user_chat_id": user_chat_id},
                {"_id": 0, "track_id": 1, "title": 1, "quality": 1, "channel_msg_id": 1, "created_at": 1}
            ).sort("created_at", -1).limit(limit)
            rows = []
            async for doc in cursor:
                rows.append(doc)
            return rows
        elif self.is_postgres:
            async def _run(conn):
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
            return await self._execute_pg_with_retry(_run)
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

        if self.is_mongo:
            doc = await self.mongo_db.rate_limits.find_one(
                {"user_chat_id": user_chat_id, "action": action}
            )
            if not doc:
                await self.mongo_db.rate_limits.insert_one({
                    "user_chat_id": user_chat_id,
                    "action": action,
                    "attempt_count": 1,
                    "window_start": now
                })
                return True

            count = doc.get("attempt_count", 1)
            start = doc.get("window_start")
            if isinstance(start, str):
                try:
                    start = datetime.fromisoformat(start)
                except Exception:
                    start = now
            elif not isinstance(start, datetime):
                start = now

            if (now - start).total_seconds() > window_secs:
                await self.mongo_db.rate_limits.update_one(
                    {"user_chat_id": user_chat_id, "action": action},
                    {"$set": {"attempt_count": 1, "window_start": now}}
                )
                return True

            if count >= limit:
                return False

            await self.mongo_db.rate_limits.update_one(
                {"user_chat_id": user_chat_id, "action": action},
                {"$inc": {"attempt_count": 1}}
            )
            return True

        elif self.is_postgres:
            async def _run(conn):
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
            return await self._execute_pg_with_retry(_run)
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

    # ── Statistics & Diagnostics ──────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        """Get aggregate system statistics."""
        if not self.is_connected:
            return {"status": "disconnected"}

        if self.is_mongo:
            user_count = await self.mongo_db.users.count_documents({})
            cached_tracks = await self.mongo_db.channel_storage.count_documents({})
            total_downloads = await self.mongo_db.download_history.count_documents({})
            return {
                "backend": f"MongoDB Atlas ({self.mongo_db.name})",
                "database_name": self.mongo_db.name,
                "total_users": user_count or 0,
                "cached_tracks": cached_tracks or 0,
                "total_downloads": total_downloads or 0
            }
        elif self.is_postgres:
            async def _run(conn):
                user_count = await conn.fetchval("SELECT COUNT(*) FROM users;")
                cached_tracks = await conn.fetchval("SELECT COUNT(*) FROM channel_storage;")
                total_downloads = await conn.fetchval("SELECT COUNT(*) FROM download_history;")
                return {
                    "backend": "PostgreSQL (Neon Lakebase)",
                    "total_users": user_count or 0,
                    "cached_tracks": cached_tracks or 0,
                    "total_downloads": total_downloads or 0
                }
            return await self._execute_pg_with_retry(_run)
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

    async def cleanup_expired_cache(self) -> int:
        """Purge expired rows from api_cache and return count of deleted rows."""
        if not self.is_connected:
            return 0

        if self.is_mongo:
            now = datetime.utcnow()
            res = await self.mongo_db.api_cache.delete_many({"expires_at": {"$lt": now}})
            return res.deleted_count if res else 0
        elif self.is_postgres:
            async def _run(conn):
                res = await conn.execute("DELETE FROM api_cache WHERE expires_at < NOW();")
                try:
                    return int(res.split()[1]) if res and len(res.split()) > 1 else 0
                except (ValueError, IndexError):
                    return 0
            return await self._execute_pg_with_retry(_run)
        else:
            now_iso = datetime.utcnow().isoformat()
            cursor = await self.sqlite_conn.execute("DELETE FROM api_cache WHERE expires_at < ?;", (now_iso,))
            count = cursor.rowcount
            await self.sqlite_conn.commit()
            return count if count and count > 0 else 0

    async def is_admin(self, user_id: int) -> bool:
        """Check if user_id is admin (via ADMIN_CHAT_ID or is_admin flag in users table/collection)."""
        if user_id and user_id == settings.ADMIN_CHAT_ID:
            return True

        if not self.is_connected:
            return False

        try:
            if self.is_mongo:
                doc = await self.mongo_db.users.find_one({"chat_id": user_id}, {"is_admin": 1})
                return bool(doc.get("is_admin")) if doc else False
            elif self.is_postgres:
                async def _run(conn):
                    val = await conn.fetchval("SELECT is_admin FROM users WHERE chat_id = $1;", user_id)
                    return bool(val)
                return await self._execute_pg_with_retry(_run)
            else:
                async with self.sqlite_conn.execute("SELECT is_admin FROM users WHERE chat_id = ?;", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    return bool(row[0]) if row else False
        except Exception as e:
            logger.warning(f"Error checking admin status for {user_id}: {e}")
            return False


db = Database()
