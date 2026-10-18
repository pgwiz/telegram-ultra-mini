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
            InlineKeyboardButton(text="📢 Channel Manager", callback_data="adm:channels")
        ],
        [
            InlineKeyboardButton(text="📜 View Logs", callback_data="adm:logs"),
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

    elif action == "logs":
        from bot.main import memory_log_handler
        logs = list(memory_log_handler.buffer)[-15:]
        log_text = "\n".join(logs) if logs else "No logs captured yet."
        if len(log_text) > 3500:
            log_text = log_text[-3500:]
        escaped = log_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = f"📜 <b>Recent Live Logs:</b>\n<pre>{escaped}</pre>"
        await callback.message.edit_text(text, reply_markup=make_admin_keyboard(), parse_mode="HTML")
        await callback.answer("Logs refreshed")

    elif action in ("channels", "channel_refresh"):
        text, kb = await get_channel_diagnostics(callback.bot)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer("Channel status refreshed")

    elif action == "panel":
        text = (
            "👑 <b>Admin Control Panel</b>\n\n"
            "Manage cloud storage, maintain caches, and monitor performance.\n\n"
            "<b>Available Admin Commands:</b>\n"
            "• <code>/channel</code> - Inspect & manage storage channel\n"
            "• <code>/setchannel &lt;id&gt;</code> - Set active storage channel\n"
            "• <code>/delete &lt;#track_id&gt;</code> - Purge track from DB & storage channel\n"
            "• <code>/cleanup</code> - Purge local temp files & expired DB cache\n"
            "• <code>/stats</code> - Real-time system & database statistics\n"
            "• <code>/cache_clear</code> - Reset in-memory RAM cache\n\n"
            "<i>Or use the interactive buttons below:</i>"
        )
        await callback.message.edit_text(text, reply_markup=make_admin_keyboard(), parse_mode="HTML")
        await callback.answer()


async def get_channel_diagnostics(bot: Bot):
    """Inspect current storage channel, test bot permissions, and build management buttons."""
    cid = await storage_manager.sync_channel_id()
    bot_me = await bot.get_me()
    web_url = f"https://telegram-ultra-mini.onrender.com/channel?token={settings.ADMIN_TOKEN}"

    try:
        chat = await bot.get_chat(cid)
        title = chat.title or "Storage Channel"
        chat_type = chat.type
        username = f"@{chat.username}" if chat.username else "Private"

        open_link = None
        if chat.username:
            open_link = f"https://t.me/{chat.username}"
        elif chat.invite_link:
            open_link = chat.invite_link
        else:
            try:
                invite = await bot.create_chat_invite_link(cid, name="Storage Manager")
                open_link = invite.invite_link
            except Exception:
                open_link = None

        can_post = True
        try:
            m = await bot.get_chat_member(cid, bot_me.id)
            can_post = getattr(m, "can_post_messages", m.status == "administrator")
        except Exception:
            pass

        status_icon = "🟢" if can_post else "🟡"
        post_badge = "✅ Allowed" if can_post else "⚠️ Restricted (Cannot Post)"

        lines = [
            f"📢 <b>Storage Channel Manager</b>\n",
            f"📦 <b>Channel:</b> {title}",
            f"🆔 <b>Channel ID:</b> <code>{cid}</code>",
            f"🔒 <b>Type:</b> {chat_type.capitalize()} ({username})",
            f"🤖 <b>Bot Status:</b> {status_icon} Administrator",
            f"✍️ <b>Posting Rights:</b> {post_badge}",
        ]
        if open_link:
            lines.append(f"🔗 <b>Invite Link:</b> {open_link}")

        lines.append("\n✅ <i>Channel is connected and accessible. All tracks are cached here and copied instantly to users.</i>")

        buttons = []
        if open_link:
            buttons.append([InlineKeyboardButton(text="📢 Open Storage Channel ↗️", url=open_link)])
        buttons.append([
            InlineKeyboardButton(text="🌐 Web Channel Manager ↗️", url=web_url),
            InlineKeyboardButton(text="🔄 Refresh", callback_data="adm:channel_refresh")
        ])
        buttons.append([
            InlineKeyboardButton(text="🔙 Back to Admin", callback_data="adm:panel")
        ])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    except Exception as e:
        error_str = str(e)
        lines = [
            f"⚠️ <b>Storage Channel Inaccessible</b>\n",
            f"🆔 <b>Configured ID:</b> <code>{cid}</code>",
            f"❌ <b>Error:</b> <i>{error_str}</i>\n",
            f"💡 <b>How to Connect Your Channel:</b>",
            f"1. Open or create your private channel in Telegram.",
            f"2. Add <b>@{bot_me.username}</b> as an <b>Administrator</b> with <i>'Post Messages'</i> permission.",
            f"3. <b>Forward any post from that channel to me right here</b>, or run:",
            f"   <code>/setchannel &lt;channel_id&gt;</code>\n",
            f"🌐 <i>Or configure it using the Web Channel Manager below:</i>"
        ]
        buttons = [
            [InlineKeyboardButton(text="🌐 Web Channel Manager ↗️", url=web_url)],
            [InlineKeyboardButton(text="🔄 Re-test Connection", callback_data="adm:channel_refresh")],
            [InlineKeyboardButton(text="🔙 Back to Admin", callback_data="adm:panel")]
        ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("channel", "channels"))
async def handle_channels_command(message: Message, bot: Bot):
    """Handle /channel and /channels command. Strictly no response for non-admins."""
    user_id = message.from_user.id if message.from_user else 0
    # For normal users, return NO RESPONSE (completely silent)
    if user_id != settings.ADMIN_CHAT_ID and not await db.is_admin(user_id):
        return

    text, keyboard = await get_channel_diagnostics(bot)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("setchannel"))
