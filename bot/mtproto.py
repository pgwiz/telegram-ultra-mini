"""MTProto client using Telethon for uploading files >50MB to the storage channel."""

import os
import logging
from typing import Optional
from bot.config import settings

logger = logging.getLogger(__name__)

# Telethon client instance
_client = None


async def get_mtproto_client():
    """Initialize and start Telethon client if MPROTO is enabled."""
    global _client
    if not settings.MPROTO or not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        return None

    if _client is not None and _client.is_connected():
        return _client

    try:
        from telethon import TelegramClient
        session_path = settings.MTPROTO_SESSION_PATH
        os.makedirs(os.path.dirname(os.path.abspath(session_path)), exist_ok=True)
        
        _client = TelegramClient(
            session_path,
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH
        )
        await _client.connect()
        if not await _client.is_user_authorized():
            logger.warning("MTProto client connected but not authorized. Run auth script to log in.")
            return None
        logger.info("MTProto Telethon client connected and authorized.")
        return _client
    except Exception as e:
        logger.error(f"Failed to initialize MTProto client: {e}")
        return None


async def upload_large_file_to_channel(
    file_path: str,
    channel_id: int,
    caption: str = "",
    thumbnail_path: Optional[str] = None
) -> Optional[int]:
    """
    Upload files >50MB via Telethon to storage channel.
    Returns the message ID in the channel, or None if failed.
    """
    client = await get_mtproto_client()
    if not client:
        logger.error("MTProto client is not available for large file upload.")
        return None

    try:
        entity = await client.get_entity(channel_id)
        msg = await client.send_file(
            entity=entity,
            file=file_path,
            caption=caption,
            thumb=thumbnail_path if thumbnail_path and os.path.exists(thumbnail_path) else None
        )
        return msg.id
    except Exception as e:
        logger.error(f"Failed to upload large file via MTProto: {e}")
        return None
