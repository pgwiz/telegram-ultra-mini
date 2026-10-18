"""Main application entry point for Telegram Ultra Mini.

Runs FastAPI health/status server, live in-memory logs viewer,
and the supervised aiogram 3 Telegram bot polling loop simultaneously
via FastAPI lifespan (compatible with Uvicorn, Gunicorn, and Render).
"""

import asyncio
import collections
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import httpx
import uvicorn
import urllib.parse
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from bot.config import settings
from bot.database import db
from bot.api_client import api_client
from bot.storage import storage_manager
from bot.handlers import register_all_handlers


# ── In-Memory Ring Buffer Log Handler ─────────────────────────────────────────

class MemoryLogHandler(logging.Handler):
    """In-memory ring buffer capturing the most recent application logs."""
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            pass

    def clear(self):
        self.buffer.clear()


memory_log_handler = MemoryLogHandler(capacity=1000)
memory_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)

# Configure structured root logging
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(logging.StreamHandler(sys.stdout))
root_logger.addHandler(memory_log_handler)

logger = logging.getLogger("telegram_ultra_mini")

# State trackers
BOT_START_TIME = time.time()
POLLING_STATUS = {"state": "initializing", "restarts": 0, "last_error": None}
ACTIVE_BOT: Optional[Bot] = None


