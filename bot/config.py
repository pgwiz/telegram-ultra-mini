"""Configuration module for Telegram Ultra Mini."""

import os
import logging
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, model_validator

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = Field(..., description="Bot token from @BotFather")
    TELOXIDE_TOKEN: Optional[str] = Field(None, description="Alias from telegram-ultra")
    ADMIN_CHAT_ID: int = Field(0, description="Telegram User ID of Admin")

    @model_validator(mode="before")
    @classmethod
    def resolve_bot_token(cls, values: dict):
        if isinstance(values, dict):
            # Fallback to TELOXIDE_TOKEN if TELEGRAM_BOT_TOKEN is missing
            token = values.get("TELEGRAM_BOT_TOKEN") or values.get("TELOXIDE_TOKEN")
            if token and isinstance(token, str):
                values["TELEGRAM_BOT_TOKEN"] = token.strip().strip('"\'')
            # Fallback to MONGO_URL if DATABASE_URL is missing
            if not values.get("DATABASE_URL") and values.get("MONGO_URL"):
                values["DATABASE_URL"] = values.get("MONGO_URL")
            if values.get("DATABASE_URL") and isinstance(values.get("DATABASE_URL"), str):
                values["DATABASE_URL"] = values["DATABASE_URL"].strip().strip('"\'')
            if values.get("MONGO_URL") and isinstance(values.get("MONGO_URL"), str):
                values["MONGO_URL"] = values["MONGO_URL"].strip().strip('"\'')
            if values.get("MONGO_DB_NAME") and isinstance(values.get("MONGO_DB_NAME"), str):
                values["MONGO_DB_NAME"] = values["MONGO_DB_NAME"].strip().strip('"\'')
            if values.get("STORAGE_CHANNEL_ID") and isinstance(values["STORAGE_CHANNEL_ID"], str):
                try:
                    values["STORAGE_CHANNEL_ID"] = int(values["STORAGE_CHANNEL_ID"].strip().strip('"\''))
                except ValueError:
                    pass
            if values.get("ADMIN_CHAT_ID") and isinstance(values["ADMIN_CHAT_ID"], str):
                try:
                    values["ADMIN_CHAT_ID"] = int(values["ADMIN_CHAT_ID"].strip().strip('"\''))
                except ValueError:
                    pass
            if values.get("DOWNLOAD_DIR") and isinstance(values["DOWNLOAD_DIR"], str):
                dl_dir = values["DOWNLOAD_DIR"].strip().strip('"\'')
                # If on Linux/Render but Windows drive letter was configured, fallback to ./downloads
                if os.name != "nt" and len(dl_dir) >= 2 and dl_dir[1] == ":":
                    values["DOWNLOAD_DIR"] = "./downloads"
                else:
                    values["DOWNLOAD_DIR"] = dl_dir
        return values

    ADMIN_TOKEN: str = Field("bf48c7ef-17fd-4bdd-aace-bc6f18403e18", description="Admin secret token for web dashboards")

    # Storage Channel (dump channel for audio messages)
    STORAGE_CHANNEL_ID: int = Field(..., description="Private channel ID (-100...)")

    # Stream Extractor API
    YTSP_API_BASE_URL: str = Field("https://ytsp-api.pgwiz.cloud", description="Stream Extractor API base URL")
    YTSP_API_TIMEOUT: int = Field(30, description="API HTTP timeout in seconds")
    ENABLE_API_FALLBACK: bool = Field(True, description="Fallback to local yt-dlp if API fails")

    # Database (MongoDB, Neon PostgreSQL or SQLite)
    DATABASE_URL: Optional[str] = Field(None, description="Database connection URI (MongoDB, PostgreSQL, or SQLite)")
    MONGO_URL: Optional[str] = Field(None, description="MongoDB connection URI alias")
    MONGO_DB_NAME: Optional[str] = Field(None, description="MongoDB database name override")
    DATABASE_PATH: Optional[str] = Field(None, description="SQLite path if used")
    DB_POOL_MIN_SIZE: int = Field(2, description="Min connection pool size")
    DB_POOL_MAX_SIZE: int = Field(10, description="Max connection pool size")

    # MTProto / Telethon
    MPROTO: bool = Field(False, description="Enable MTProto client for files >50MB")
    TELEGRAM_API_ID: int = Field(0, description="Telegram API ID")
    TELEGRAM_API_HASH: str = Field("", description="Telegram API Hash")
    TELEGRAM_PHONE: str = Field("", description="Telegram Phone Number")
    MTPROTO_SESSION_PATH: str = Field("./sessions/hermes_session", description="Path to Telethon session")

    # Downloads & File Handling
    DOWNLOAD_DIR: str = Field("./downloads", description="Local temp download directory")
    MAX_CONCURRENT_DL: int = Field(3, description="Max concurrent downloads per instance")
    AUTO_CLEANUP_TEMP: bool = Field(True, description="Auto delete local temp files after upload")

    # Cache Settings
    ENABLE_MEMORY_CACHE: bool = Field(True, description="Enable RAM cache")
    MEMORY_CACHE_MAXSIZE: int = Field(2000, description="Max items in memory cache")
    MEMORY_CACHE_TTL_SECS: int = Field(3600, description="RAM cache TTL in seconds")
    DB_METADATA_TTL_DAYS: int = Field(7, description="Metadata cache TTL in days")
    DB_SEARCH_TTL_HOURS: int = Field(24, description="Search cache TTL in hours")

    # Optional HTTP Server / Render port
    ENABLE_HEALTH_SERVER: bool = Field(True, description="Run lightweight FastAPI health/metrics server")
    API_HOST: str = Field("0.0.0.0", description="API server host")
    API_PORT: int = Field(default_factory=lambda: int(os.environ.get("PORT", "8080")), description="API server port")


# Singleton instance
settings = Settings()

# Safe directory creation with fallback for cross-platform / Render paths
try:
    if os.name != "nt" and len(settings.DOWNLOAD_DIR) >= 2 and settings.DOWNLOAD_DIR[1] == ":":
        settings.DOWNLOAD_DIR = "./downloads"
    Path(settings.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
except Exception as e:
    logger.warning(f"Could not create DOWNLOAD_DIR={settings.DOWNLOAD_DIR} ({e}), falling back to ./downloads")
    settings.DOWNLOAD_DIR = "./downloads"
    Path(settings.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

try:
    Path(settings.MTPROTO_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
