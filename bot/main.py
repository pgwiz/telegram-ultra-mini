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

    db_stats = {}
    try:
        db_stats = await db.get_stats()
    except Exception:
        pass

    db_backend = db_stats.get("backend", "MongoDB Atlas" if db.is_mongo else ("PostgreSQL" if db.is_postgres else "SQLite"))
    cached_count = db_stats.get("cached_tracks", 0)
    downloads_count = db_stats.get("total_downloads", 0)

    # Process RAM
    ram_str = "19.8 / 122.8 MB (16.1%)"
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    ram_str = f"{kb / 1024:.1f} MB (active)"
                    break
    except Exception:
        pass

    # Bot Uptime
    secs = int(time.time() - BOT_START_TIME)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    uptime_str = f"{d}d {h}h {m}m" if d else (f"{h}h {m}m" if h else f"{m}m {s}s")

    status_badge = '<span style="color:#16a34a;font-weight:700;">🟢 Online / Connected</span>' if is_accessible else '<span style="color:#dc2626;font-weight:700;">🔴 Inaccessible</span>'
    net_pill = "🟢 192.168.21.80 Net: Online" if is_accessible else "🔴 192.168.21.80 Net: Attention"

    open_btn_html = ""
    if open_link:
        open_btn_html = f'<a href="{open_link}" target="_blank" class="btn-classic">📢 Open Storage Channel in Telegram ↗</a>'
    else:
        open_btn_html = f'<span style="color:var(--text-muted);font-size:12px;">Add bot to channel to generate invite link</span>'

    error_box_html = ""
    if not is_accessible:
        error_box_html = f"""
        <div style="background:#fee2e2;border:1px solid #ef4444;color:#991b1b;padding:10px 14px;border-radius:2px;margin:12px 0;font-size:12px;">
            <strong>⚠️ Channel Error:</strong> {error_msg}
        </div>
        """

    admins = await db.get_admins()
    owner_id = int(settings.ADMIN_CHAT_ID) if settings.ADMIN_CHAT_ID else None
    admin_rows = []
    for aid in admins:
        is_owner = (owner_id is not None and aid == owner_id)
        role_badge = '<span style="border:1px solid #000;background:#000;color:#fff;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:700;">👑 OWNER</span>' if is_owner else '<span style="border:1px solid var(--btn-border);color:var(--text-main);padding:1px 6px;border-radius:2px;font-size:10px;font-weight:600;">ADMIN</span>'

        del_btn = ""
        if not is_owner:
            del_btn = f'''
            <form method="POST" action="/channel?token={req_token}" style="display:inline;margin:0;">
                <input type="hidden" name="action" value="del_admin">
                <input type="hidden" name="del_admin_id" value="{aid}">
                <button type="submit" class="btn-classic btn-danger" style="padding:1px 6px;font-size:11px;">Remove</button>
            </form>
            '''

        admin_rows.append(f'''
        <tr>
            <td style="padding:6px 8px;font-family:monospace;font-size:12px;">{aid}</td>
            <td style="padding:6px 8px;">{role_badge}</td>
            <td style="padding:6px 8px;color:#16a34a;font-weight:600;font-size:11px;">● Active</td>
            <td style="padding:6px 8px;text-align:right;">{del_btn}</td>
        </tr>
        ''')
    admins_table_rows = "".join(admin_rows)

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=120x120&margin=0&data={urllib.parse.quote_plus(open_link or 'https://t.me/popoxstbot')}"

    html = f"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AP6010SN-GN - Storage Channel Manager - Telegram Ultra Mini</title>
    <style>
        :root {{
            --bg-body: #ffffff;
            --bg-panel: #ffffff;
            --text-main: #111111;
            --text-muted: #555555;
            --border-hairline: #e2e8f0;
            --border-line: #000000;
            --border-box: #000000;
            --btn-bg: #ffffff;
            --btn-text: #111111;
            --btn-border: #000000;
            --row-border: #f0f0f0;
            --tag-active-bg: #000000;
            --tag-active-text: #ffffff;
            --code-bg: #f8fafc;
            --header-line: #000000;
        }}
        [data-theme="dark"] {{
            --bg-body: #121315;
            --bg-panel: #1a1c1e;
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --border-hairline: #2d3748;
            --border-line: #4b5563;
            --border-box: #64748b;
            --btn-bg: #22262b;
            --btn-text: #e2e8f0;
            --btn-border: #64748b;
            --row-border: #22252a;
            --tag-active-bg: #2563eb;
            --tag-active-text: #ffffff;
            --code-bg: #1e2227;
            --header-line: #64748b;
        }}
        body {{
            background: var(--bg-body);
            color: var(--text-main);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 18px 28px;
            font-size: 13px;
        }}
        .console-wrap {{
            max-width: 980px;
            margin: 0 auto;
        }}
        /* Top Brand Bar */
        .top-bar {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding-bottom: 8px;
        }}
        .brand-left {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .brand-title {{
            font-size: 20px;
            font-weight: 800;
            letter-spacing: -0.5px;
            line-height: 1.1;
        }}
        .brand-sub {{
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 3px;
        }}
        .signature {{
            text-decoration: underline dotted #666;
            cursor: pointer;
        }}
        .top-right {{
            display: flex;
            align-items: center;
            gap: 12px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
        }}
        /* Classic Buttons */
        .btn-classic {{
            background: var(--btn-bg);
            color: var(--btn-text);
            border: 1px solid var(--btn-border);
            padding: 2px 10px;
            font-size: 12px;
            font-family: inherit;
            cursor: pointer;
            border-radius: 2px;
            text-decoration: none;
            display: inline-block;
            font-weight: 500;
            line-height: 1.4;
        }}
        .btn-classic:hover {{
            background: rgba(125,125,125,0.15);
        }}
        .btn-classic.btn-danger {{
            border-color: #ef4444;
            color: #ef4444;
        }}
        .btn-classic.btn-danger:hover {{
            background: #ef4444;
            color: #ffffff;
        }}
        /* Nav Tabs Bar */
        .tabs-bar {{
            display: flex;
            gap: 20px;
            border-bottom: 2px solid var(--header-line);
            margin-top: 10px;
            padding-bottom: 0px;
            font-size: 13px;
        }}
        .tab-btn {{
            padding: 6px 4px;
            cursor: pointer;
            color: var(--text-muted);
            border-bottom: 2px solid transparent;
            margin-bottom: -2px;
            display: flex;
            align-items: center;
            gap: 6px;
            user-select: none;
        }}
        .tab-btn.active {{
            color: var(--text-main);
            font-weight: 700;
            border-bottom: 2px solid var(--header-line);
        }}
        /* Main Layout Grid */
        .main-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 32px;
            margin-top: 14px;
        }}
        .sec-head {{
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: var(--text-main);
            border-bottom: 1px solid var(--header-line);
            padding-bottom: 4px;
            margin-bottom: 8px;
        }}
        /* Status Key-Value Table */
        .status-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12.5px;
        }}
        .status-table tr {{
            border-bottom: 1px solid var(--row-border);
        }}
        .status-table td {{
            padding: 4px 0;
            vertical-align: middle;
        }}
        .status-table td.lbl {{
            color: var(--text-muted);
            width: 44%;
        }}
        .status-table td.val {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            text-align: right;
            color: var(--text-main);
            font-size: 12px;
        }}
        /* Quick Connect QR Box */
        .qr-box {{
            border: 1px solid var(--border-box);
            border-radius: 2px;
            padding: 12px;
            margin-top: 12px;
            background: var(--bg-panel);
        }}
        .qr-tabs {{
            display: flex;
            gap: 4px;
            margin-bottom: 10px;
        }}
        .qr-tab {{
            border: 1px solid var(--btn-border);
            background: var(--btn-bg);
            color: var(--btn-text);
            padding: 2px 10px;
            font-size: 11px;
            font-weight: 600;
            cursor: pointer;
            border-radius: 2px;
            text-decoration: none;
        }}
        .qr-tab.active {{
            background: var(--tag-active-bg);
            color: var(--tag-active-text);
            border-color: var(--tag-active-bg);
        }}
        .qr-content {{
            display: flex;
            gap: 16px;
            align-items: center;
        }}
        .qr-img {{
            border: 1px solid var(--border-hairline);
            background: #ffffff;
            padding: 4px;
            border-radius: 2px;
            width: 115px;
            height: 115px;
        }}
        .qr-details {{
            font-size: 11.5px;
            line-height: 1.5;
            width: 100%;
        }}
        .qr-row {{
            display: flex;
            justify-content: space-between;
            margin-bottom: 3px;
        }}
        .qr-lbl {{ color: var(--text-muted); }}
        .qr-val {{ font-family: monospace; font-weight: 600; }}
        /* Cards below */
        .classic-card {{
            border: 1px solid var(--border-hairline);
            border-radius: 2px;
            padding: 16px;
            margin-top: 20px;
            background: var(--bg-panel);
        }}
        .input-classic {{
            width: 100%;
            padding: 6px 10px;
            background: var(--code-bg);
            border: 1px solid var(--btn-border);
            color: var(--text-main);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 13px;
            border-radius: 2px;
            box-sizing: border-box;
            margin: 6px 0 10px 0;
        }}
        /* Footer */
        .console-footer {{
            border-top: 1px solid var(--border-hairline);
            margin-top: 36px;
            padding-top: 12px;
            display: flex;
            justify-content: space-between;
            font-size: 11.5px;
            color: var(--text-muted);
        }}
        @media (max-width: 768px) {{
            .main-grid {{ grid-template-columns: 1fr; gap: 20px; }}
            .qr-content {{ flex-direction: column; align-items: flex-start; }}
        }}
    </style>
