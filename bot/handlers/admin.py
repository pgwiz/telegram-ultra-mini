"""Admin, statistics, and history handlers."""

import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from bot.config import settings
from bot.database import db
from bot.cache import cache

logger = logging.getLogger(__name__)
router = Router(name="admin")


@router.message(Command("stats"))
async def handle_stats(message: Message):
    """Handle /stats command (admin only)."""
    user_id = message.from_user.id if message.from_user else 0
    if user_id != settings.ADMIN_CHAT_ID:
        await message.answer("⛔ Access denied. Admin only.")
        return

    stats = await db.get_stats()
    text = (
        "📊 <b>Telegram Ultra Mini - System Stats</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{stats['total_users']}</code>\n"
        f"💾 <b>Cached Channel Tracks:</b> <code>{stats['cached_tracks']}</code>\n"
        f"⬇️ <b>Total Downloads Logged:</b> <code>{stats['total_downloads']}</code>\n"
        f"⚡ <b>RAM Cache Items:</b> <code>{len(cache._ram_cache)}</code>\n"
        f"🌐 <b>API Base URL:</b> <code>{settings.YTSP_API_BASE_URL}</code>\n"
        f"📦 <b>Storage Channel ID:</b> <code>{settings.STORAGE_CHANNEL_ID}</code>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("history"))
async def handle_history(message: Message):
    """Handle /history command for user."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    history = await db.get_user_history(user_id, limit=8)
    if not history:
        await message.answer("📭 You don't have any recorded downloads yet.")
        return

    lines = ["📜 <b>Your Recent Downloads:</b>\n"]
    for idx, item in enumerate(history, 1):
        title = item.get("title", "Audio")
        quality = item.get("quality", "audio")
        date_str = item.get("created_at").strftime("%Y-%m-%d %H:%M") if item.get("created_at") else ""
        lines.append(f"{idx}. <b>{title}</b> (<code>{quality}</code>) - <i>{date_str}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("cache_clear"))
async def handle_cache_clear(message: Message):
    """Clear RAM cache (admin only)."""
    user_id = message.from_user.id if message.from_user else 0
    if user_id != settings.ADMIN_CHAT_ID:
        return

    cache.clear_ram()
    await message.answer("🧹 In-memory RAM cache cleared successfully.")
