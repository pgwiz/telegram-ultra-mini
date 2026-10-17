"""Unit tests for playlist batch sizing, Smart Mode state machine, and button styling."""

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


if __name__ == "__main__":
    unittest.main()

