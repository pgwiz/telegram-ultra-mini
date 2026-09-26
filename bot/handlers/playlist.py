"""Playlist and album batch download handlers."""

import asyncio
import logging
from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message
from bot.utils.link_detector import extract_media_info
from bot.api_client import api_client
from bot.storage import storage_manager
from bot.downloader import downloader

logger = logging.getLogger(__name__)
router = Router(name="playlist")


@router.message(Command("playlist"))
async def handle_playlist_command(message: Message, bot: Bot):
    """Handle /playlist <url> command."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/playlist &lt;youtube or spotify playlist url&gt;</code>", parse_mode="HTML")
        return

    url = args[1].strip()
    platform, media_type, identifier = extract_media_info(url)
    if not identifier:
        await message.answer("❌ Could not recognize a valid playlist link.", parse_mode="HTML")
        return

    await process_playlist(message, bot, platform, identifier)


async def process_playlist(message: Message, bot: Bot, platform: str, identifier: str):
    """Batch process playlist tracks one-by-one with channel warehousing."""
    status_msg = await message.answer("🔍 <i>Fetching playlist tracks...</i>", parse_mode="HTML")

    try:
        if platform == "spotify":
            tracks = await api_client.get_spotify_playlist(identifier, limit=25)
        else:
            tracks = await api_client.get_youtube_playlist(identifier, limit=25)

        if not tracks:
            await status_msg.edit_text("❌ Could not retrieve tracks from this playlist.")
            return

        total_tracks = len(tracks)
        await status_msg.edit_text(
            f"📋 <b>Found {total_tracks} tracks.</b>\n"
            f"⚡ Processing individual tracks (cached tracks deliver instantly)...",
            parse_mode="HTML"
        )

        completed = 0
        for idx, track in enumerate(tracks, 1):
            title = track.get("title") or track.get("name") or f"Track {idx}"
            artist = track.get("artist") or track.get("uploader") or "Unknown"
            video_id = track.get("videoId") or track.get("id")

            if not video_id:
                continue

            # Update progress status
            await status_msg.edit_text(
                f"⏳ <b>Processing [{idx}/{total_tracks}]:</b>\n"
                f"🎵 <i>{title}</i>",
                parse_mode="HTML"
            )

            # 1. Try instant channel cache delivery
            delivered = await storage_manager.deliver_cached(
                bot=bot,
                user_chat_id=message.chat.id,
                track_id=video_id,
                quality="audio_high"
            )

            # 2. If not cached, download & cache
            if not delivered:
                file_path, thumb_path, meta = await downloader.download_track(video_id, quality="audio_high")
                if file_path:
                    await storage_manager.upload_and_cache(
                        bot=bot,
                        user_chat_id=message.chat.id,
                        file_path=file_path,
                        track_id=video_id,
                        quality="audio_high",
                        title=title,
                        artist=artist,
                        duration=meta.get("duration"),
                        thumbnail_path=thumb_path,
                        source="api_playlist"
                    )

            completed += 1
            # Respect Telegram flood limits
            await asyncio.sleep(1.0)

        await status_msg.edit_text(f"✅ <b>Playlist completed!</b> Delivered {completed}/{total_tracks} tracks.", parse_mode="HTML")

    except Exception as e:
        logger.error(f"Playlist error for {identifier}: {e}")
        await status_msg.edit_text("❌ An error occurred during playlist processing.")
