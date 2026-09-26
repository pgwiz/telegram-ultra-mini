"""Admin, statistics, and system maintenance handlers."""

import os
import re
import logging
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from bot.config import settings
from bot.database import db
from bot.cache import cache
from bot.storage import storage_manager

logger = logging.getLogger(__name__)
router = Router(name="admin")


def make_admin_keyboard() -> InlineKeyboardMarkup:
    """Build interactive Admin Control Panel keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 System Stats", callback_data="adm:stats"),
            InlineKeyboardButton(text="🧹 Run Cleanup", callback_data="adm:cleanup")
        ],
        [
            InlineKeyboardButton(text="⚡ Clear RAM Cache", callback_data="adm:cache_clear")
        ]
    ])


@router.message(Command("admin"))
async def handle_admin_panel(message: Message):
    """Handle /admin command (admin only)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        await message.answer("⛔ Access denied. Admin only.")
        return

    text = (
        "👑 <b>Admin Control Panel</b>\n\n"
        "Manage cloud storage, maintain caches, and monitor performance.\n\n"
        "<b>Available Admin Commands:</b>\n"
        "• <code>/delete &lt;#track_id&gt;</code> - Purge track from DB & storage channel\n"
        "• <code>/cleanup</code> - Purge local temp files & expired DB cache\n"
        "• <code>/stats</code> - Real-time system & database statistics\n"
        "• <code>/cache_clear</code> - Reset in-memory RAM cache\n\n"
        "<i>Or use the interactive buttons below:</i>"
    )
    await message.answer(text, reply_markup=make_admin_keyboard(), parse_mode="HTML")


@router.message(Command("delete", "del"))
async def handle_delete_track(message: Message, bot: Bot):
    """
    Handle /delete <#track_id> or /del <#track_id> [quality].
    Also supports replying to an audio/video message with /delete or /del.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        await message.answer("⛔ Access denied. Admin only.")
        return

    track_id = None
    quality = None

    args = message.text.split(maxsplit=2)
    if len(args) > 1:
        track_id = args[1].strip()
        if len(args) > 2:
            quality = args[2].strip()

    # If no argument, check if message is a reply to an audio/video track
    if not track_id and message.reply_to_message:
        target_msg = message.reply_to_message
        caption_text = target_msg.caption or target_msg.text or ""
        match = re.search(r"#([a-zA-Z0-9_-]{5,32})", caption_text)
        if match:
            track_id = match.group(1)

    if not track_id:
        await message.answer(
            "Usage: <code>/delete &lt;#track_id&gt; [quality]</code>\n"
            "<i>Tip: You can also reply to any song message with <code>/delete</code>.</i>",
            parse_mode="HTML"
        )
        return

    clean_id = track_id.lstrip("#").strip()
    status_msg = await message.answer(f"⏳ Purging track <code>#{clean_id}</code>...", parse_mode="HTML")

    deleted_count = await storage_manager.delete_cached_track(bot, clean_id, quality=quality)
    if deleted_count > 0:
        await status_msg.edit_text(
            f"🗑 <b>Track Purged Successfully!</b>\n"
            f"🆔 <b>Track ID:</b> <code>#{clean_id}</code>\n"
            f"📦 <b>Deleted:</b> <code>{deleted_count}</code> version(s) from Neon DB and storage channel.",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"⚠️ Track <code>#{clean_id}</code> was not found in storage channel database.",
            parse_mode="HTML"
        )


@router.message(Command("cleanup"))
async def handle_cleanup(message: Message):
    """
    Handle /cleanup command (admin only).
    Removes temp files, expired DB cache entries, and flushes RAM.
    """
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        await message.answer("⛔ Access denied. Admin only.")
        return

    status_msg = await message.answer("🧹 <i>Running comprehensive system cleanup...</i>", parse_mode="HTML")
    
    # 1. Clean local temp files in DOWNLOAD_DIR
    download_path = Path(settings.DOWNLOAD_DIR)
    temp_files_removed = 0
    bytes_freed = 0

    if download_path.exists():
        for file in download_path.glob("*"):
            if file.is_file():
                try:
                    fsize = file.stat().st_size
                    file.unlink()
                    temp_files_removed += 1
                    bytes_freed += fsize
                except Exception as err:
                    logger.warning(f"Could not remove temp file {file}: {err}")

    # 2. Clean expired DB cache
    db_cache_purged = await db.cleanup_expired_cache()

    # 3. Clear RAM cache
    cache.clear_ram()

    mb_freed = bytes_freed / (1024 * 1024)
    result_text = (
        "🧹 <b>System Cleanup Complete!</b>\n\n"
        f"• <b>Local Temp Files Removed:</b> <code>{temp_files_removed}</code> ({mb_freed:.2f} MB freed)\n"
        f"• <b>Expired DB Cache Rows Purged:</b> <code>{db_cache_purged}</code>\n"
        f"• <b>In-Memory RAM Cache:</b> Flushed to 0 items"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")


@router.message(Command("stats"))
async def handle_stats(message: Message):
    """Handle /stats command (admin only)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        await message.answer("⛔ Access denied. Admin only.")
        return

    stats = await db.get_stats()
    text = (
        "📊 <b>Telegram Ultra Mini - System Stats</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{stats.get('total_users', 0)}</code>\n"
        f"💾 <b>Cached Channel Tracks:</b> <code>{stats.get('cached_tracks', 0)}</code>\n"
        f"⬇️ <b>Total Downloads Logged:</b> <code>{stats.get('total_downloads', 0)}</code>\n"
        f"⚡ <b>RAM Cache Items:</b> <code>{len(cache._ram_cache)}</code>\n"
        f"🌐 <b>API Base URL:</b> <code>{settings.YTSP_API_BASE_URL}</code>\n"
        f"📦 <b>Storage Channel ID:</b> <code>{settings.STORAGE_CHANNEL_ID}</code>"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("cache_clear"))
