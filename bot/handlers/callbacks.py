"""Callback query handlers for quality selection and inline actions."""

import logging
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from bot.storage import storage_manager
from bot.downloader import downloader

logger = logging.getLogger(__name__)
router = Router(name="callbacks")


@router.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery, bot: Bot):
    """
    Handle quality selection callback: dl:{platform}:{identifier}:{quality}
    """
    await callback.answer("Processing request...")
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.message.answer("⚠️ Invalid callback data.")
        return

    _, platform, identifier, quality = parts[0], parts[1], parts[2], parts[3]
    user_id = callback.from_user.id

    # 1. Check storage channel cache in Neon DB
    cached = await storage_manager.deliver_cached(
        bot=bot,
        user_chat_id=user_id,
        track_id=identifier,
        quality=quality
    )
    if cached:
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    # 2. Cache miss: trigger download
    status_msg = await callback.message.edit_text(
        f"⏳ <b>Downloading track...</b>\nQuality: <code>{quality}</code>",
        parse_mode="HTML"
    )

    file_path, thumb_path, meta = await downloader.download_track(identifier, quality=quality)
    if not file_path:
        await status_msg.edit_text("❌ <b>Download failed.</b> Please try again or check the link.")
        return

    # 3. Upload to storage channel & copy to user
    await status_msg.edit_text("⚡ <b>Caching to cloud storage...</b>", parse_mode="HTML")
    success = await storage_manager.upload_and_cache(
        bot=bot,
        user_chat_id=user_id,
        file_path=file_path,
        track_id=identifier,
        quality=quality,
        title=meta.get("title", "Audio Track"),
        artist=meta.get("artist"),
        duration=meta.get("duration"),
        thumbnail_path=thumb_path,
        source=meta.get("source", "api")
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not success:
        await callback.message.answer("⚠️ Failed to deliver track. Please try again.")
