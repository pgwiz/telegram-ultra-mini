"""Start, help, ping, and user info handlers."""

import time
import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from bot.database import db
from bot.cache import cache

logger = logging.getLogger(__name__)
router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message):
    """Handle /start command."""
    user = message.from_user
    if user:
        await db.upsert_user(user.id, user.username, user.first_name)

    welcome_text = (
        "🚀 <b>Welcome to Telegram Ultra Mini!</b>\n\n"
        "I am an ultra-fast, lightweight music & stream downloader powered by high-speed APIs "
        "and persistent cloud channel storage.\n\n"
        "⚡ <b>Instant Delivery:</b> Cached tracks are delivered immediately without re-downloading!\n\n"
        "<b>Available Commands:</b>\n"
        "• <code>/download &lt;url&gt;</code> - Download audio from YouTube or Spotify\n"
        "• <code>/da &lt;url&gt;</code> - Choose audio quality (320k, 192k, 64k)\n"
        "• <code>/search &lt;query&gt;</code> - Search tracks with 1-click download\n"
        "• <code>/playlist &lt;url&gt;</code> - Batch download playlist tracks\n"
        "• <code>/history</code> - View your recent downloads\n"
        "• <code>/ping</code> - Check bot health & latency\n"
        "• <code>/chatid</code> - Show your Telegram Chat ID\n\n"
        "💡 <i>Tip: You can paste any YouTube or Spotify link directly into this chat!</i>"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@router.message(Command("help"))
async def handle_help(message: Message):
    """Handle /help command with dynamic admin control section."""
    user_id = message.from_user.id if message.from_user else 0
    is_adm = await db.is_admin(user_id)

    sections = [
        "📖 <b>Telegram Ultra Mini - Help</b>\n",
        "<b>🎵 Direct Links:</b>\n"
        "Send any YouTube or Spotify link directly. The bot detects it automatically.\n"
        "Add <code>force</code> to re-download fresh (e.g. <code>https://... force</code>).\n",
        "<b>⚡ Commands:</b>\n"
        "• <code>/search &lt;query&gt;</code> - Browse songs with interactive format selection\n"
        "• <code>/download &lt;url&gt; [force]</code> - Download default high quality audio\n"
        "• <code>/video &lt;url&gt; [force]</code> or <code>/dv</code> - Download 720p HD MP4 video\n"
        "• <code>/da &lt;url&gt; [force]</code> - Choose format & quality (320k, 192k, 64k, 720p, 360p)\n"
        "• <code>/playlist &lt;url&gt;</code> - Batch download playlist tracks\n"
        "• <code>/history</code> - View your recent downloads\n"
        "• <code>/ping</code> - Check bot latency and database connection\n"
        "• <code>/chatid</code> - Show your Telegram Chat ID"
    ]

    if is_adm:
        sections.append(
            "\n👑 <b>Admin Control Panel:</b>\n"
            "• <code>/delete &lt;#track_id&gt;</code> (or <code>/del</code>) - Purge track from DB & storage channel\n"
            "• <code>/cleanup</code> - Purge local temp files, expired DB cache & flush RAM\n"
            "• <code>/admin</code> - Interactive Admin Control Panel dashboard\n"
            "• <code>/stats</code> - Real-time system, DB, storage & cache stats\n"
            "• <code>/cache_clear</code> - Reset in-memory RAM cache\n"
            "<i>(Tip: You can reply to any song message with <code>/delete</code> to purge it)</i>"
        )

    await message.answer("\n".join(sections), parse_mode="HTML")


@router.message(Command("ping"))
async def handle_ping(message: Message):
    """Handle /ping health check."""
    start_time = time.perf_counter()
    msg = await message.answer("🏓 <i>Pinging...</i>", parse_mode="HTML")
    latency_ms = (time.perf_counter() - start_time) * 1000

    db_status = "Connected 🟢" if db.is_connected else "Disconnected 🔴"
    ram_cache_size = len(cache._ram_cache)

    reply_text = (
        "🏓 <b>Pong!</b>\n\n"
        f"• <b>Bot Latency:</b> <code>{latency_ms:.1f}ms</code>\n"
        f"• <b>Neon Database:</b> {db_status}\n"
        f"• <b>In-Memory Cache:</b> <code>{ram_cache_size} items</code>"
    )
    await msg.edit_text(reply_text, parse_mode="HTML")


@router.message(Command("chatid"))
async def handle_chatid(message: Message):
    """Return user's chat ID."""
    await message.answer(f"🆔 Your Telegram Chat ID: <code>{message.chat.id}</code>", parse_mode="HTML")