async def handle_setchannel_command(message: Message, bot: Bot):
    """Handle /setchannel <channel_id> command. Strictly no response for non-admins."""
    user_id = message.from_user.id if message.from_user else 0
    if user_id != settings.ADMIN_CHAT_ID and not await db.is_admin(user_id):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Usage: <code>/setchannel &lt;channel_id&gt;</code>\n"
            "Example: <code>/setchannel -1001234567890</code>\n\n"
            "<i>Tip: You can also just forward any post from your channel to this chat!</i>",
            parse_mode="HTML"
        )
        return

    raw_id = args[1].strip()
    try:
        new_id = int(raw_id)
    except ValueError:
        await message.answer("❌ Invalid channel ID. Must be an integer like <code>-100...</code>.", parse_mode="HTML")
        return

    await storage_manager.set_channel_id(new_id)
    text, keyboard = await get_channel_diagnostics(bot)
    await message.answer(f"✅ <b>Storage Channel ID set to <code>{new_id}</code>!</b>\n\n{text}", reply_markup=keyboard, parse_mode="HTML")


@router.message(F.forward_from_chat)
async def handle_forward_from_channel(message: Message, bot: Bot):
    """Auto-bind storage channel when admin forwards a post from that channel."""
    user_id = message.from_user.id if message.from_user else 0
    if user_id != settings.ADMIN_CHAT_ID and not await db.is_admin(user_id):
        return

    fwd = message.forward_from_chat
    if not fwd or fwd.type not in ("channel", "supergroup"):
        return

    new_id = fwd.id
    title = fwd.title or "Channel"
    await storage_manager.set_channel_id(new_id)
    text, keyboard = await get_channel_diagnostics(bot)
    await message.answer(
        f"🎉 <b>Detected Channel '{title}' (ID: <code>{new_id}</code>)!</b>\n\n{text}",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.message(Command("logs"))
async def handle_logs(message: Message):
    """Handle /logs [limit] command for admin."""
    user_id = message.from_user.id if message.from_user else 0
    if not await db.is_admin(user_id):
        await message.answer("⛔ Access denied. Admin only.")
        return

    from bot.main import memory_log_handler
    limit = 20
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        limit = min(max(int(args[1]), 5), 50)

    logs = list(memory_log_handler.buffer)[-limit:]
    if not logs:
        await message.answer("📭 No logs captured yet.")
        return

    log_text = "\n".join(logs)
    if len(log_text) > 3800:
        log_text = log_text[-3800:]
    escaped = log_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await message.answer(f"📋 <b>Recent Logs (last {len(logs)}):</b>\n<pre>{escaped}</pre>", parse_mode="HTML")


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

