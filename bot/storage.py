"""Storage Channel Manager.

Handles uploading downloaded tracks to the private storage channel,
saving message IDs into Neon PostgreSQL, and delivering cached tracks
instantly via copy_message.
"""

import os
import re
import logging
import asyncio
from typing import Optional, Dict, Any
from aiogram import Bot
from aiogram.types import FSInputFile, Message
from aiogram.exceptions import TelegramRetryAfter
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
        for attempt in range(1, 3):
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
            except TelegramRetryAfter as e:
                logger.warning(f"Telegram FloodWait during copy_message ({e.retry_after}s). Sleeping...")
                await asyncio.sleep(e.retry_after + 1.0)
            except Exception as e:
                logger.error(f"Failed to copy_message for {track_id} (msg {channel_msg_id}): {e}")
                return False
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
        source: str = "api",
        is_video: Optional[bool] = None
    ) -> bool:
        """
        Upload audio/video file to storage channel, index in Neon DB, and copy to user.
        """
        if not os.path.exists(file_path):
            logger.error(f"Cannot upload: file does not exist {file_path}")
            return False

        file_size = os.path.getsize(file_path)
        thumb_input = FSInputFile(thumbnail_path) if thumbnail_path and os.path.exists(thumbnail_path) else None
        
        if is_video is None:
            is_video = quality in ("720p", "360p", "best", "video") or file_path.endswith(".mp4")

        # Ensure duration is always an integer number of seconds (convert ms if > 10000)
        safe_duration = 0
        if duration is not None:
            try:
                if isinstance(duration, (int, float)) and duration > 10000:
                    safe_duration = int(duration // 1000)
                else:
                    safe_duration = int(duration)
            except Exception:
                safe_duration = 0

        # Sanitize filenames for Telegram multipart upload
        clean_title = re.sub(r'[\\/*?:"<>|\r\n]+', "", title).strip() or "Track"
        clean_artist = re.sub(r'[\\/*?:"<>|\r\n]+', "", artist or "Artist").strip() or "Artist"

        try:
            for attempt in range(1, 4):
                try:
                    logger.info(f"Uploading {'video' if is_video else 'audio'} {track_id} to storage channel {self.channel_id} (attempt {attempt}/3)...")
                    # 1. Send to private storage channel
                    if is_video:
                        video_input = FSInputFile(file_path, filename=f"{clean_title}.mp4")
                        channel_msg: Message = await bot.send_video(
                            chat_id=self.channel_id,
                            video=video_input,
                            caption=f"🎬 <b>{title}</b>\n👤 {artist or 'Unknown'}\n⚙️ Quality: {quality}\n🆔 #{track_id}",
                            duration=safe_duration,
                            thumbnail=thumb_input,
                            supports_streaming=True,
                            parse_mode="HTML"
                        )
                        telegram_file_id = channel_msg.video.file_id if channel_msg.video else None
                    else:
                        ext = os.path.splitext(file_path)[1].lstrip(".") or "mp3"
                        if ext not in ("mp3", "m4a", "ogg", "flac"):
                            ext = "mp3"
                        audio_input = FSInputFile(file_path, filename=f"{clean_artist} - {clean_title}.{ext}")
                        channel_msg: Message = await bot.send_audio(
                            chat_id=self.channel_id,
                            audio=audio_input,
                            title=title,
                            performer=artist or "Unknown Artist",
                            duration=safe_duration,
                            thumbnail=thumb_input,
                            caption=f"🎵 {title}\n👤 {artist or 'Unknown'}\n⚙️ Quality: {quality}\n🆔 #{track_id}"
                        )
                        telegram_file_id = channel_msg.audio.file_id if channel_msg.audio else None

                    channel_msg_id = channel_msg.message_id

                    # 2. Persist message ID and metadata into DB
                    await db.save_cached_track(
                        track_id=track_id,
                        quality=quality,
                        channel_msg_id=channel_msg_id,
                        telegram_file_id=telegram_file_id,
                        title=title,
                        artist=artist,
                        duration_secs=safe_duration,
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

                except TelegramRetryAfter as e:
                    logger.warning(f"Telegram FloodWait during upload_and_cache ({e.retry_after}s). Sleeping...")
                    await asyncio.sleep(e.retry_after + 1.0)
                except Exception as e:
                    logger.error(f"Error in upload_and_cache for {track_id}: {e}", exc_info=True)
                    return False

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

    async def delete_cached_track(
        self,
        bot: Bot,
        track_id: str,
        quality: Optional[str] = None
    ) -> int:
        """
        Delete track from Neon DB and remove message from private storage channel.
        Returns count of deleted storage records.
        """
        clean_track_id = track_id.lstrip("#").strip()
        msg_ids = await db.delete_cached_track(clean_track_id, quality=quality)
        deleted_count = 0
        for msg_id in msg_ids:
            try:
                await bot.delete_message(chat_id=self.channel_id, message_id=msg_id)
                deleted_count += 1
            except Exception as e:
                logger.warning(f"Could not delete message {msg_id} from storage channel {self.channel_id}: {e}")

        logger.info(f"Purged track {clean_track_id}: deleted {len(msg_ids)} db rows and {deleted_count} channel messages.")
        return len(msg_ids)


storage_manager = StorageManager()
