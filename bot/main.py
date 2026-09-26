"""Main application entry point for Telegram Ultra Mini."""

import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import uvicorn
from fastapi import FastAPI
from bot.config import settings
from bot.database import db
from bot.api_client import api_client
from bot.handlers import register_all_handlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("telegram_ultra_mini")

# FastAPI Health/Metrics App
app = FastAPI(title="Telegram Ultra Mini Health API")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    db_ok = db.pool is not None
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "api_endpoint": settings.YTSP_API_BASE_URL
    }


@app.get("/stats")
async def get_stats():
    """Public stats endpoint."""
    return await db.get_stats()


async def run_fastapi():
    """Run lightweight FastAPI server in background."""
    config = uvicorn.Config(
        app=app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        log_level="warning"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    """Main execution function."""
    logger.info("Starting Telegram Ultra Mini...")

    # 1. Connect to Neon PostgreSQL
    await db.connect()

    # 2. Setup Bot & Dispatcher
    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # 3. Register handlers
    register_all_handlers(dp)

    tasks = []

    # 4. Optional Health Server
    if settings.ENABLE_HEALTH_SERVER:
        logger.info(f"Starting health API on {settings.API_HOST}:{settings.API_PORT}...")
        tasks.append(asyncio.create_task(run_fastapi()))

    # 5. Start Telegram Polling
    logger.info("Starting Telegram polling loop...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        bot_task = asyncio.create_task(dp.start_polling(bot))
        tasks.append(bot_task)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown signal received.")
    finally:
        logger.info("Cleaning up resources...")
        await bot.session.close()
        await api_client.close()
        await db.disconnect()
        logger.info("Telegram Ultra Mini stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated.")
