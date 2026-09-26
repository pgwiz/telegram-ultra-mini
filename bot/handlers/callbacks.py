"""Callback query handlers for interactive track selection, quality selection, and navigation."""

import logging
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from bot.storage import storage_manager
from bot.downloader import downloader
from bot.cache import cache
from bot.api_client import api_client
from bot.handlers.search import build_search_view

logger = logging.getLogger(__name__)
router = Router(name="callbacks")

QUALITY_LABELS = {
    "audio_high": "🎵 Audio High (320k)",
    "audio": "🎶 Audio Normal (192k)",
    "saver": "💾 Saver Audio (64k)",
    "720p": "🎬 Video HD (720p)",
    "360p": "🎬 Video SD (360p)",
}


@router.callback_query(F.data.startswith("sel:"))
async def handle_select_track(callback: CallbackQuery):
    """
    Handle track selection from search results: sel:{identifier}:{q_hash}
    Edits the search message into format selection (Audio vs Video).
    """
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("⚠️ Invalid track selection.", show_alert=True)
        return

    _, identifier, q_hash = parts[0], parts[1], parts[2]
    
    title = "Selected Track"
    artist = "Unknown Artist"
    duration = ""

    # 1. Lookup in search cache session
    session = await cache.get(f"search_session:{q_hash}")
    if session and "results" in session:
        for item in session["results"]:
            vid = item.get("videoId") or item.get("id")
            if vid == identifier:
                title = item.get("title") or item.get("name") or title
                artist = item.get("artist") or item.get("uploader") or artist
                duration = item.get("duration") or ""
                break
    else:
        # Fallback stream info lookup
        info = await api_client.get_stream_info(identifier)
        if info:
            title = info.get("title") or title
            artist = info.get("uploader") or info.get("artist") or artist
            duration = str(info.get("duration") or "")

    dur_text = f"\n⏱ <i>Duration:</i> <code>{duration}</code>" if duration else ""
    text = (
        f"🎧 <b>Selected Track:</b>\n"
        f"<b>{title}</b>\n"
        f"👤 <i>{artist}</i>"
        f"{dur_text}\n\n"
        f"👇 <b>Select download format:</b>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎵 Audio High (320k)", callback_data=f"dl:youtube:{identifier}:audio_high"),
            InlineKeyboardButton(text="🎶 Normal (192k)", callback_data=f"dl:youtube:{identifier}:audio"),
        ],
        [
            InlineKeyboardButton(text="🎬 Video HD (720p)", callback_data=f"dl:youtube:{identifier}:720p"),
            InlineKeyboardButton(text="🎬 Video SD (360p)", callback_data=f"dl:youtube:{identifier}:360p"),
        ],
        [
            InlineKeyboardButton(text="💾 Saver Audio (64k)", callback_data=f"dl:youtube:{identifier}:saver"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Back to Search Results", callback_data=f"back:{q_hash}"),
        ]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("back:"))
async def handle_back_to_search(callback: CallbackQuery):
    """
    Handle 'Back to Search' navigation: back:{q_hash}
    Restores the search results track list and numbered buttons.
    """
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("⚠️ Search expired.", show_alert=True)
        return

    q_hash = parts[1]
    session = await cache.get(f"search_session:{q_hash}")
    if not session or "results" not in session or "query" not in session:
        await callback.answer("⚠️ Search session expired. Please search again.", show_alert=True)
        return

    text, keyboard = build_search_view(session["query"], session["results"], q_hash)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


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
    label = QUALITY_LABELS.get(quality, quality)

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
        f"⏳ <b>Downloading {label}...</b>\n<i>Please wait while your media is being downloaded and verified...</i>",
        parse_mode="HTML"
    )

    file_path, thumb_path, meta = await downloader.download_track(identifier, quality=quality)
    if not file_path:
        await status_msg.edit_text(f"❌ <b>Download failed for {label}.</b> Please try another format or link.")
        return

    # 3. Upload to storage channel & copy to user
    await status_msg.edit_text(f"⚡ <b>Sending {label} to chat...</b>", parse_mode="HTML")
    success = await storage_manager.upload_and_cache(
        bot=bot,
        user_chat_id=user_id,
        file_path=file_path,
        track_id=identifier,
        quality=quality,
        title=meta.get("title", "Track"),
        artist=meta.get("artist"),
        duration=meta.get("duration"),
        thumbnail_path=thumb_path,
        source=meta.get("source", "api"),
        is_video=meta.get("is_video")
    )

    try:
        await status_msg.delete()
    except Exception:
        pass

    if not success:
        await callback.message.answer("⚠️ Failed to deliver track. Please try again.")

