"""Direct download and link detection handlers."""

import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from bot.utils.link_detector import extract_media_info
from bot.storage import storage_manager
from bot.downloader import downloader
from bot.database import db

logger = logging.getLogger(__name__)
router = Router(name="download")


def parse_force_arg(raw_text: str) -> tuple[str, bool]:
    """Check if 'force' or '-f' is passed, returning (cleaned_url, is_forced)."""
    parts = raw_text.strip().split()
    is_forced = False
    clean_parts = []
    for p in parts:
        if p.lower() in ("force", "-f", "--force"):
            is_forced = True
        else:
            clean_parts.append(p)
    return " ".join(clean_parts).strip(), is_forced


def make_quality_keyboard(platform: str, identifier: str, force: bool = False) -> InlineKeyboardMarkup:
    """Build quality selection inline keyboard with Audio and Video presets."""
    suffix = ":force" if force else ""
    force_tag = "⚡ " if force else ""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{force_tag}🎵 High (320k)",
                callback_data=f"dl:{platform}:{identifier}:audio_high{suffix}"
            ),
            InlineKeyboardButton(
                text=f"{force_tag}🎶 Normal (192k)",
                callback_data=f"dl:{platform}:{identifier}:audio{suffix}"
            ),
            InlineKeyboardButton(
                text=f"{force_tag}💾 Saver (64k)",
                callback_data=f"dl:{platform}:{identifier}:saver{suffix}"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"{force_tag}🎬 Video HD (720p)",
                callback_data=f"dl:{platform}:{identifier}:720p{suffix}"
            ),
            InlineKeyboardButton(
                text=f"{force_tag}🎬 Video SD (360p)",
                callback_data=f"dl:{platform}:{identifier}:360p{suffix}"
            )
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("da"))
async def handle_da_command(message: Message):
    """Handle /da <url> [force] with interactive quality selection (Audio & Video)."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/da &lt;youtube or spotify url&gt; [force]</code>", parse_mode="HTML")
        return

    raw_input = args[1].strip()
    url, is_forced = parse_force_arg(raw_input)
    platform, media_type, identifier = extract_media_info(url)
    if not identifier:
        await message.answer("❌ Could not recognize a valid YouTube or Spotify link.", parse_mode="HTML")
        return

    if media_type == "playlist":
        await message.answer(f"ℹ️ For playlists, use <code>/playlist {url}</code>", parse_mode="HTML")
        return

    keyboard = make_quality_keyboard(platform, identifier, force=is_forced)
    force_note = " <i>(⚡ Force Re-download)</i>" if is_forced else ""
    await message.answer(f"🎧 <b>Select Format & Quality:</b>{force_note}", reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("video", "dv"))
async def handle_video_command(message: Message, bot: Bot):
    """Handle /video <url> [force] or /dv <url> [force] for direct 720p MP4 video download."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/video &lt;youtube url&gt; [force]</code>", parse_mode="HTML")
        return

    url, is_forced = parse_force_arg(args[1].strip())
    await process_download(message, bot, url, quality="720p", force=is_forced)


@router.message(Command("download"))
async def handle_download_command(message: Message, bot: Bot):
    """Handle /download <url> [force] with default high quality audio."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/download &lt;youtube or spotify url&gt; [force]</code>", parse_mode="HTML")
        return

    url, is_forced = parse_force_arg(args[1].strip())
    await process_download(message, bot, url, force=is_forced)


@router.message(F.text)
async def handle_direct_link(message: Message, bot: Bot):
    """Auto-detect YouTube/Spotify links (with optional force), or fallback to search for plain text."""
    text = message.text.strip()
    if text.startswith("/"):
        return

    clean_text, is_forced = parse_force_arg(text)
    platform, media_type, identifier = extract_media_info(clean_text)
    if identifier:
        if media_type == "playlist":
            from bot.handlers.playlist import process_playlist
            await process_playlist(message, bot, platform, identifier)
            return

        await process_download(message, bot, clean_text, platform=platform, identifier=identifier, force=is_forced)
        return

    # Plain text without recognized links: automatically execute search
    from bot.handlers.search import execute_search
    await execute_search(message, text)


async def process_download(
    message: Message,
    bot: Bot,
    url_or_id: str,
    platform: str = None,
    identifier: str = None,
    quality: str = "audio_high",
    force: bool = False
):
    """Core download routine with channel caching check and force re-download support."""
    user_id = message.from_user.id if message.from_user else message.chat.id
    
    # 1. Rate limit check
    if not await db.check_rate_limit(user_id, "download", limit=15, window_secs=60):
        await message.answer("⚠️ <b>Rate limit exceeded.</b> Please wait a minute before downloading again.", parse_mode="HTML")
        return

    if not identifier or not platform:
        p, m, ident = extract_media_info(url_or_id)
        if not ident:
            await message.answer("❌ Invalid URL or track ID.", parse_mode="HTML")
            return
        platform, identifier = p, ident

    # 2. Check storage channel cache in Neon DB (bypassed if force=True)
    if not force:
        cached = await storage_manager.deliver_cached(
            bot=bot,
            user_chat_id=message.chat.id,
            track_id=identifier,
            quality=quality
        )
        if cached:
            return
    else:
        logger.info(f"Force re-download requested for track {identifier} ({quality}), bypassing storage cache.")

    # 3. Cache miss or forced re-download: download from API
    status_label = "⚡ <i>Force re-downloading fresh media...</i>" if force else "⏳ <i>Extracting stream & audio...</i>"
    status_msg = await message.answer(status_label, parse_mode="HTML")

    file_path, thumb_path, meta = await downloader.download_track(identifier, quality=quality)
    if not file_path:
        await status_msg.edit_text("❌ <b>Download failed.</b> Please check if the link is accessible.")
        return

    # 4. Upload to storage channel & copy to user
    await status_msg.edit_text("⚡ <i>Delivering from cloud storage...</i>", parse_mode="HTML")
    success = await storage_manager.upload_and_cache(
        bot=bot,
        user_chat_id=message.chat.id,
        file_path=file_path,
        track_id=identifier,
        quality=quality,
        title=meta.get("title", "Audio Track"),
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
        await message.answer("⚠️ Could not deliver track. Please try again.")
