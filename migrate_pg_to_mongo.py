#!/usr/bin/env python3
"""Database Migration Script: Neon PostgreSQL -> MongoDB Atlas.

Migrates all existing tables and records:
- users
- channel_storage (cached audio tracks & telegram message IDs)
- download_history
- api_cache
- rate_limits

Supports:
- Multi-database naming isolation per bot token ID or custom schema/dbname.
- Idempotent upserts (safe to run multiple times).
- CLI arguments or automatic .env detection.
- Detailed migration summary and row count verification.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional, Dict, Any, List

import asyncpg
import motor.motor_asyncio
from pymongo import UpdateOne, ReplaceOne

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import settings
from bot.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("pg_to_mongo_migration")

DEFAULT_PG_URL = (
    "postgresql://neondb_owner:npg_Ots3aN9RMFvb@ep-autumn-silence-b4czkrxz-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)
DEFAULT_MONGO_URL = (
    "mongodb+srv://rtwoft_db_user:0sudhUOXWychqplX@clasto.nlhmbxk.mongodb.net/?retryWrites=true&w=majority"
)


async def migrate(
    pg_url: str,
    mongo_url: str,
    db_name: Optional[str] = None,
    drop_target: bool = False,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Execute migration from PostgreSQL to MongoDB."""
    # Resolve target MongoDB database name
    bot_token = settings.TELEGRAM_BOT_TOKEN or getattr(settings, "TELOXIDE_TOKEN", None)
    target_db_name = Database.resolve_mongo_db_name(
        uri=mongo_url,
        configured_db_name=db_name or getattr(settings, "MONGO_DB_NAME", None),
        bot_token=bot_token
    )

    logger.info("=" * 65)
    logger.info("MIGRATION: Neon PostgreSQL -> MongoDB Atlas")
    logger.info(f"Target Database: '{target_db_name}'")
    logger.info(f"Dry Run Mode:    {dry_run}")
    logger.info(f"Drop Target:     {drop_target}")
    logger.info("=" * 65)

    # 1. Connect to PostgreSQL
    logger.info("Connecting to Neon PostgreSQL...")
    pg_conn = await asyncpg.connect(pg_url, statement_cache_size=0, timeout=30.0)
    logger.info("Connected to Neon PostgreSQL.")

    # 2. Connect to MongoDB
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
        mongo_url,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000
    )
    await mongo_client.admin.command("ping")
    mongo_db = mongo_client[target_db_name]
    logger.info(f"Connected to MongoDB cluster. Database: '{target_db_name}'")

    results = {}

    try:
        if drop_target and not dry_run:
            logger.warning(f"Dropping target collections in '{target_db_name}'...")
            await mongo_db.users.drop()
            await mongo_db.channel_storage.drop()
            await mongo_db.download_history.drop()
            await mongo_db.api_cache.drop()
            await mongo_db.rate_limits.drop()

        # ── Table 1: USERS ───────────────────────────────────────────────
        logger.info("\n--- Migrating 'users' ---")
        user_rows = await pg_conn.fetch("""
            SELECT chat_id, username, first_name, is_admin, created_at, last_active
            FROM users;
        """)
        results["users_pg"] = len(user_rows)
        logger.info(f"Found {len(user_rows)} users in PostgreSQL.")

        if not dry_run and user_rows:
            ops = []
            for r in user_rows:
                doc = {
                    "chat_id": int(r["chat_id"]),
                    "username": r["username"],
                    "first_name": r["first_name"],
                    "is_admin": bool(r["is_admin"]),
                    "created_at": r["created_at"],
                    "last_active": r["last_active"]
                }
                ops.append(
                    UpdateOne(
                        {"chat_id": doc["chat_id"]},
                        {"$set": doc},
                        upsert=True
                    )
                )
            if ops:
                res = await mongo_db.users.bulk_write(ops)
                logger.info(f"MongoDB users: upserted={len(res.upserted_ids)}, modified={res.modified_count}, matched={res.matched_count}")

        # ── Table 2: CHANNEL_STORAGE ────────────────────────────────────
        logger.info("\n--- Migrating 'channel_storage' ---")
        cs_rows = await pg_conn.fetch("""
            SELECT track_id, quality, channel_msg_id, telegram_file_id,
                   title, artist, duration_secs, file_size_bytes, source, created_at
            FROM channel_storage;
        """)
        results["channel_storage_pg"] = len(cs_rows)
        logger.info(f"Found {len(cs_rows)} cached tracks in PostgreSQL.")

        if not dry_run and cs_rows:
            ops = []
            for r in cs_rows:
                doc = {
                    "track_id": r["track_id"],
                    "quality": r["quality"],
                    "channel_msg_id": int(r["channel_msg_id"]),
                    "telegram_file_id": r["telegram_file_id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "duration_secs": int(r["duration_secs"] or 0),
                    "file_size_bytes": int(r["file_size_bytes"] or 0),
                    "source": r["source"] or "youtube",
                    "created_at": r["created_at"]
                }
                ops.append(
                    UpdateOne(
                        {"track_id": doc["track_id"], "quality": doc["quality"]},
                        {"$set": doc},
                        upsert=True
                    )
                )
            if ops:
                res = await mongo_db.channel_storage.bulk_write(ops)
                logger.info(f"MongoDB channel_storage: upserted={len(res.upserted_ids)}, modified={res.modified_count}, matched={res.matched_count}")

        # ── Table 3: DOWNLOAD_HISTORY ───────────────────────────────────
        logger.info("\n--- Migrating 'download_history' ---")
        hist_rows = await pg_conn.fetch("""
            SELECT user_chat_id, track_id, title, quality, channel_msg_id, created_at
            FROM download_history;
        """)
        results["download_history_pg"] = len(hist_rows)
        logger.info(f"Found {len(hist_rows)} download history records in PostgreSQL.")

        if not dry_run and hist_rows:
            ops = []
            for r in hist_rows:
                doc = {
                    "user_chat_id": int(r["user_chat_id"]),
                    "track_id": r["track_id"],
                    "title": r["title"],
                    "quality": r["quality"],
                    "channel_msg_id": int(r["channel_msg_id"]) if r["channel_msg_id"] else None,
                    "created_at": r["created_at"]
                }
                # Use composite filter to prevent duplicates on repeated migration runs
                ops.append(
                    UpdateOne(
                        {
                            "user_chat_id": doc["user_chat_id"],
                            "track_id": doc["track_id"],
                            "created_at": doc["created_at"]
                        },
                        {"$set": doc},
                        upsert=True
                    )
                )
            if ops:
                res = await mongo_db.download_history.bulk_write(ops)
                logger.info(f"MongoDB download_history: upserted={len(res.upserted_ids)}, matched={res.matched_count}")

        # ── Table 4: API_CACHE ──────────────────────────────────────────
        logger.info("\n--- Migrating 'api_cache' ---")
        cache_rows = await pg_conn.fetch("""
            SELECT cache_key, response_json, expires_at, created_at
            FROM api_cache;
        """)
        results["api_cache_pg"] = len(cache_rows)
        logger.info(f"Found {len(cache_rows)} cache entries in PostgreSQL.")

        if not dry_run and cache_rows:
            ops = []
            for r in cache_rows:
                raw_json = r["response_json"]
                if isinstance(raw_json, str):
                    try:
                        parsed_json = json.loads(raw_json)
                    except Exception:
                        parsed_json = raw_json
                else:
                    parsed_json = raw_json

                doc = {
                    "cache_key": r["cache_key"],
                    "response_json": parsed_json,
                    "expires_at": r["expires_at"],
                    "created_at": r["created_at"]
                }
                ops.append(
                    UpdateOne(
                        {"cache_key": doc["cache_key"]},
                        {"$set": doc},
                        upsert=True
                    )
                )
            if ops:
                res = await mongo_db.api_cache.bulk_write(ops)
                logger.info(f"MongoDB api_cache: upserted={len(res.upserted_ids)}, matched={res.matched_count}")

        # ── Table 5: RATE_LIMITS ─────────────────────────────────────────
        logger.info("\n--- Migrating 'rate_limits' ---")
        rl_rows = await pg_conn.fetch("""
            SELECT user_chat_id, action, attempt_count, window_start
            FROM rate_limits;
        """)
        results["rate_limits_pg"] = len(rl_rows)
        logger.info(f"Found {len(rl_rows)} rate limit records in PostgreSQL.")

        if not dry_run and rl_rows:
            ops = []
            for r in rl_rows:
                doc = {
                    "user_chat_id": int(r["user_chat_id"]),
                    "action": r["action"],
                    "attempt_count": int(r["attempt_count"] or 1),
                    "window_start": r["window_start"]
                }
                ops.append(
                    UpdateOne(
                        {"user_chat_id": doc["user_chat_id"], "action": doc["action"]},
                        {"$set": doc},
                        upsert=True
                    )
                )
            if ops:
                res = await mongo_db.rate_limits.bulk_write(ops)
                logger.info(f"MongoDB rate_limits: upserted={len(res.upserted_ids)}, matched={res.matched_count}")

        # ── Ensure Indexes ───────────────────────────────────────────────
        if not dry_run:
            logger.info("\nEnsuring MongoDB indexes...")
            await mongo_db.users.create_index("chat_id", unique=True)
            await mongo_db.channel_storage.create_index([("track_id", 1), ("quality", 1)], unique=True)
            await mongo_db.channel_storage.create_index("track_id")
            await mongo_db.api_cache.create_index("cache_key", unique=True)
            await mongo_db.api_cache.create_index("expires_at")
            await mongo_db.download_history.create_index("user_chat_id")
            await mongo_db.download_history.create_index([("user_chat_id", 1), ("created_at", -1)])
            await mongo_db.rate_limits.create_index([("user_chat_id", 1), ("action", 1)], unique=True)
            logger.info("Indexes built successfully.")

        # ── Count Verification ───────────────────────────────────────────
        if not dry_run:
            results["users_mongo"] = await mongo_db.users.count_documents({})
            results["channel_storage_mongo"] = await mongo_db.channel_storage.count_documents({})
            results["download_history_mongo"] = await mongo_db.download_history.count_documents({})
            results["api_cache_mongo"] = await mongo_db.api_cache.count_documents({})
            results["rate_limits_mongo"] = await mongo_db.rate_limits.count_documents({})
        else:
            results["users_mongo"] = 0
            results["channel_storage_mongo"] = 0
            results["download_history_mongo"] = 0
            results["api_cache_mongo"] = 0
            results["rate_limits_mongo"] = 0

        results["target_database"] = target_db_name

        print("\n" + "=" * 65)
        print(f"MIGRATION SUMMARY (Target DB: {target_db_name})")
        print("=" * 65)
        print(f"{'Collection':<22} | {'PostgreSQL':<12} | {'MongoDB':<12} | {'Status'}")
        print("-" * 65)
        for tbl in ["users", "channel_storage", "download_history", "api_cache", "rate_limits"]:
            pg_cnt = results.get(f"{tbl}_pg", 0)
            mg_cnt = results.get(f"{tbl}_mongo", 0)
            status = "MATCH" if pg_cnt == mg_cnt and not dry_run else ("DRY RUN" if dry_run else "MISMATCH")
            print(f"{tbl:<22} | {pg_cnt:<12} | {mg_cnt:<12} | {status}")
        print("=" * 65)

        return results

    finally:
        await pg_conn.close()
        mongo_client.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate Neon PostgreSQL data to MongoDB Atlas")
    parser.add_argument(
        "--pg-url",
        default=os.getenv("DATABASE_URL") if (os.getenv("DATABASE_URL") or "").startswith("postgres") else DEFAULT_PG_URL,
        help="PostgreSQL connection URI"
    )
    parser.add_argument(
        "--mongo-url",
        default=os.getenv("MONGO_URL") or DEFAULT_MONGO_URL,
        help="MongoDB connection URI"
    )
    parser.add_argument(
        "--db-name",
        default=os.getenv("MONGO_DB_NAME"),
        help="MongoDB database name override"
    )
    parser.add_argument(
        "--drop-target",
        action="store_true",
        help="Drop target MongoDB collections before migration"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect counts without writing to MongoDB"
    )

    args = parser.parse_args()

    asyncio.run(
        migrate(
            pg_url=args.pg_url,
            mongo_url=args.mongo_url,
            db_name=args.db_name,
            drop_target=args.drop_target,
            dry_run=args.dry_run
        )
    )


if __name__ == "__main__":
    main()
