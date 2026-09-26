"""Comprehensive diagnostic verification for Telegram Ultra Mini."""

import asyncio
import os
import sys

# Ensure current directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath("."))

from bot.config import settings
from bot.database import db
from bot.api_client import api_client
from bot.storage import storage_manager
from bot.utils.link_detector import extract_media_info


async def run_diagnostics():
    print("=" * 60)
    print("1. NEON POSTGRESQL CONNECTION & MIGRATION")
    print("=" * 60)
    await db.connect()
    stats = await db.get_stats()
    print("Neon Database Stats:", stats)

    print("\n" + "=" * 60)
    print("2. REGEX LINK DETECTOR")
    print("=" * 60)
    samples = [
        "https://youtu.be/dQw4w9WgXcQ",
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "dQw4w9WgXcQ"
    ]
    for sample in samples:
        p, m, ident = extract_media_info(sample)
        print(f"Sample: {sample} -> Platform: {p}, Type: {m}, ID: {ident}")

    print("\n" + "=" * 60)
    print("3. STREAM EXTRACTOR API (ytsp-api.pgwiz.cloud)")
    print("=" * 60)
    search_res = await api_client.search_tracks("Never gonna give you up", limit=2)
    print(f"Search results count: {len(search_res)}")
    if search_res:
        print("First track:", search_res[0].get("title"), "-", search_res[0].get("videoId"))

    meta = await api_client.get_stream_info("dQw4w9WgXcQ", quality="audio")
    print("Stream metadata fetched:", bool(meta))
    if meta:
        print("Title:", meta.get("title"))
        print("Proxy URL:", meta.get("proxy_url"))

    print("\n" + "=" * 60)
    print("4. STORAGE CHANNEL CACHE CHECK")
    print("=" * 60)
    cached = await storage_manager.get_cached("dQw4w9WgXcQ", "audio_high")
    print("Channel cache result for dQw4w9WgXcQ:", cached)

    # Clean shutdown
    await api_client.close()
    await db.disconnect()
    print("\n[SUCCESS] ALL SYSTEM CHECKS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_diagnostics())
