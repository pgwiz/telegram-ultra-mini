"""Comprehensive automated test suite for MongoDB backend in Telegram Ultra Mini."""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import settings
from bot.database import Database

TEST_DB_NAME = "test_integration_suite_temp"
TEST_MONGO_URL = f"mongodb+srv://rtwoft_db_user:0sudhUOXWychqplX@clasto.nlhmbxk.mongodb.net/{TEST_DB_NAME}?retryWrites=true&w=majority"


class TestMongoIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Override settings for isolated test database
        self.orig_db_url = settings.DATABASE_URL
        settings.DATABASE_URL = TEST_MONGO_URL
        self.db = Database()
        await self.db.connect()

    async def asyncTearDown(self):
        # Drop test database and cleanup
        if self.db.mongo_client:
            await self.db.mongo_client.drop_database(TEST_DB_NAME)
        await self.db.disconnect()
        settings.DATABASE_URL = self.orig_db_url

    def test_database_name_resolution(self):
        r = Database.resolve_mongo_db_name
        # 1. URI path priority
        self.assertEqual(
            r("mongodb+srv://user:pass@cluster.mongodb.net/custom_db?retryWrites=true"),
            "custom_db"
        )
        # 2. MONGO_DB_NAME setting priority
        self.assertEqual(
            r("mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true", configured_db_name="env_db"),
            "env_db"
        )
        # 3. Bot token ID extraction
        self.assertEqual(
            r("mongodb+srv://user:pass@cluster.mongodb.net/", bot_token="8908464066:AAHLqC4Z3eM6hNCYHxJIuXrr2s1LmzMVjQc"),
            "bot_8908464066"
        )
        self.assertEqual(
            r("mongodb+srv://user:pass@cluster.mongodb.net/", bot_token="8126033137:AAEsd9EaUkSTAsk8jhvxe8pDbU_Jga9bza8"),
            "bot_8126033137"
        )
        # 4. Fallback
        self.assertEqual(
            r("mongodb+srv://user:pass@cluster.mongodb.net/"),
            "telegram_ultra"
        )

    async def test_backend_detection(self):
        self.assertTrue(self.db.is_mongo)
        self.assertFalse(self.db.is_postgres)
        self.assertFalse(self.db.is_sqlite)
        self.assertTrue(self.db.is_connected)
        self.assertEqual(self.db.active_database_name, TEST_DB_NAME)

    async def test_user_management(self):
        # Upsert normal user
        await self.db.upsert_user(123456, "alice", "Alice W")
        is_adm = await self.db.is_admin(123456)
        self.assertFalse(is_adm)

        # Upsert admin user
        admin_id = settings.ADMIN_CHAT_ID
        await self.db.upsert_user(admin_id, "admin_user", "Admin Master")
        is_admin_check = await self.db.is_admin(admin_id)
        self.assertTrue(is_admin_check)

        # Update existing user without losing is_admin
        await self.db.upsert_user(123456, "alice_renamed", "Alice Wonder")
        doc = await self.db.mongo_db.users.find_one({"chat_id": 123456})
        self.assertEqual(doc["username"], "alice_renamed")
        self.assertEqual(doc["first_name"], "Alice Wonder")

    async def test_channel_storage_and_cache(self):
        # 1. Save track
        await self.db.save_cached_track(
            track_id="song_abc_1",
            quality="audio_high",
            channel_msg_id=555,
            telegram_file_id="tg_file_555",
            title="Cool Track",
            artist="Cool Artist",
            duration_secs=210,
            file_size_bytes=8192000,
            source="youtube"
        )

        # 2. Retrieve track
        track = await self.db.get_cached_track("song_abc_1", "audio_high")
        self.assertIsNotNone(track)
        self.assertEqual(track["channel_msg_id"], 555)
        self.assertEqual(track["telegram_file_id"], "tg_file_555")
        self.assertEqual(track["title"], "Cool Track")
        self.assertEqual(track["artist"], "Cool Artist")
        self.assertEqual(track["duration_secs"], 210)
        self.assertEqual(track["file_size_bytes"], 8192000)

        # 3. Update existing track (upsert idempotency)
        await self.db.save_cached_track(
            track_id="song_abc_1",
            quality="audio_high",
            channel_msg_id=777,
            telegram_file_id="tg_file_777",
            title="Cool Track (Updated)",
            artist="Cool Artist",
            duration_secs=210,
            file_size_bytes=8192000
        )
        updated = await self.db.get_cached_track("song_abc_1", "audio_high")
        self.assertEqual(updated["channel_msg_id"], 777)
        self.assertEqual(updated["telegram_file_id"], "tg_file_777")

        # 4. Delete track
        deleted_ids = await self.db.delete_cached_track("song_abc_1", "audio_high")
        self.assertEqual(deleted_ids, [777])
        self.assertIsNone(await self.db.get_cached_track("song_abc_1", "audio_high"))

    async def test_download_history(self):
        user_id = 987654
        # Add 3 history entries
        for i in range(1, 4):
            await self.db.add_history(
                user_chat_id=user_id,
                track_id=f"track_{i}",
                title=f"Track Title {i}",
                quality="audio",
                channel_msg_id=100 + i
            )

        history = await self.db.get_user_history(user_id, limit=2)
        self.assertEqual(len(history), 2)
        # Should be ordered by most recent first
        self.assertEqual(history[0]["track_id"], "track_3")
        self.assertEqual(history[1]["track_id"], "track_2")

    async def test_rate_limiting(self):
        user_id = 112233
        action = "test_action"

        # Limit is 2 attempts per 5 seconds
        first = await self.db.check_rate_limit(user_id, action, limit=2, window_secs=5)
        second = await self.db.check_rate_limit(user_id, action, limit=2, window_secs=5)
        third = await self.db.check_rate_limit(user_id, action, limit=2, window_secs=5)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertFalse(third)  # Exceeded limit

    async def test_api_cache_and_expiration(self):
        # 1. Set cache with valid TTL
        data = {"tracks": [{"id": 1, "title": "Song A"}], "success": True}
        await self.db.set_api_cache("cache_valid", data, ttl_seconds=300)

        cached = await self.db.get_api_cache("cache_valid")
        self.assertEqual(cached, data)

        # 2. Manually insert expired cache entry
        past = datetime.utcnow() - timedelta(seconds=10)
        await self.db.mongo_db.api_cache.insert_one({
            "cache_key": "cache_expired",
            "response_json": {"expired": True},
            "expires_at": past,
            "created_at": past
        })

        self.assertIsNone(await self.db.get_api_cache("cache_expired"))

        # 3. Cleanup expired cache
        deleted_count = await self.db.cleanup_expired_cache()
        self.assertGreaterEqual(deleted_count, 1)

    async def test_stats(self):
        await self.db.upsert_user(101, "u1", "First1")
        await self.db.save_cached_track("track_x", "audio", 900)
        await self.db.add_history(101, "track_x", "Track X", "audio", 900)

        stats = await self.db.get_stats()
        self.assertIn("MongoDB", stats["backend"])
        self.assertEqual(stats["database_name"], TEST_DB_NAME)
        self.assertGreaterEqual(stats["total_users"], 1)
        self.assertGreaterEqual(stats["cached_tracks"], 1)
        self.assertGreaterEqual(stats["total_downloads"], 1)


if __name__ == "__main__":
    unittest.main()