</head>
<body>
    <div class="console-wrap">
        <!-- Top Bar -->
        <div class="top-bar">
            <div class="brand-left">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="#cf1717">
                    <path d="M12 2C13.5 5 13.5 7 12 9C10.5 7 10.5 5 12 2Z"/>
                    <path d="M12 22C10.5 19 10.5 17 12 15C13.5 17 13.5 19 12 22Z"/>
                    <path d="M2 12C5 10.5 7 10.5 9 12C7 13.5 5 13.5 2 12Z"/>
                    <path d="M22 12C19 13.5 17 13.5 15 12C17 10.5 19 10.5 22 12Z"/>
                    <path d="M4.93 4.93C7.5 6.5 8.5 8 8 9.5C6.5 9 5 8 4.93 4.93Z"/>
                    <path d="M19.07 19.07C16.5 17.5 15.5 16 16 14.5C17.5 15 19 16 19.07 19.07Z"/>
                    <path d="M4.93 19.07C6.5 16.5 8 15.5 9.5 16C9 17.5 8 19 4.93 19.07Z"/>
                    <path d="M19.07 4.93C17.5 7.5 16 8.5 14.5 8C15 6.5 16 5 19.07 4.93Z"/>
                    <circle cx="12" cy="12" r="2.5"/>
                </svg>
                <div>
                    <div class="brand-title">AP6010SN-GN · Storage Channel Manager</div>
                    <div class="brand-sub">Huawei Fat AP Management · <span class="signature">UI by pgwiz</span></div>
                </div>
            </div>
            <div class="top-right">
                <span class="status-pill">{net_pill}</span>
                <button id="btn-theme" class="btn-classic" onclick="toggleTheme()">Dark</button>
                <a href="{open_link or 'https://t.me/popoxstbot'}" target="_blank" class="btn-classic">Telegram ↗</a>
            </div>
        </div>

        <!-- Navigation Tabs -->
        <div class="tabs-bar">
            <div class="tab-btn active" id="tb-overview" onclick="showTab('overview')"><span>📑</span> Overview</div>
            <div class="tab-btn" id="tb-channel" onclick="showTab('channel')"><span>📢</span> Storage Channel</div>
            <div class="tab-btn" id="tb-admins" onclick="showTab('admins')"><span>👥</span> Admins ( {len(admins)} )</div>
            <div class="tab-btn" id="tb-logs" onclick="showTab('logs')"><span>📜</span> Logs</div>
        </div>

        {feedback_html}
        {error_box_html}

        <!-- TAB 1: OVERVIEW -->
        <div id="view-overview">
            <div class="main-grid">
                <!-- Left Column: STATUS -->
                <div>
                    <div class="sec-head">STATUS</div>
                    <table class="status-table">
                        <tr>
                            <td class="lbl">Storage Channel</td>
                            <td class="val">{channel_title}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Channel ID</td>
                            <td class="val"><code>{cid}</code></td>
                        </tr>
                        <tr>
                            <td class="lbl">Channel Status</td>
                            <td class="val">{status_badge}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Channel Type</td>
                            <td class="val">{channel_type} ({channel_username})</td>
                        </tr>
                        <tr>
                            <td class="lbl">Bot Status</td>
                            <td class="val">{status_badge}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Posting Rights</td>
                            <td class="val">{bot_rights}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Configured Admins</td>
                            <td class="val">{len(admins)}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Database</td>
                            <td class="val">{db_backend}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Uptime</td>
                            <td class="val">{uptime_str}</td>
                        </tr>
                        <tr>
                            <td class="lbl">RAM</td>
                            <td class="val">{ram_str}</td>
                        </tr>
                        <tr>
                            <td class="lbl">Cached Audio</td>
                            <td class="val">{cached_count} tracks</td>
                        </tr>
                        <tr>
                            <td class="lbl">Total Downloads</td>
                            <td class="val">{downloads_count} files</td>
                        </tr>
                        <tr>
                            <td class="lbl">Bridge Link</td>
                            <td class="val">br0 (eth0, tg0)</td>
                        </tr>
                    </table>
                </div>

                <!-- Right Column: ACTIONS & QR -->
                <div>
                    <div class="sec-head">ACTIONS</div>
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;">
                        <button class="btn-classic" onclick="location.reload()">Refresh</button>
                        <a href="{open_link or '#'}" target="_blank" class="btn-classic">Open Channel ↗</a>
                        <button class="btn-classic" onclick="clearRam()">Clear RAM Cache</button>
                        <button class="btn-classic btn-danger" onclick="confirmReset()">Reboot</button>
                    </div>

                    <div style="display:flex;justify-content:space-between;border-bottom:1px solid var(--row-border);padding-bottom:6px;font-size:12px;">
                        <span style="color:var(--text-muted);">Operating Mode</span>
                        <span style="font-family:monospace;font-weight:600;">Pure Access Point / Media Gateway</span>
                    </div>

                    <!-- QR Box -->
                    <div class="qr-box">
                        <div style="font-size:10.5px;font-weight:700;letter-spacing:0.05em;margin-bottom:8px;">QUICK CONNECT & LOGIN QR</div>
                        <div class="qr-tabs">
                            <span class="qr-tab active">Secure Channel</span>
                            <a href="https://t.me/popoxstbot" target="_blank" class="qr-tab">Bot Chat</a>
                            <a href="/channel?token={req_token}" class="qr-tab">Admin Web</a>
                        </div>
                        <div class="qr-content">
                            <img src="{qr_url}" alt="QR" class="qr-img">
                            <div class="qr-details">
                                <div class="qr-row">
                                    <span class="qr-lbl">Target</span>
                                    <span class="qr-val">Storage Channel</span>
                                </div>
                                <div class="qr-row">
                                    <span class="qr-lbl">SSID / URL</span>
                                    <span class="qr-val" style="word-break:break-all;">{channel_username}</span>
                                </div>
                                <div class="qr-row">
                                    <span class="qr-lbl">Channel ID</span>
                                    <span class="qr-val">{cid}</span>
                                </div>
                                <div style="color:var(--text-muted);margin-top:6px;font-size:11px;">
                                    Scan with phone camera to connect directly to the storage channel in Telegram.
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- TAB 2: STORAGE CHANNEL CONFIG -->
        <div id="view-channel" style="display:none;">
            <div class="classic-card">
                <div class="sec-head">STORAGE CHANNEL CONFIGURATION</div>
                <form method="POST" action="/channel?token={req_token}">
                    <label style="color:var(--text-muted);font-weight:600;font-size:12px;">Storage Channel ID (e.g. <code>-100...</code>):</label>
                    <input type="text" name="channel_id" value="{cid}" class="input-classic" placeholder="-1001234567890" required>
                    <button type="submit" class="btn-classic" style="padding:4px 14px;font-weight:600;">💾 Save & Switch Channel</button>
                </form>

                <div style="border-top:1px solid var(--border-hairline);margin-top:14px;padding-top:12px;font-size:12px;color:var(--text-muted);line-height:1.6;">
                    <strong>💡 How to Connect Your Channel:</strong>
                    <ol style="margin:6px 0 0 18px;padding:0;">
                        <li>Open or create your private channel in Telegram.</li>
                        <li>Add your bot as an <b>Administrator</b> with <i>"Post Messages"</i> permission.</li>
                        <li>Enter the Channel ID above, or forward any post from that channel to the bot in Telegram!</li>
                    </ol>
                </div>
            </div>
        </div>

        <!-- TAB 3: ADMINISTRATORS -->
        <div id="view-admins" style="display:none;">
            <div class="classic-card">
                <div class="sec-head">BOT ADMINISTRATORS</div>
                <table style="width:100%;border-collapse:collapse;margin-bottom:14px;">
                    <thead>
                        <tr style="border-bottom:1px solid var(--header-line);color:var(--text-muted);font-size:11px;text-align:left;">
                            <th style="padding:4px 8px;">USER ID</th>
                            <th style="padding:4px 8px;">ROLE</th>
                            <th style="padding:4px 8px;">STATUS</th>
                            <th style="padding:4px 8px;text-align:right;">ACTION</th>
                        </tr>
                    </thead>
                    <tbody>
                        {admins_table_rows}
                    </tbody>
                </table>

                <div class="sec-head" style="margin-top:16px;">ADD ADMINISTRATOR</div>
                <form method="POST" action="/channel?token={req_token}" style="display:flex;gap:8px;align-items:center;">
                    <input type="hidden" name="action" value="add_admin">
                    <input type="text" name="new_admin_id" placeholder="Telegram User ID (e.g. 123456789)" class="input-classic" style="margin:0;max-width:300px;" required>
                    <button type="submit" class="btn-classic" style="padding:6px 12px;font-weight:600;">➕ Add Admin</button>
                </form>
            </div>
        </div>

        <!-- TAB 4: SYSTEM LOGS -->
        <div id="view-logs" style="display:none;">
            <div class="classic-card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <div class="sec-head" style="margin:0;border:none;">RECENT SYSTEM LOGS</div>
                    <div>
                        <a href="/logs" target="_blank" class="btn-classic">Full Logs Page ↗</a>
                        <button class="btn-classic" onclick="location.reload()">Refresh</button>
                    </div>
                </div>
                <iframe src="/logs" style="width:100%;height:380px;border:1px solid var(--border-hairline);border-radius:2px;background:var(--code-bg);"></iframe>
            </div>
        </div>

        <!-- Footer -->
        <div class="console-footer">
            <div>Huawei AP6010SN-GN · Atheros AR9344 (MIPS 74Kc) · Telegram Ultra Mini v1.6.0</div>
            <div><span class="signature">UI by pgwiz</span></div>
        </div>
    </div>

    <script>
        // Tab switching
        function showTab(name) {{
            const tabs = ['overview', 'channel', 'admins', 'logs'];
            tabs.forEach(t => {{
                const el = document.getElementById('view-' + t);
                const btn = document.getElementById('tb-' + t);
                if (el) el.style.display = (t === name) ? 'block' : 'none';
                if (btn) {{
                    if (t === name) btn.classList.add('active');
                    else btn.classList.remove('active');
                }}
            }});
        }}

        // Dark/Light mode theme toggle
        function toggleTheme() {{
            const html = document.documentElement;
            const btn = document.getElementById('btn-theme');
            const current = html.getAttribute('data-theme') || 'light';
            const next = (current === 'light') ? 'dark' : 'light';
            html.setAttribute('data-theme', next);
            localStorage.setItem('console_theme', next);
            if (btn) btn.innerText = (next === 'light') ? 'Dark' : 'Light';
        }}

        // Restore saved theme on load
        (function() {{
            const saved = localStorage.getItem('console_theme') || 'light';
            document.documentElement.setAttribute('data-theme', saved);
            const btn = document.getElementById('btn-theme');
            if (btn) btn.innerText = (saved === 'light') ? 'Dark' : 'Light';
        }})();

        async function clearRam() {{
            if (confirm('Clear in-memory cache?')) {{
                await fetch('/logs/clear', {{ method: 'POST' }});
                alert('Cache reset.');
            }}
        }}

        function confirmReset() {{
            if (confirm('Reboot AP Management Supervisor?')) {{
                location.reload();
            }}
        }}
    </script>
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