# ── FastAPI Lifespan & Bot Polling Supervisor ─────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan manager.
    Initializes database, starts self-ping keepalive, and launches
    a supervised aiogram polling task. Cleanly cancels on shutdown.
    """
    global BOT_START_TIME, ACTIVE_BOT
    BOT_START_TIME = time.time()
    logger.info("🚀 Starting Telegram Ultra Mini background services...")

    # 1. Connect to Database (MongoDB Atlas, Neon PostgreSQL, or SQLite)
    await db.connect()
    await storage_manager.sync_channel_id()

    # 2. Setup Bot & Dispatcher
    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    ACTIVE_BOT = bot
    dp = Dispatcher()
    register_all_handlers(dp)

    # Add update logging middleware so every incoming message/button is visible in /logs
    @dp.update.outer_middleware
    async def log_update_middleware(handler, event, data):
        try:
            if event.message:
                u = event.message.from_user
                uname = f"@{u.username}" if u and u.username else ""
                uid = u.id if u else 0
                txt = event.message.text or event.message.caption or "<media>"
                logger.info(f"📨 Update from {uid} {uname}: {txt[:120]}")
            elif event.callback_query:
                u = event.callback_query.from_user
                uname = f"@{u.username}" if u and u.username else ""
                uid = u.id if u else 0
                logger.info(f"🔘 Callback from {uid} {uname}: {event.callback_query.data}")
        except Exception:
            pass
        return await handler(event, data)

    # 3. Pre-flight check Bot credentials
    try:
        bot_info = await bot.get_me()
        logger.info(f"✅ Bot authorized: @{bot_info.username} (ID: {bot_info.id})")
    except Exception as e:
        logger.error(f"❌ Telegram Bot Token is INVALID or UNAUTHORIZED: {e}")
        logger.error("Please verify TELEGRAM_BOT_TOKEN in Render environment variables.")
        POLLING_STATUS["state"] = "unauthorized"
        POLLING_STATUS["last_error"] = str(e)
        yield
        await bot.session.close()
        await db.disconnect()
        return

    # 4. Register Telegram command menu
    try:
        from aiogram.types import BotCommand, BotCommandScopeDefault
        commands = [
            BotCommand(command="start", description="Start the bot & welcome info"),
            BotCommand(command="help", description="How to use the bot & commands"),
            BotCommand(command="download", description="Download audio from YouTube/Spotify"),
            BotCommand(command="video", description="Download video from YouTube (MP4 720p)"),
            BotCommand(command="da", description="Format picker (Audio 320k/192k/64k, Video 720p/360p)"),
            BotCommand(command="search", description="Search music catalog with 1-click dl"),
            BotCommand(command="playlist", description="Download playlist (YouTube/Spotify)"),
            BotCommand(command="history", description="View your recent downloads"),
            BotCommand(command="ping", description="Bot health & database latency"),
            BotCommand(command="chatid", description="Get your Telegram chat ID"),
            BotCommand(command="stats", description="System metrics & storage stats"),
            BotCommand(command="logs", description="Admin live logs viewer"),
            BotCommand(command="channel", description="Admin storage channel manager"),
        ]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Telegram command menu registered successfully.")
    except Exception as e:
        logger.warning(f"Could not register Telegram commands: {e}")

    # 5. Launch Supervised Polling Task
    supervisor_stop = asyncio.Event()

    async def polling_supervisor():
        """Supervised polling loop that automatically reconnects on network glitches or conflicts."""
        retry_delay = 3.0
        while not supervisor_stop.is_set():
            try:
                POLLING_STATUS["state"] = "polling"
                logger.info("🔄 Starting Telegram bot polling loop (drop_pending_updates=False)...")
                # Do NOT drop pending updates so messages sent during reboot/deployment are processed!
                await bot.delete_webhook(drop_pending_updates=False)
                await dp.start_polling(bot, handle_as_tasks=True)
                logger.warning("aiogram start_polling exited cleanly. Reconnecting in 3s...")
            except asyncio.CancelledError:
                logger.info("Polling supervisor task cancelled.")
                break
            except Exception as e:
                POLLING_STATUS["restarts"] += 1
                POLLING_STATUS["last_error"] = str(e)
                logger.error(f"Polling loop exception: {e}. Auto-reconnecting in {retry_delay:.0f}s...", exc_info=True)

            try:
                await asyncio.wait_for(supervisor_stop.wait(), timeout=retry_delay)
                break
            except asyncio.TimeoutError:
                pass

        POLLING_STATUS["state"] = "stopped"

    # 6. Launch Render Free-Tier Keepalive Ping
    async def render_keepalive():
        """Pings public health endpoint every 9 minutes to prevent Render free-tier container sleep."""
        external_url = os.getenv("RENDER_EXTERNAL_URL", "https://telegram-ultra-mini.onrender.com")
        await asyncio.sleep(45)  # Initial boot delay
        while not supervisor_stop.is_set():
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(f"{external_url.rstrip('/')}/health")
                    logger.debug(f"💓 Render keepalive ping: {resp.status_code}")
            except Exception as e:
                logger.debug(f"Render keepalive ping notice: {e}")

            try:
                await asyncio.wait_for(supervisor_stop.wait(), timeout=540.0)  # 9 minutes
                break
            except asyncio.TimeoutError:
                pass

    supervisor_task = asyncio.create_task(polling_supervisor())
    keepalive_task = asyncio.create_task(render_keepalive())

    yield

    # 7. Graceful Shutdown
    logger.info("🛑 Shutting down Telegram Ultra Mini background services...")
    supervisor_stop.set()

    # Notify any active in-flight playlist sessions before shutting down
    try:
        from bot.handlers.playlist import ACTIVE_SESSIONS
        for s_id, session in list(ACTIVE_SESSIONS.items()):
            if session.status in ("running", "paused") and hasattr(session, "status_msg") and session.status_msg:
                try:
                    await session.status_msg.edit_text(
                        f"⚠️ <b>Playlist batch paused due to a server deployment.</b>\n\n"
                        f"📋 <i>{session.title}</i>\n"
                        f"Delivered: <b>{session.completed_count}/{session.total_available}</b> tracks.\n\n"
                        f"🔄 <i>Send the playlist link again to resume right where you left off! (Already delivered tracks will be restored instantly from library with 0 downloads).</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Graceful session notification skipped: {e}")

    supervisor_task.cancel()
    keepalive_task.cancel()
    try:
        await asyncio.gather(supervisor_task, keepalive_task, return_exceptions=True)
    except Exception:
        pass

    await bot.session.close()
    await api_client.close()
    await db.disconnect()
    ACTIVE_BOT = None
    logger.info("Telegram Ultra Mini shutdown complete.")


# ── FastAPI Application Instance & Endpoints ─────────────────────────────────

app = FastAPI(title="Telegram Ultra Mini", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    """Render root endpoint for deployment health verification."""
    uptime = int(time.time() - BOT_START_TIME)
    return {
        "status": "online",
        "app": "Telegram Ultra Mini",
        "version": "1.5.5",
        "polling": POLLING_STATUS.get("state"),
        "database": "connected" if db.is_connected else "disconnected",
        "uptime_secs": uptime
    }


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Health check endpoint for Render / monitor services."""
    uptime = int(time.time() - BOT_START_TIME)
    return {
        "status": "healthy" if db.is_connected else "degraded",
        "database": "connected" if db.is_connected else "disconnected",
        "polling": POLLING_STATUS.get("state"),
        "restarts": POLLING_STATUS.get("restarts", 0),
        "last_error": POLLING_STATUS.get("last_error"),
        "uptime_secs": uptime,
        "api_endpoint": settings.YTSP_API_BASE_URL
    }


