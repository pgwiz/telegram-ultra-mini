"""Storage Channel Manager.

Handles uploading downloaded tracks to the private storage channel,
saving message IDs into Neon PostgreSQL, and delivering cached tracks
instantly via copy_message.
"""

import os
import logging
from typing import Optional, Dict, Any
from aiogram import Bot
from aiogram.types import FSInputFile, Message
from bot.config import settings
from bot.database import db

logger = logging.getLogger(__name__)


class StorageManager:
    def __init__(self):
        self.channel_id = settings.STORAGE_CHANNEL_ID

    async def get_cached(self, track_id: str, quality: str) -> Optional[Dict[str, Any]]:
        """Check if track at specified quality already exists in storage."""
        return await db.get_cached_track(track_id, quality)

    async def deliver_cached(
        self,
        bot: Bot,
        user_chat_id: int,
        track_id: str,
        quality: str
    ) -> bool:
        """
        Attempt to deliver track directly from storage channel using copy_message.
        Returns True if delivered, False if cache miss.
        """
        cached = await self.get_cached(track_id, quality)
        if not cached or not cached.get("channel_msg_id"):
            return False

        channel_msg_id = cached["channel_msg_id"]
        try:
            # Copy message from storage channel to user chat
            await bot.copy_message(
                chat_id=user_chat_id,
                from_chat_id=self.channel_id,
                message_id=channel_msg_id
            )
            # Record user download history
            await db.add_history(
                user_chat_id=user_chat_id,
                track_id=track_id,
                title=cached.get("title") or "Audio Track",
                quality=quality,
                channel_msg_id=channel_msg_id
            )
            logger.info(f"Delivered cached track {track_id} ({quality}) to user {user_chat_id} from channel msg {channel_msg_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to copy_message for {track_id} (msg {channel_msg_id}): {e}")
            return False

    async def upload_and_cache(
        self,
        bot: Bot,
        user_chat_id: int,
        file_path: str,
        track_id: str,
        quality: str,
        title: str,
        artist: Optional[str] = None,
        duration: Optional[int] = None,
        thumbnail_path: Optional[str] = None,
        source: str = "api"
    ) -> bool:
        """
        Upload audio file to storage channel, index in Neon DB, and copy to user.
        """
        if not os.path.exists(file_path):
            logger.error(f"Cannot upload: file does not exist {file_path}")
            return False

        file_size = os.path.getsize(file_path)
        thumb_input = FSInputFile(thumbnail_path) if thumbnail_path and os.path.exists(thumbnail_path) else None
        is_video = quality in ("720p", "360p", "best", "video") or file_path.endswith(".mp4")

        try:
            logger.info(f"Uploading {'video' if is_video else 'audio'} {track_id} to storage channel {self.channel_id}...")
            # 1. Send to private storage channel
            if is_video:
                video_input = FSInputFile(file_path, filename=f"{title}.mp4")
                channel_msg: Message = await bot.send_video(
                    chat_id=self.channel_id,
                    video=video_input,
                    caption=f"🎬 <b>{title}</b>\n👤 {artist or 'Unknown'}\n⚙️ Quality: {quality}\n🆔 #{track_id}",
                    duration=duration or 0,
                    thumbnail=thumb_input,
                    supports_streaming=True,
                    parse_mode="HTML"
                )
                telegram_file_id = channel_msg.video.file_id if channel_msg.video else None
            else:
                audio_input = FSInputFile(file_path, filename=f"{artist or 'Artist'} - {title}.mp3")
                channel_msg: Message = await bot.send_audio(
                    chat_id=self.channel_id,
                    audio=audio_input,
                    title=title,
                    performer=artist or "Unknown Artist",
                    duration=duration or 0,
                    thumbnail=thumb_input,
                    caption=f"🎵 {title}\n👤 {artist or 'Unknown'}\n⚙️ Quality: {quality}\n🆔 #{track_id}"
                )
                telegram_file_id = channel_msg.audio.file_id if channel_msg.audio else None

            channel_msg_id = channel_msg.message_id

            # 2. Persist message ID and metadata into Neon DB
            await db.save_cached_track(
                track_id=track_id,
                quality=quality,
                channel_msg_id=channel_msg_id,
                telegram_file_id=telegram_file_id,
                title=title,
                artist=artist,
                duration_secs=duration,
                file_size_bytes=file_size,
                source=source
            )

            # 3. Copy message to requesting user
            await bot.copy_message(
                chat_id=user_chat_id,
                from_chat_id=self.channel_id,
                message_id=channel_msg_id
            )

            # 4. Log download history
            await db.add_history(
                user_chat_id=user_chat_id,
                track_id=track_id,
                title=title,
                quality=quality,
                channel_msg_id=channel_msg_id
            )

            logger.info(f"Successfully stored and copied track {track_id} (msg {channel_msg_id}) to {user_chat_id}")
            return True

        except Exception as e:
            logger.error(f"Error in upload_and_cache for {track_id}: {e}")
            return False

        finally:
            # 5. Clean up temporary files if auto-cleanup is enabled
            if settings.AUTO_CLEANUP_TEMP:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                except Exception as clean_err:
                    logger.warning(f"Error cleaning up temp file {file_path}: {clean_err}")


storage_manager = StorageManager()
