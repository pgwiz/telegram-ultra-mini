"""Handlers package for Telegram Ultra Mini."""

from aiogram import Dispatcher
from bot.handlers.start import router as start_router
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.search import router as search_router
from bot.handlers.playlist import router as playlist_router
from bot.handlers.admin import router as admin_router
from bot.handlers.download import router as download_router


def register_all_handlers(dp: Dispatcher) -> None:
    """Register all modular routers in priority order."""
    dp.include_router(start_router)
    dp.include_router(callbacks_router)
    dp.include_router(search_router)
    dp.include_router(playlist_router)
    dp.include_router(admin_router)
    dp.include_router(download_router)  # Handles general text / URLs