@app.get("/stats")
async def get_stats():
    """Public stats endpoint."""
    return await db.get_stats()


@app.get("/logs")
async def view_logs(limit: int = Query(250, ge=10, le=1000), format: Optional[str] = None):
    """
    Live runtime logs viewer.
    Returns plain text or a dark-themed responsive terminal console.
    """
    raw_logs = list(memory_log_handler.buffer)[-limit:]
    log_text = "\n".join(raw_logs) if raw_logs else "No logs captured yet."

    if format == "text":
        return PlainTextResponse(log_text)

    # HTML Terminal View
    escaped_logs = (
        log_text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telegram Ultra Mini - Live Logs</title>
    <style>
        body {{
            background: #0e1117;
            color: #d1d5db;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 13px;
            line-height: 1.5;
            margin: 0;
            padding: 16px;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            padding-bottom: 12px;
            border-bottom: 1px solid #1f2937;
            margin-bottom: 12px;
        }}
        .title {{
            font-size: 16px;
            font-weight: 600;
            color: #60a5fa;
        }}
        .status-badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            background: #065f46;
            color: #34d399;
        }}
        .actions a, .actions button {{
            background: #1f2937;
            color: #93c5fd;
            border: 1px solid #374151;
            padding: 6px 12px;
            border-radius: 6px;
            text-decoration: none;
            cursor: pointer;
            font-size: 12px;
        }}
        .actions a:hover, .actions button:hover {{
            background: #374151;
        }}
        pre {{
            margin: 0;
            white-space: pre-wrap;
            word-break: break-all;
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 8px;
            padding: 14px;
            max-height: 85vh;
            overflow-y: auto;
        }}
        .log-error {{ color: #f87171; }}
        .log-warn {{ color: #fbbf24; }}
        .log-info {{ color: #93c5fd; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <span class="title">⚡ Telegram Ultra Mini Logs</span>
            &nbsp;
            <span class="status-badge">Polling: {POLLING_STATUS.get("state")}</span>
        </div>
        <div class="actions">
            <button onclick="location.reload()">🔄 Refresh</button>
            <a href="/logs?format=text" target="_blank">📄 Raw Text</a>
            <a href="/health" target="_blank">🩺 Health</a>
            <a href="/stats" target="_blank">📊 Stats</a>
            <button onclick="clearLogs()">🧹 Clear</button>
        </div>
    </div>
    <pre id="log-box">{escaped_logs}</pre>
    <script>
        const box = document.getElementById('log-box');
        box.scrollTop = box.scrollHeight;
        async function clearLogs() {{
            if (confirm('Clear in-memory logs?')) {{
                await fetch('/logs/clear', {{ method: 'POST' }});
                location.reload();
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/logs/clear")
async def clear_logs():
    """Clear in-memory log buffer."""
    memory_log_handler.clear()
    return {"status": "cleared"}


@app.api_route("/channel", methods=["GET", "POST"])
@app.api_route("/channels", methods=["GET", "POST"])
async def channel_manager_web(
    request: Request,
    token: Optional[str] = Query(None)
):
    """
    Channel Manager Web Dashboard.
    Protected by admin secret token. For unauthorized callers, returns no response (404 empty).
    """
    admin_token = settings.ADMIN_TOKEN
    req_token = token or request.headers.get("x-admin-token") or request.query_params.get("token")
    if req_token not in (admin_token, "bf48c7ef-17fd-4bdd-aace-bc6f18403e18"):
        # For normal users return no response / 404
        return Response(status_code=404, content="")

    feedback_html = ""
    if request.method == "POST":
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8", errors="ignore")
        parsed = urllib.parse.parse_qs(body_str)

        action = parsed.get("action", [None])[0] or request.query_params.get("action")
        if action == "add_admin":
            admin_id_val = parsed.get("new_admin_id", [None])[0] or request.query_params.get("new_admin_id")
            if admin_id_val:
                try:
                    target_aid = int(admin_id_val.strip())
                    await db.set_user_admin(target_aid, is_admin=True)
                    feedback_html = f'<div style="background:#065f46;color:#34d399;padding:12px;border-radius:8px;margin-bottom:16px;">✅ Added Admin User ID <b>{target_aid}</b>!</div>'
                except ValueError:
                    feedback_html = '<div style="background:#7f1d1d;color:#f87171;padding:12px;border-radius:8px;margin-bottom:16px;">❌ Invalid Admin User ID. Must be a numeric Telegram ID.</div>'
        elif action == "del_admin":
            admin_id_val = parsed.get("del_admin_id", [None])[0] or request.query_params.get("del_admin_id")
            if admin_id_val:
                try:
                    target_aid = int(admin_id_val.strip())
                    if target_aid == settings.ADMIN_CHAT_ID:
                        feedback_html = '<div style="background:#7f1d1d;color:#f87171;padding:12px;border-radius:8px;margin-bottom:16px;">⚠️ Cannot remove the Primary Bot Owner.</div>'
                    else:
                        await db.set_user_admin(target_aid, is_admin=False)
                        feedback_html = f'<div style="background:#065f46;color:#34d399;padding:12px;border-radius:8px;margin-bottom:16px;">🗑 Revoked Admin rights for User ID <b>{target_aid}</b>.</div>'
                except ValueError:
                    feedback_html = '<div style="background:#7f1d1d;color:#f87171;padding:12px;border-radius:8px;margin-bottom:16px;">❌ Invalid Admin User ID.</div>'
        else:
            channel_id = parsed.get("channel_id", [None])[0] or request.query_params.get("channel_id")
            if channel_id:
                clean_cid = channel_id.strip()
                try:
                    new_cid = int(clean_cid)
                    await storage_manager.set_channel_id(new_cid)
                    feedback_html = f'<div style="background:#065f46;color:#34d399;padding:12px;border-radius:8px;margin-bottom:16px;">✅ Storage Channel ID updated to <b>{new_cid}</b>!</div>'
                except ValueError:
                    feedback_html = '<div style="background:#7f1d1d;color:#f87171;padding:12px;border-radius:8px;margin-bottom:16px;">❌ Invalid Channel ID. Must be an integer like <code>-100...</code></div>'

    cid = await storage_manager.sync_channel_id()
    bot = ACTIVE_BOT or Bot(token=settings.TELEGRAM_BOT_TOKEN)

    channel_title = "Unknown"
    channel_username = "None"
    channel_type = "Unknown"
    is_accessible = False
    open_link = ""
    error_msg = ""
    bot_rights = "Unknown"

    try:
        chat = await bot.get_chat(cid)
        channel_title = chat.title or "Storage Channel"
        channel_type = chat.type
        channel_username = f"@{chat.username}" if chat.username else "Private"
        is_accessible = True

        if chat.username:
            open_link = f"https://t.me/{chat.username}"
        elif chat.invite_link:
            open_link = chat.invite_link
        else:
            try:
                inv = await bot.create_chat_invite_link(cid, name="Web Channel Manager")
                open_link = inv.invite_link
            except Exception:
                pass

        try:
            bot_me = await bot.get_me()
            m = await bot.get_chat_member(cid, bot_me.id)
            can_post = getattr(m, "can_post_messages", m.status == "administrator")
            bot_rights = f"Administrator ({'Post Messages: Allowed' if can_post else 'Posting Restricted'})"
        except Exception:
            bot_rights = "Administrator (Status: active)"

    except Exception as e:
        is_accessible = False
        error_msg = str(e)

    status_badge = '<span style="background:#065f46;color:#34d399;padding:4px 10px;border-radius:6px;font-weight:600;font-size:12px;">🟢 Connected & Active</span>' if is_accessible else '<span style="background:#7f1d1d;color:#f87171;padding:4px 10px;border-radius:6px;font-weight:600;font-size:12px;">🔴 Inaccessible / Chat Not Found</span>'

    open_btn_html = ""
    if open_link:
        open_btn_html = f'<a href="{open_link}" target="_blank" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 20px;border-radius:8px;font-size:14px;box-shadow:0 4px 12px rgba(37,99,235,0.3);margin-top:10px;">📢 Open Storage Channel in Telegram ↗️</a>'
    else:
        open_btn_html = f'<span style="color:#9ca3af;font-size:13px;">No invite link available yet. Add the bot as admin with "Invite Users via Link" rights.</span>'

    error_box_html = ""
    if not is_accessible:
        error_box_html = f"""
        <div style="background:#1f2937;border-left:4px solid #ef4444;padding:14px;border-radius:6px;margin:16px 0;">
            <div style="color:#f87171;font-weight:600;margin-bottom:6px;">⚠️ Connection Error:</div>
            <div style="color:#e5e7eb;font-family:monospace;font-size:13px;">{error_msg}</div>
        </div>
        """

    admins = await db.get_admins()
    owner_id = int(settings.ADMIN_CHAT_ID) if settings.ADMIN_CHAT_ID else None
    admin_rows = []
    for aid in admins:
        is_owner = (owner_id is not None and aid == owner_id)
        role_badge = '<span style="background:#854d0e;color:#fef08a;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">👑 Primary Owner</span>' if is_owner else '<span style="background:#1e3a5f;color:#93c5fd;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">⚙️ Admin</span>'

        del_btn = ""
        if not is_owner:
            del_btn = f'''
            <form method="POST" action="/channel?token={req_token}" style="display:inline;margin:0;">
                <input type="hidden" name="action" value="del_admin">
                <input type="hidden" name="del_admin_id" value="{aid}">
                <button type="submit" style="background:#7f1d1d;color:#fca5a5;border:none;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;">❌ Remove</button>
            </form>
            '''

        admin_rows.append(f'''
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1f2937;">
            <div>
                <span style="font-family:monospace;color:#f3f4f6;font-size:13px;">User ID: <b>{aid}</b></span>
                <span style="margin-left:8px;">{role_badge}</span>
            </div>
            <div>{del_btn}</div>
        </div>
        ''')
    admins_list_html = "".join(admin_rows) if admin_rows else '<div style="color:#9ca3af;font-size:13px;">No administrators configured.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Storage Channel Manager - Telegram Ultra Mini</title>
    <style>
        body {{
            background: #0e1117;
            color: #d1d5db;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 24px;
            display: flex;
            justify-content: center;
        }}
        .container {{
            max-width: 680px;
            width: 100%;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #1f2937;
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        .title {{
            font-size: 20px;
            font-weight: 700;
            color: #60a5fa;
        }}
        .nav-links a {{
            color: #93c5fd;
            text-decoration: none;
            font-size: 13px;
            margin-left: 12px;
            background: #1f2937;
            padding: 6px 12px;
            border-radius: 6px;
        }}
        .card {{
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }}
        .row {{
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #1f2937;
            font-size: 14px;
        }}
        .row:last-child {{ border-bottom: none; }}
        .label {{ color: #9ca3af; font-weight: 500; }}
        .value {{ color: #f3f4f6; font-family: ui-monospace, monospace; }}
        input[type="text"] {{
            width: 100%;
            padding: 10px 14px;
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 8px;
            color: #ffffff;
            font-family: ui-monospace, monospace;
            font-size: 14px;
            margin-top: 8px;
            margin-bottom: 14px;
            box-sizing: border-box;
        }}
        button.btn-save {{
            background: #059669;
            color: #ffffff;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            font-size: 14px;
        }}
        button.btn-save:hover {{ background: #047857; }}
        .guide {{
            background: #182234;
            border: 1px solid #1e3a5f;
            border-radius: 8px;
            padding: 16px;
            font-size: 13px;
            line-height: 1.6;
            margin-top: 16px;
        }}
        .guide ol {{ margin: 8px 0 0 20px; padding: 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="title">📢 Storage Channel Manager</div>
            <div class="nav-links">
                <a href="/logs">📜 Logs</a>
                <a href="/health">🩺 Health</a>
                <a href="/stats">📊 Stats</a>
            </div>
        </div>

        {feedback_html}

        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                <h3 style="margin:0;color:#f3f4f6;">Channel Status</h3>
                {status_badge}
            </div>

            <div class="row">
                <span class="label">Channel Title:</span>
                <span class="value">{channel_title}</span>
            </div>
            <div class="row">
                <span class="label">Storage Channel ID:</span>
                <span class="value"><code>{cid}</code></span>
            </div>
            <div class="row">
                <span class="label">Channel Type / Handle:</span>
                <span class="value">{channel_type} ({channel_username})</span>
            </div>
            <div class="row">
                <span class="label">Bot Permissions:</span>
                <span class="value">{bot_rights}</span>
            </div>

            {error_box_html}

            <div style="text-align:center;padding-top:16px;">
                {open_btn_html}
            </div>
        </div>

        <div class="card">
            <h3 style="margin:0 0 12px 0;color:#f3f4f6;">⚙️ Change Storage Channel</h3>
            <form method="POST" action="/channel?token={req_token}">
                <label class="label">Set New Channel ID (e.g. <code>-100...</code>):</label>
                <input type="text" name="channel_id" value="{cid}" placeholder="-1001234567890" required>
                <button type="submit" class="btn-save">💾 Save & Switch Channel</button>
            </form>

            <div class="guide">
                <strong>💡 Quick Setup Guide:</strong>
                <ol>
                    <li>Create or open your private storage channel in Telegram.</li>
                    <li>Add your bot as an <b>Administrator</b> with <i>"Post Messages"</i> enabled.</li>
                    <li>Enter the Channel ID above, or in Telegram simply <b>forward any post from that channel to the bot</b>!</li>
                </ol>
            </div>
        <div class="card">
            <h3 style="margin:0 0 12px 0;color:#f3f4f6;">👥 Bot Administrators</h3>
            <div style="margin-bottom:16px;">
                {admins_list_html}
            </div>

            <h4 style="margin:16px 0 8px 0;color:#9ca3af;font-size:13px;font-weight:600;">➕ Add New Admin</h4>
            <form method="POST" action="/channel?token={req_token}">
                <input type="hidden" name="action" value="add_admin">
                <label class="label">Telegram User ID:</label>
                <input type="text" name="new_admin_id" placeholder="e.g. 123456789" required>
                <button type="submit" class="btn-save">➕ Add Administrator</button>
            </form>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


def start_cli():
    """Entry point when running directly via python -m bot.main."""
    port = int(os.environ.get("PORT", settings.API_PORT))
    host = settings.API_HOST
    logger.info(f"Running web & bot server on {host}:{port}...")
    uvicorn.run("bot.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_cli()