async def handle_cache_clear(message: Message):
    """Clear RAM cache (admin only)."""
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        return

    cache.clear_ram()
    await message.answer("🧹 In-memory RAM cache cleared successfully.")


@router.callback_query(F.data.startswith("adm:"))
async def handle_admin_callback(callback: CallbackQuery):
    """Handle interactive Admin Control Panel callbacks."""
    user_id = callback.from_user.id
    if not await db.is_admin(user_id):
        await callback.answer("⛔ Access denied.", show_alert=True)
        return

    action = callback.data.split(":")[1]

    if action == "stats":
        stats = await db.get_stats()
        text = (
            "📊 <b>Telegram Ultra Mini - System Stats</b>\n\n"
            f"👥 <b>Total Users:</b> <code>{stats.get('total_users', 0)}</code>\n"
            f"💾 <b>Cached Channel Tracks:</b> <code>{stats.get('cached_tracks', 0)}</code>\n"
            f"⬇️ <b>Total Downloads Logged:</b> <code>{stats.get('total_downloads', 0)}</code>\n"
            f"⚡ <b>RAM Cache Items:</b> <code>{len(cache._ram_cache)}</code>\n"
            f"🌐 <b>API Base URL:</b> <code>{settings.YTSP_API_BASE_URL}</code>\n"
            f"📦 <b>Storage Channel ID:</b> <code>{settings.STORAGE_CHANNEL_ID}</code>"
        )
        await callback.message.edit_text(text, reply_markup=make_admin_keyboard(), parse_mode="HTML")
        await callback.answer("Stats updated")

    elif action == "cleanup":
        await callback.answer("Running cleanup...")
        download_path = Path(settings.DOWNLOAD_DIR)
        temp_files_removed = 0
        bytes_freed = 0
        if download_path.exists():
            for file in download_path.glob("*"):
                if file.is_file():
                    try:
                        fsize = file.stat().st_size
                        file.unlink()
                        temp_files_removed += 1
                        bytes_freed += fsize
                    except Exception:
                        pass

        db_cache_purged = await db.cleanup_expired_cache()
        cache.clear_ram()
        mb_freed = bytes_freed / (1024 * 1024)

        result_text = (
            "🧹 <b>System Cleanup Complete!</b>\n\n"
            f"• <b>Local Temp Files Removed:</b> <code>{temp_files_removed}</code> ({mb_freed:.2f} MB freed)\n"
            f"• <b>Expired DB Cache Rows Purged:</b> <code>{db_cache_purged}</code>\n"
            f"• <b>In-Memory RAM Cache:</b> Flushed to 0 items"
        )
        await callback.message.edit_text(result_text, reply_markup=make_admin_keyboard(), parse_mode="HTML")

    elif action == "cache_clear":
        cache.clear_ram()
        await callback.answer("RAM cache cleared!", show_alert=True)


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

