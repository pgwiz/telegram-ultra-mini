"""Main application entry point for Telegram Ultra Mini.

Runs FastAPI health/status server and the aiogram 3 Telegram bot polling loop
simultaneously via FastAPI lifespan (compatible with Uvicorn, Gunicorn, and Render).
"""

import asyncio
import logging
import sys
import os
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import uvicorn
from fastapi import FastAPI
from bot.config import settings
from bot.database import db
from bot.api_client import api_client
from bot.handlers import register_all_handlers

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("telegram_ultra_mini")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan manager.
    Initializes database and launches aiogram polling task on server startup.
    Cleanly cancels polling and releases DB/HTTP pools on shutdown.
    """
    logger.info("Starting Telegram Ultra Mini background services...")
    # 1. Connect to Database (Neon PostgreSQL or SQLite)
    await db.connect()

    # 2. Setup Bot & Dispatcher
    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    register_all_handlers(dp)

    # 3. Launch polling as background task inside event loop
    logger.info("Starting Telegram bot polling loop...")
    await bot.delete_webhook(drop_pending_updates=True)
    polling_task = asyncio.create_task(dp.start_polling(bot))

    yield

    # 4. Graceful Shutdown
    logger.info("Shutting down background tasks and connection pools...")
    polling_task.cancel()
    try:
        await polling_task
    except (asyncio.CancelledError, Exception):
        pass
    await bot.session.close()
    await api_client.close()
    await db.disconnect()
    logger.info("Telegram Ultra Mini shutdown complete.")


# FastAPI Application instance
app = FastAPI(title="Telegram Ultra Mini", lifespan=lifespan)


@app.get("/")
async def root():
    """Render root endpoint for deployment health verification."""
    return {
        "status": "online",
        "app": "Telegram Ultra Mini",
        "version": "1.0.0",
        "database": "connected" if db.is_connected else "disconnected"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint for Render / monitor services."""
    return {
        "status": "healthy" if db.is_connected else "degraded",
        "database": "connected" if db.is_connected else "disconnected",
        "api_endpoint": settings.YTSP_API_BASE_URL
    }


@app.get("/stats")
async def get_stats():
    """Public stats endpoint."""
    return await db.get_stats()


def start_cli():
    """Entry point when running directly via python -m bot.main."""
    port = int(os.environ.get("PORT", settings.API_PORT))
    host = settings.API_HOST
    logger.info(f"Running web & bot server on {host}:{port}...")
    uvicorn.run("bot.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    start_cli()
