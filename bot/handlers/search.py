"""Search handler using ytsp-api search endpoint."""

import hashlib
import logging
from typing import Tuple, List, Dict, Any
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from bot.api_client import api_client
from bot.database import db
from bot.cache import cache

logger = logging.getLogger(__name__)
router = Router(name="search")


def build_search_view(query: str, results: List[Dict[str, Any]], q_hash: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the track list message and interactive track selection buttons."""
    text_lines = [
        f"🔍 <b>Search results for:</b> <i>{query}</i>\n"
    ]
    buttons = []

    for idx, item in enumerate(results, 1):
        title = item.get("title") or item.get("name") or "Unknown"
        artist = item.get("artist") or item.get("uploader") or "Unknown"
        duration = item.get("duration") or ""
        video_id = item.get("videoId") or item.get("id")

        if not video_id:
            continue

        dur_str = f" ({duration})" if duration else ""
        text_lines.append(f"{idx}. <b>{title}</b> - <i>{artist}</i>{dur_str}")

        display_title = title if len(title) <= 32 else f"{title[:29]}..."
        buttons.append([
            InlineKeyboardButton(
                text=f"{idx}. {display_title}",
                callback_data=f"sel:{video_id}:{q_hash}"
            )
        ])

    text_lines.append("\n👇 <i>Tap a track above to select Audio or Video format.</i>")
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return "\n".join(text_lines), keyboard


@router.message(Command("search"))
async def handle_search(message: Message):
    """Handle /search <query> command."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/search &lt;song title or artist&gt;</code>", parse_mode="HTML")
        return

    query = args[1].strip()
    await execute_search(message, query)


async def execute_search(message: Message, query: str) -> None:
    """Execute music search and render interactive track selection list."""
    user_id = message.from_user.id if message.from_user else message.chat.id

    # Rate limit check
    if not await db.check_rate_limit(user_id, "search", limit=20, window_secs=60):
        await message.answer("⚠️ <b>Rate limit exceeded.</b> Please wait a moment before searching again.", parse_mode="HTML")
        return

    status_msg = await message.answer(f"🔍 <i>Searching for:</i> <b>{query}</b>...", parse_mode="HTML")

    try:
        results = await api_client.search_tracks(query, limit=6)
        if not results:
            await status_msg.edit_text(f"❌ No tracks found for <b>{query}</b>.", parse_mode="HTML")
            return

        q_hash = hashlib.md5(query.strip().lower().encode("utf-8")).hexdigest()[:8]
        # Cache search session for 1 hour
        await cache.set(f"search_session:{q_hash}", {"query": query, "results": results}, ttl_seconds=3600)

        msg_text, keyboard = build_search_view(query, results, q_hash)
        await status_msg.edit_text(msg_text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Search error for query '{query}': {e}")
        await status_msg.edit_text("❌ An error occurred while searching. Please try again.")
