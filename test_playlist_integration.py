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


if __name__ == "__main__":
    unittest.main()
