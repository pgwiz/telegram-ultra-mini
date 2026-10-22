"""Playlist and album batch download handlers with interactive batch sizing and Smart Mode."""

import asyncio
import os
import logging
import time
import uuid
from typing import Optional, Dict, Any, List, Tuple
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from bot.utils.link_detector import extract_media_info
from bot.api_client import api_client
from bot.storage import storage_manager
from bot.downloader import downloader

logger = logging.getLogger(__name__)
router = Router(name="playlist")


class PlaylistSession:
    """In-memory state tracker for active playlist batch downloads."""

    def __init__(
        self,
        session_id: str,
        user_id: int,
        chat_id: int,
        platform: str,
        identifier: str,
        title: str,
        author: str,
        total_available: int,
        tracks: List[Dict[str, Any]]
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.chat_id = chat_id
        self.platform = platform
        self.identifier = identifier
        self.title = title
        self.author = author
        self.total_available = total_available
        self.tracks = tracks
        self.status = "menu"  # "menu", "running", "paused", "stopped", "completed"
        self.requested_count = min(25, len(tracks))
        self.smart_mode = False
        self.completed_count = 0
        self.failed_count = 0
        self.action_event = asyncio.Event()
        self.action_choice = ""  # "retry", "skip", "stop"
        self.created_at = time.time()

    @property
    def chat(self):
        """Compatibility property allowing session.chat.id access."""
        class _ChatProxy:
            def __init__(self, cid: int):
                self.id = cid
        return _ChatProxy(self.chat_id)


ACTIVE_SESSIONS: Dict[str, PlaylistSession] = {}


def cleanup_old_sessions() -> None:
    """Purge sessions older than 15 minutes (900s) or cap at 50 sessions to save RAM."""
    now = time.time()
    expired = [sid for sid, s in ACTIVE_SESSIONS.items() if now - s.created_at > 900]
    for sid in expired:
        s = ACTIVE_SESSIONS.pop(sid, None)
        if s and hasattr(s, "tracks"):
            s.tracks.clear()

    # If still > 50 sessions, evict oldest
    if len(ACTIVE_SESSIONS) > 50:
        sorted_sids = sorted(ACTIVE_SESSIONS.keys(), key=lambda k: ACTIVE_SESSIONS[k].created_at)
        for sid in sorted_sids[: len(ACTIVE_SESSIONS) - 50]:
            s = ACTIVE_SESSIONS.pop(sid, None)
            if s and hasattr(s, "tracks"):
                s.tracks.clear()


def build_batch_keyboard(session: PlaylistSession) -> InlineKeyboardMarkup:
    """Build interactive batch size selector with Bot API 9.4 semantic styles."""
    total = session.total_available
    buttons = []

    if total <= 25:
        buttons.append([
            InlineKeyboardButton(
                text=f"Download All ({total})",
                callback_data=f"pl:start:{session.session_id}:all:normal",
                style="success"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text="Smart Mode (Pause on Error)",
                callback_data=f"pl:start:{session.session_id}:all:smart",
                style="primary"
            )
        ])
    else:
        # Multiple batches available: 25, 50, 100, All, Smart Mode
        row1 = [
            InlineKeyboardButton(
                text="25 Tracks",
                callback_data=f"pl:start:{session.session_id}:25:normal",
                style="success"
            )
        ]
        if total > 25:
            row1.append(
                InlineKeyboardButton(
                    text="50 Tracks",
                    callback_data=f"pl:start:{session.session_id}:50:normal",
                    style="primary"
                )
            )
        buttons.append(row1)

        row2 = []
        if total > 50:
            row2.append(
                InlineKeyboardButton(
                    text="100 Tracks",
                    callback_data=f"pl:start:{session.session_id}:100:normal",
                    style="primary"
                )
            )
        row2.append(
            InlineKeyboardButton(
                text=f"All ({total})",
                callback_data=f"pl:start:{session.session_id}:all:normal",
                style="primary"
            )
        )
        buttons.append(row2)

        buttons.append([
            InlineKeyboardButton(
                text="Smart Mode (Pause on Error)",
                callback_data=f"pl:start:{session.session_id}:smart:smart",
                style="primary"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="Cancel",
            callback_data=f"pl:cancel:{session.session_id}",
            style="danger"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("playlist"))
async def handle_playlist_command(message: Message, bot: Bot):
    """Handle /playlist <url> command."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: <code>/playlist &lt;youtube or spotify playlist url&gt;</code>", parse_mode="HTML")
        return

    url = args[1].strip()
    platform, media_type, identifier = extract_media_info(url)
    if not identifier:
        await message.answer("❌ Could not recognize a valid playlist link.", parse_mode="HTML")
        return

    await start_playlist_flow(message, bot, platform, identifier)


async def start_playlist_flow(message: Message, bot: Bot, platform: str, identifier: str):
    """Inspect playlist metadata and present interactive batch size selection menu."""
    cleanup_old_sessions()
    status_msg = await message.answer("🔍 <i>Inspecting playlist tracks & metadata...</i>", parse_mode="HTML")

    try:
        info = await api_client.get_playlist_info(platform, identifier, limit=100)
        tracks = info.get("tracks") or []
        total_available = info.get("total_tracks") or len(tracks)

        if not tracks or total_available == 0:
            await status_msg.edit_text("❌ Could not retrieve tracks from this playlist. Please check the link accessibility.")
            return

        session_id = uuid.uuid4().hex[:8]
        user_id = message.from_user.id if message.from_user else message.chat.id
        title = info.get("title") or "Playlist"
        author = info.get("author") or "Various Artists"

        session = PlaylistSession(
            session_id=session_id,
            user_id=user_id,
            chat_id=message.chat.id,
            platform=platform,
            identifier=identifier,
            title=title,
            author=author,
            total_available=total_available,
            tracks=tracks
        )
        ACTIVE_SESSIONS[session_id] = session

        # Quick pre-scan for tracks already in database cache (throttled to 10 concurrent DB queries)
        db_sem = asyncio.Semaphore(10)

        async def _check_track(t):
            async with db_sem:
                try:
                    t_title = t.get("title") or t.get("name")
                    t_artist = t.get("artist") or t.get("subtitle")
                    t_vid = t.get("videoId")
                    cached = await storage_manager.find_cached(track_id=t_vid, title=t_title, artist=t_artist, quality="audio_high")
                    if cached:
                        t["cached_info"] = cached
                        return True
                except Exception:
                    pass
                return False

        check_results = await asyncio.gather(*[_check_track(t) for t in tracks], return_exceptions=True)
        existing_in_db = sum(1 for r in check_results if r is True)

        cached_line = f"📦 <b>Already in Library:</b> <code>{existing_in_db}</code> tracks\n" if existing_in_db > 0 else ""

        keyboard = build_batch_keyboard(session)
        await status_msg.edit_text(
            f"📋 <b>Playlist:</b> {title}\n"
            f"👤 <b>Curator:</b> {author}\n"
            f"🎵 <b>Tracks Available:</b> {total_available}\n"
            f"{cached_line}\n"
            f"<i>Choose how many tracks to download:</i>\n"
            f"💡 <i>Smart Mode pauses on any track failure and lets you Retry, Skip, or Stop.</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error starting playlist flow for {identifier}: {e}")
        await status_msg.edit_text("❌ An error occurred while inspecting the playlist.")


# ── Callback Handlers ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pl:start:"))
async def handle_start_playlist_batch(callback: CallbackQuery, bot: Bot):
    """Handle batch size and mode selection."""
    parts = callback.data.split(":")
    if len(parts) < 5:
        await callback.answer("Invalid session callback.", show_alert=True)
        return

    session_id = parts[2]
    limit_choice = parts[3]
    mode = parts[4]

    session = ACTIVE_SESSIONS.get(session_id)
    if not session:
        await callback.answer("⚠️ Session expired. Please send the playlist link again.", show_alert=True)
        return

    if session.status != "menu":
        await callback.answer("⏳ Batch is already processing.", show_alert=False)
        return

    if callback.from_user.id != session.user_id:
        await callback.answer("⚠️ This playlist menu belongs to another user.", show_alert=True)
        return

    # Determine requested limit
    if limit_choice == "all" or limit_choice == "smart":
        target_count = len(session.tracks)
    else:
        try:
            target_count = min(int(limit_choice), len(session.tracks))
        except ValueError:
            target_count = min(25, len(session.tracks))

    session.requested_count = target_count
    session.smart_mode = (mode == "smart")
    session.status = "running"
    session.tracks = session.tracks[:target_count]

    mode_label = "Smart Mode" if session.smart_mode else "Standard Batch"
    stop_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Stop Batch",
            callback_data=f"pl:stop:{session_id}",
            style="danger"
        )
    ]])

    await callback.message.edit_text(
        f"⏳ <b>Starting {mode_label}...</b>\n"
        f"📋 <i>{session.title}</i>\n"
        f"🎯 Preparing {target_count} tracks for processing...",
        reply_markup=stop_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

    # Launch batch execution in background task and attach task to session
    session.task = asyncio.create_task(run_playlist_download(session, bot, callback.message))


@router.callback_query(F.data.startswith("pl:act:"))
async def handle_smart_mode_action(callback: CallbackQuery):
    """Handle Smart Mode user choice on track failure: retry, skip, stop."""
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer()
        return

    session_id = parts[2]
    action = parts[3]

    session = ACTIVE_SESSIONS.get(session_id)
    if not session:
        await callback.answer("⚠️ Session expired.", show_alert=True)
        return

    if callback.from_user.id != session.user_id:
        await callback.answer("⚠️ Not your session.", show_alert=True)
        return

    session.action_choice = action
    session.action_event.set()
    await callback.answer(f"Action: {action.capitalize()}")


@router.callback_query(F.data.startswith("pl:stop:"))
async def handle_stop_playlist(callback: CallbackQuery):
    """Handle explicit stop button from user."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    session_id = parts[2]
    session = ACTIVE_SESSIONS.get(session_id)
    if not session:
        await callback.answer("Session already finished.")
        return

    if callback.from_user.id != session.user_id:
        await callback.answer("⚠️ Not your session.", show_alert=True)
        return

    session.status = "stopped"
    session.action_choice = "stop"
    session.action_event.set()
    await callback.answer("🛑 Stopping batch download...")


@router.callback_query(F.data.startswith("pl:cancel:"))
async def handle_cancel_playlist(callback: CallbackQuery):
    """Cancel playlist before starting and immediately free session track memory."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    session_id = parts[2]
    session = ACTIVE_SESSIONS.pop(session_id, None)
    if session:
        try:
            session.tracks.clear()
        except Exception:
            pass
        if callback.from_user.id != session.user_id:
            await callback.answer("⚠️ Not your session.", show_alert=True)
            return

    await callback.message.edit_text("❌ <i>Playlist download cancelled.</i>", parse_mode="HTML")
    await callback.answer("Cancelled")


# ── Batch Download Execution Loop ─────────────────────────────────────────────

async def run_playlist_download(session: PlaylistSession, bot: Bot, status_msg: Message):
    """Execute batch download loop: deliver library tracks first, then download only missing songs."""
    total_tracks = len(session.tracks)
    session.status_msg = status_msg
    stop_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Stop Batch",
            callback_data=f"pl:stop:{session.session_id}",
            style="danger"
        )
    ]])

    try:
        # 1. Pre-scan batch for existing tracks in database cache
        cached_tracks: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        new_tracks: List[Dict[str, Any]] = []

        for track in session.tracks:
            cached = track.get("cached_info")
            if not cached:
                t_title = track.get("title") or track.get("name")
                t_artist = track.get("artist") or track.get("subtitle")
                t_vid = track.get("videoId")
                cached = await storage_manager.find_cached(
                    track_id=t_vid,
                    title=t_title,
                    artist=t_artist,
                    quality="audio_high"
                )
            if cached:
                cached_tracks.append((track, cached))
            else:
                new_tracks.append(track)

        cached_count = len(cached_tracks)
        to_download_count = len(new_tracks)

        # 2. Show analysis breakdown upfront
        next_step_hint = "⚡ <i>Delivering library songs first...</i>" if cached_count > 0 else "📥 <i>Starting downloads...</i>"
        try:
            await status_msg.edit_text(
                f"📋 <b>{session.title}</b>\n\n"
                f"📊 <b>Batch Analysis ({total_tracks} tracks):</b>\n"
                f"📦 <b>Already in Library:</b> <code>{cached_count}</code> tracks (instant delivery)\n"
                f"📥 <b>New to Download:</b> <code>{to_download_count}</code> tracks\n\n"
                f"{next_step_hint}",
                reply_markup=stop_keyboard,
                parse_mode="HTML"
            )
        except Exception:
            pass
        await asyncio.sleep(2.0)

        # 3. Deliver existing tracks from library with zero re-downloading
        library_delivered = 0
        for c_idx, (track, cached) in enumerate(cached_tracks, 1):
            if session.status == "stopped":
                break

            title = track.get("title") or track.get("name") or f"Track {c_idx}"
            artist = track.get("artist") or track.get("subtitle") or "Unknown Artist"
            msg_id = cached.get("channel_msg_id")
            track_id = cached.get("track_id") or track.get("videoId") or f"cached_{c_idx}"

            try:
                await status_msg.edit_text(
                    f"⚡ <b>Delivering from Library [{c_idx}/{cached_count}]:</b>\n"
                    f"🎵 <b>{title}</b>\n"
                    f"👤 <i>{artist}</i>",
                    reply_markup=stop_keyboard,
                    parse_mode="HTML"
                )
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass

            delivered = False
            if msg_id:
                delivered = await storage_manager.deliver_cached_msg(
                    bot=bot,
                    user_chat_id=session.chat_id,
                    channel_msg_id=msg_id,
                    track_id=track_id,
                    title=title,
                    quality="audio_high"
                )

            if delivered:
                session.completed_count += 1
                library_delivered += 1
            else:
                # If cached message could not be delivered, queue for fresh download
                new_tracks.append(track)

            await asyncio.sleep(1.5)

        # If all tracks were in library, complete immediately
        if not new_tracks:
            session.status = "completed"
            await status_msg.edit_text(
                f"🎉 <b>Playlist Complete!</b>\n\n"
                f"📦 All <b>{library_delivered}</b> tracks were already in your library and delivered instantly!\n"
                f"🚀 0 new downloads required.",
                parse_mode="HTML"
            )
            return

        if session.status == "stopped":
            new_downloaded = session.completed_count - library_delivered
            await status_msg.edit_text(
                f"🛑 <b>Playlist batch stopped by user.</b>\n"
                f"Delivered: <b>{session.completed_count}/{total_tracks}</b> tracks.\n"
                f"📦 From Library: <b>{library_delivered}</b> | 📥 Downloaded: <b>{new_downloaded}</b>",
                parse_mode="HTML"
            )
            return

        # 4. Download remaining tracks that do not exist in library
        new_total = len(new_tracks)
        try:
            await status_msg.edit_text(
                f"📥 <b>Library delivery complete ({library_delivered} tracks).</b>\n"
                f"Starting downloads for remaining <b>{new_total}</b> new tracks...",
                reply_markup=stop_keyboard,
                parse_mode="HTML"
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # 4. Pipelined download & upload loop (download next while uploading previous to channel)
        async def _fetch_single_track(trk: Dict[str, Any]) -> Dict[str, Any]:
            """Helper to resolve metadata and download a track, or verify channel cache."""
            t_title = trk.get("title") or trk.get("name") or "Track"
            t_artist = trk.get("artist") or trk.get("subtitle") or "Unknown Artist"
            vid = trk.get("videoId")

            # Resolve missing videoId for Spotify tracks if needed
            if not vid:
                try:
                    q = f"{t_title} {t_artist}"
                    s_res = await api_client.search_tracks(q, limit=1)
                    if s_res and s_res[0].get("videoId"):
                        vid = s_res[0]["videoId"]
                        trk["videoId"] = vid
                except Exception as res_err:
                    logger.debug(f"Could not resolve videoId for '{t_title}': {res_err}")

            # Check instant storage cache before downloading
            if vid:
                cached_item = await storage_manager.find_cached(track_id=vid, quality="audio_high")
                if cached_item and cached_item.get("channel_msg_id"):
                    return {
                        "track": trk,
                        "status": "cached",
                        "cached": cached_item,
                        "title": t_title,
                        "artist": t_artist,
                        "videoId": vid
                    }

            # Download track to local temp file (with 120s timeout guard)
            target = vid or trk.get("url") or f"{t_title} {t_artist}"
            try:
                f_path, th_path, m_data = await asyncio.wait_for(
                    downloader.download_track(target, quality="audio_high"),
                    timeout=120.0
                )
                if f_path and os.path.exists(f_path):
                    return {
                        "track": trk,
                        "status": "downloaded",
                        "file_path": f_path,
                        "thumb_path": th_path,
                        "meta": m_data,
                        "cached_id": vid or target,
                        "title": t_title,
                        "artist": t_artist,
                        "duration": m_data.get("duration") or trk.get("duration")
                    }
            except asyncio.TimeoutError:
                logger.warning(f"Track download timed out (120s) for {t_title} - {t_artist}")
            except Exception as dl_err:
                logger.warning(f"Track download error for {t_title} - {t_artist}: {dl_err}")

            return {
                "track": trk,
                "status": "failed",
                "title": t_title,
                "artist": t_artist
            }

        # Pipeline: Pre-fetch track 1 immediately
        current_fetch_task: Optional[asyncio.Task] = None
        if new_tracks:
            current_fetch_task = asyncio.create_task(_fetch_single_track(new_tracks[0]))

        for idx, track in enumerate(new_tracks, 1):
            if session.status == "stopped":
                if current_fetch_task and not current_fetch_task.done():
                    current_fetch_task.cancel()
                break

            title = track.get("title") or track.get("name") or f"Track {idx}"
            artist = track.get("artist") or track.get("subtitle") or "Unknown Artist"
            mode_tag = " (Smart Mode)" if session.smart_mode else ""

            # Update status message: downloading current track
            try:
                await status_msg.edit_text(
                    f"📥 <b>Processing [{idx}/{new_total}]{mode_tag}:</b>\n"
                    f"🎵 <b>{title}</b>\n"
                    f"👤 <i>{artist}</i>",
                    reply_markup=stop_keyboard,
                    parse_mode="HTML"
                )
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass

            # Await the in-flight download of the current track
            if current_fetch_task:
                try:
                    fetch_res = await current_fetch_task
                except Exception as e:
                    fetch_res = {"status": "failed", "title": title, "artist": artist}
            else:
                fetch_res = await _fetch_single_track(track)

            # PIPELINING: If there is a next track, launch its download in background NOW!
            next_fetch_task: Optional[asyncio.Task] = None
            if idx < new_total and session.status != "stopped":
                next_fetch_task = asyncio.create_task(_fetch_single_track(new_tracks[idx]))

            track_succeeded = False

            if fetch_res.get("status") == "cached":
                # Instant delivery from storage channel cache (0 bytes disk)
                cached_doc = fetch_res["cached"]
                delivered = await storage_manager.deliver_cached_msg(
                    bot=bot,
                    user_chat_id=session.chat_id,
                    channel_msg_id=cached_doc["channel_msg_id"],
                    track_id=fetch_res.get("videoId") or f"cached_{idx}",
                    title=title,
                    quality="audio_high"
                )
                if delivered:
                    session.completed_count += 1
                    track_succeeded = True

            elif fetch_res.get("status") == "downloaded":
                f_path = fetch_res["file_path"]
                th_path = fetch_res.get("thumb_path")
                c_id = fetch_res.get("cached_id") or f"track_{idx}"
                dur = fetch_res.get("duration")

                try:
                    # Update status: uploading to channel while next track downloads in parallel
                    try:
                        next_title = new_tracks[idx].get("title") or new_tracks[idx].get("name") if idx < new_total else ""
                        next_hint = f"\n⚡ <i>(Downloading next track in parallel...)</i>" if next_title else ""
                        await status_msg.edit_text(
                            f"📤 <b>Uploading to channel & delivering [{idx}/{new_total}]{mode_tag}:</b>\n"
                            f"🎵 <b>{title}</b>\n"
                            f"👤 <i>{artist}</i>{next_hint}",
                            reply_markup=stop_keyboard,
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                    # Upload to storage channel -> purge local file instantly -> copy message to user
                    success = await storage_manager.upload_and_cache(
                        bot=bot,
                        user_chat_id=session.chat_id,
                        file_path=f_path,
                        track_id=c_id,
                        quality="audio_high",
                        title=title,
                        artist=artist,
                        duration=dur,
                        thumbnail_path=th_path,
                        source="api_playlist"
                    )
                    if success:
                        session.completed_count += 1
                        track_succeeded = True
                finally:
                    # Guarantee local file is immediately wiped from disk
                    try:
                        if f_path and os.path.exists(f_path):
                            os.remove(f_path)
                        if th_path and os.path.exists(th_path):
                            os.remove(th_path)
                    except Exception:
                        pass

            # Smart Mode Retry / Pause if failed
            if not track_succeeded:
                logger.warning(f"Track failed [{idx}/{new_total}]: {title} - {artist}")
                if session.smart_mode:
                    if next_fetch_task and not next_fetch_task.done():
                        next_fetch_task.cancel()
                        next_fetch_task = None

                    session.status = "paused"
                    session.action_event.clear()
                    session.action_choice = ""

                    action_buttons = [
                        [
                            InlineKeyboardButton(text="Retry Track", callback_data=f"pl:act:{session.session_id}:retry", style="success"),
                            InlineKeyboardButton(text="Skip Track", callback_data=f"pl:act:{session.session_id}:skip", style="primary")
                        ],
                        [
                            InlineKeyboardButton(text="Stop Batch", callback_data=f"pl:act:{session.session_id}:stop", style="danger")
                        ]
                    ]

                    await status_msg.edit_text(
                        f"⚠️ <b>Track Failed [{idx}/{new_total}]:</b>\n"
                        f"🎵 <b>{title}</b>\n"
                        f"👤 <i>{artist}</i>\n\n"
                        f"<i>Download was unsuccessful. What would you like to do?</i>",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=action_buttons),
                        parse_mode="HTML"
                    )

                    try:
                        await asyncio.wait_for(session.action_event.wait(), timeout=600.0)
                    except asyncio.TimeoutError:
                        session.action_choice = "skip"

                    if session.action_choice == "retry":
                        session.status = "running"
                        retry_res = await _fetch_single_track(track)
                        if retry_res.get("status") == "downloaded":
                            r_path = retry_res["file_path"]
                            r_th = retry_res.get("thumb_path")
                            try:
                                r_succ = await storage_manager.upload_and_cache(
                                    bot=bot,
                                    user_chat_id=session.chat_id,
                                    file_path=r_path,
                                    track_id=retry_res.get("cached_id") or f"track_{idx}",
                                    quality="audio_high",
                                    title=title,
                                    artist=artist,
                                    duration=retry_res.get("duration"),
                                    thumbnail_path=r_th,
                                    source="api_playlist"
                                )
                                if r_succ:
                                    session.completed_count += 1
                                    track_succeeded = True
                            finally:
                                try:
                                    if r_path and os.path.exists(r_path):
                                        os.remove(r_path)
                                    if r_th and os.path.exists(r_th):
                                        os.remove(r_th)
                                except Exception:
                                    pass

                    elif session.action_choice == "stop":
                        session.status = "stopped"
                        break
                    else:
                        session.status = "running"

                if not track_succeeded:
                    session.failed_count += 1

            if session.status == "stopped":
                if next_fetch_task and not next_fetch_task.done():
                    next_fetch_task.cancel()
                elif next_fetch_task and next_fetch_task.done():
                    try:
                        res = next_fetch_task.result()
                        if isinstance(res, dict):
                            f_p = res.get("file_path")
                            th_p = res.get("thumb_path")
                            if f_p and os.path.exists(f_p):
                                os.remove(f_p)
                            if th_p and os.path.exists(th_p):
                                os.remove(th_p)
                    except Exception:
                        pass
                break

            # Advance pipeline: current task becomes next task
            current_fetch_task = next_fetch_task

            # Brief sleep to avoid Telegram rate limits
            await asyncio.sleep(1.0)

        # Final completion report
        new_downloaded = session.completed_count - library_delivered
        if session.status == "stopped":
            await status_msg.edit_text(
                f"🛑 <b>Playlist batch stopped by user.</b>\n"
                f"Delivered: <b>{session.completed_count}/{total_tracks}</b> tracks.\n"
                f"📦 From Library: <b>{library_delivered}</b> | 📥 Downloaded: <b>{new_downloaded}</b>\n"
                f"Skipped / Failed: <b>{session.failed_count}</b> tracks.",
                parse_mode="HTML"
            )
        else:
            session.status = "completed"
            await status_msg.edit_text(
                f"✅ <b>Playlist completed!</b>\n"
                f"Delivered: <b>{session.completed_count}/{total_tracks}</b> tracks.\n"
                f"📦 From Library: <b>{library_delivered}</b> | 📥 Downloaded: <b>{new_downloaded}</b>\n"
                f"{f'⚠️ Skipped: <b>{session.failed_count}</b> tracks.' if session.failed_count > 0 else '🎉 All tracks delivered successfully!'}",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Error during playlist batch download for session {session.session_id}: {e}", exc_info=True)
        try:
            err_msg = str(e).strip() or type(e).__name__
            if len(err_msg) > 120:
                err_msg = err_msg[:120] + "..."
            await status_msg.edit_text(
                f"❌ <b>An error occurred during playlist processing:</b>\n<code>{err_msg}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        try:
            session.tracks.clear()
        except Exception:
            pass
        ACTIVE_SESSIONS.pop(session.session_id, None)
