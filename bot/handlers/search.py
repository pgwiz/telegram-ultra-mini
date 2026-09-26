"""Search handler using ytsp-api search endpoint."""

import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from bot.api_client import api_client
from bot.database import db

logger = logging.getLogger(__name__)
router = Router(name="search")


@router.message(Command("search"))
async def handle_search(message: Message):
    """Handle /search <query> command."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/search &lt;song title or artist&gt;</code>", parse_mode="HTML")
        return

    query = args[1].strip()
    user_id = message.from_user.id if message.from_user else message.chat.id

    # Rate limit check
    if not await db.check_rate_limit(user_id, "search", limit=20, window_secs=60):
        await message.answer("⚠️ <b>Rate limit exceeded.</b> Please wait a moment before searching again.", parse_mode="HTML")
        return

    status_msg = await message.answer(f"🔍 <i>Searching for:</i> <b>{query}</b>...", parse_mode="HTML")

    try:
        results = await api_client.search_tracks(query, limit=8)
        if not results:
            await status_msg.edit_text(f"❌ No tracks found for <b>{query}</b>.", parse_mode="HTML")
            return

        buttons = []
        text_lines = [f"🎵 <b>Search results for:</b> <i>{query}</i>\n"]

        for idx, item in enumerate(results, 1):
            title = item.get("title") or item.get("name") or "Unknown"
            artist = item.get("artist") or item.get("uploader") or "Unknown"
            duration = item.get("duration") or ""
            video_id = item.get("videoId") or item.get("id")

            if not video_id:
                continue

            text_lines.append(f"{idx}. <b>{title}</b> - <i>{artist}</i> ({duration})")
            
            # Button to trigger 1-click download with high audio quality
            btn_label = f"⬇️ {idx}. {title[:28]}"
            buttons.append([
                InlineKeyboardButton(
                    text=btn_label,
                    callback_data=f"dl:youtube:{video_id}:audio_high"
                )
            ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await status_msg.edit_text("\n".join(text_lines), reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Search error for query '{query}': {e}")
        await status_msg.edit_text("❌ An error occurred while searching. Please try again.")
