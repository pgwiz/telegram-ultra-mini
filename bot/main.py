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
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from bot.config import settings
from bot.database import db
from bot.api_client import api_client
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


# ── FastAPI Lifespan & Bot Polling Supervisor ─────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan manager.
    Initializes database, starts self-ping keepalive, and launches
    a supervised aiogram polling task. Cleanly cancels on shutdown.
    """
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    logger.info("🚀 Starting Telegram Ultra Mini background services...")

    # 1. Connect to Database (MongoDB Atlas, Neon PostgreSQL, or SQLite)
    await db.connect()

    # 2. Setup Bot & Dispatcher
    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
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


def start_cli():
    """Entry point when running directly via python -m bot.main."""
    port = int(os.environ.get("PORT", settings.API_PORT))
    host = settings.API_HOST
    logger.info(f"Running web & bot server on {host}:{port}...")
    uvicorn.run("bot.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_cli()
