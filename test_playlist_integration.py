"""Unit tests for playlist batch sizing, Smart Mode state machine, and button styling."""

import os
import unittest
import asyncio
from bot.api_client import api_client
from bot.handlers.playlist import PlaylistSession, build_batch_keyboard, ACTIVE_SESSIONS


class TestPlaylistBatch(unittest.IsolatedAsyncioTestCase):

    async def test_batch_keyboard_small_playlist(self):
        session = PlaylistSession(
            session_id="test_sm",
            user_id=12345,
            chat_id=12345,
            platform="spotify",
            identifier="test_pl_1",
            title="Short Playlist",
            author="Curator",
            total_available=15,
            tracks=[{"title": f"T{i}"} for i in range(15)]
        )
        kb = build_batch_keyboard(session)
        # Should have Download All, Smart Mode, Cancel
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Download All (15)" in t for t in texts))
        self.assertTrue(any("Smart Mode" in t for t in texts))
        self.assertTrue(any("Cancel" in t for t in texts))

    async def test_batch_keyboard_large_playlist(self):
        session = PlaylistSession(
            session_id="test_lg",
            user_id=12345,
            chat_id=12345,
            platform="spotify",
            identifier="769zdIYqQARK3BKxUNBxZ8",
            title="Large Playlist",
            author="Spotify",
            total_available=100,
            tracks=[{"title": f"T{i}"} for i in range(100)]
        )
        kb = build_batch_keyboard(session)
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("25 Tracks" in t for t in texts))
        self.assertTrue(any("50 Tracks" in t for t in texts))
        self.assertTrue(any("100 Tracks" in t for t in texts))
        self.assertTrue(any("All (100)" in t for t in texts))
        self.assertTrue(any("Smart Mode" in t for t in texts))
        self.assertTrue(any("Cancel" in t for t in texts))

    async def test_spotify_playlist_100_tracks_fetch(self):
        """Test fetching the user's specific playlist 769zdIYqQARK3BKxUNBxZ8."""
        info = await api_client.get_playlist_info("spotify", "769zdIYqQARK3BKxUNBxZ8", limit=100)
        self.assertEqual(info.get("total_tracks"), 100)
        self.assertEqual(len(info.get("tracks", [])), 100)
        self.assertIn("chiki chaka", info.get("title", ""))

    def test_session_chat_id_compatibility(self):
        """Verify PlaylistSession supports both session.chat_id and session.chat.id seamlessly."""
        session = PlaylistSession(
            session_id="test_chat",
            user_id=12345,
            chat_id=67890,
            platform="spotify",
            identifier="test_pl",
            title="Chat Test",
            author="Artist",
            total_available=1,
            tracks=[{"title": "Track 1"}]
        )
        self.assertEqual(session.chat_id, 67890)
        self.assertEqual(session.chat.id, 67890)

    async def test_find_cached_track_fallback(self):
        """Verify find_cached_track resolves by track_id and case-insensitive title+artist."""
        from bot.database import Database
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        test_db = Database()
        test_db.db_url = f"sqlite:///{temp_db}"
        await test_db.connect()

        try:
            # 1. Save track into channel_storage
            await test_db.save_cached_track(
                track_id="vid_123",
                quality="audio_high",
                channel_msg_id=777,
                telegram_file_id="tg_file_777",
                title="Only Girl (In The World)",
                artist="Rihanna",
                duration_secs=235
            )

            # 2. Lookup by track_id
            res1 = await test_db.find_cached_track(track_id="vid_123", quality="audio_high")
            self.assertIsNotNone(res1)
            self.assertEqual(res1["channel_msg_id"], 777)

            # 3. Lookup by Title and Artist (case-insensitive) without track_id
            res2 = await test_db.find_cached_track(
                title="only girl (in the world)",
                artist="rihanna",
                quality="audio_high"
            )
            self.assertIsNotNone(res2)
            self.assertEqual(res2["channel_msg_id"], 777)
            self.assertEqual(res2["title"], "Only Girl (In The World)")

            # 4. Lookup non-existent track returns None
            res3 = await test_db.find_cached_track(title="Nonexistent Song 999", quality="audio_high")
            self.assertIsNone(res3)

        finally:
            await test_db.disconnect()
            if os.path.exists(temp_db):
                try:
                    os.remove(temp_db)
                except Exception:
                    pass

    async def test_upload_and_cache_direct_user_fallback(self):
        """Verify upload_and_cache falls back directly to user when channel raises 'chat not found'."""
        from unittest.mock import AsyncMock, MagicMock
        from aiogram.exceptions import TelegramBadRequest
        from bot.storage import storage_manager
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(b"fake audio data")
            tf_path = tf.name

        mock_bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.audio.file_id = "audio_123"
        mock_msg.message_id = 999

        def mock_send_audio(*args, **kwargs):
            if kwargs.get("chat_id") == storage_manager.channel_id:
                raise TelegramBadRequest(method="sendAudio", message="Telegram server says - Bad Request: chat not found")
            return mock_msg

        mock_bot.send_audio.side_effect = mock_send_audio

        try:
            success = await storage_manager.upload_and_cache(
                bot=mock_bot,
                user_chat_id=987654,
                file_path=tf_path,
                track_id="fallback_trk",
                quality="audio_high",
                title="Test Song",
                artist="Test Artist",
                duration=180
            )
            self.assertTrue(success)
            # Verify mock_bot.send_audio was called with user_chat_id=987654
            calls = mock_bot.send_audio.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs.get("chat_id"), storage_manager.channel_id)
            self.assertEqual(calls[1].kwargs.get("chat_id"), 987654)
            # Verify temp file was purged
            self.assertFalse(os.path.exists(tf_path))
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    async def test_bot_settings_persistence(self):
        """Verify dynamic setting storage and storage_manager syncing."""
        from bot.database import Database
        from bot.storage import StorageManager
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        test_db = Database(db_url=f"sqlite:///{temp_db}")
        self.assertTrue(test_db.is_sqlite)
        self.assertFalse(test_db.is_mongo)
        await test_db.connect()

        try:
            # 1. Test set and get
            await test_db.set_setting("test_storage_channel_id", "-1001122334455")
            val = await test_db.get_setting("test_storage_channel_id")
            self.assertEqual(val, "-1001122334455")

            # 2. Test fallback default
            missing = await test_db.get_setting("non_existent_key", default="default_val")
            self.assertEqual(missing, "default_val")

            # 3. Test delete_setting
            deleted = await test_db.delete_setting("test_storage_channel_id")
            self.assertTrue(deleted)
            self.assertIsNone(await test_db.get_setting("test_storage_channel_id"))
        finally:
            await test_db.disconnect()
            if os.path.exists(temp_db):
                os.remove(temp_db)

    async def test_channel_manager_web_security(self):
        """Verify /channel returns 404 (no response) for normal users and 200 for admin token."""
        from httpx import AsyncClient, ASGITransport
        from bot.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Normal user without token -> 404 (no response)
            resp_no_token = await client.get("/channel")
            self.assertEqual(resp_no_token.status_code, 404)

            # 2. Normal user with wrong token -> 404 (no response)
            resp_wrong_token = await client.get("/channel?token=fake_random_token")
            self.assertEqual(resp_wrong_token.status_code, 404)

            # 3. Admin user with valid token -> 200 and dashboard HTML
            resp_admin = await client.get("/channel?token=bf48c7ef-17fd-4bdd-aace-bc6f18403e18")
            self.assertEqual(resp_admin.status_code, 200)
            self.assertIn("Storage Channel Manager", resp_admin.text)
            self.assertIn("Channel Status", resp_admin.text)

    async def test_channel_command_silent_for_normal_users(self):
        """Verify /channel and /channels command returns no response for normal non-admin users."""
        from unittest.mock import AsyncMock, MagicMock
        from bot.handlers.admin import handle_channels_command

        mock_bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.from_user.id = 999999999  # Normal user, definitely not admin

        await handle_channels_command(mock_msg, mock_bot)
        # Verify message.answer was NEVER called (silent / no response)
        mock_msg.answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()

