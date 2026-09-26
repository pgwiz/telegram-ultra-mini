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
    """Handle /help command."""
    help_text = (
        "📖 <b>Telegram Ultra Mini - Help</b>\n\n"
        "<b>Direct Link Pasting:</b>\n"
        "Simply send a link from YouTube (video, short, playlist) or Spotify (track, album, playlist). "
        "The bot will detect it automatically.\n\n"
        "<b>Quality Options (/da):</b>\n"
        "• <code>audio_high</code> - 320k / 256k AAC high fidelity\n"
        "• <code>audio</code> - 192k standard quality\n"
        "• <code>saver</code> - 64k mobile data saver\n\n"
        "<b>Search:</b>\n"
        "Type <code>/search &lt;song title or artist&gt;</code> to browse and pick a track.\n\n"
        "<b>Speed & Caching:</b>\n"
        "All songs are backed by a private storage channel. When you request a previously downloaded song, "
        "it delivers in milliseconds!"
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("ping"))
async def handle_ping(message: Message):
    """Handle /ping health check."""
    start_time = time.perf_counter()
    msg = await message.answer("🏓 <i>Pinging...</i>", parse_mode="HTML")
    latency_ms = (time.perf_counter() - start_time) * 1000

    db_status = "Connected 🟢" if db.pool else "Disconnected 🔴"
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
