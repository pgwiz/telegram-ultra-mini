"""Configuration module for Telegram Ultra Mini."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str = Field(..., description="Bot token from @BotFather")
    ADMIN_CHAT_ID: int = Field(0, description="Telegram User ID of Admin")

    # Storage Channel (dump channel for audio messages)
    STORAGE_CHANNEL_ID: int = Field(..., description="Private channel ID (-100...)")

    # Stream Extractor API
    YTSP_API_BASE_URL: str = Field("https://ytsp-api.pgwiz.cloud", description="Stream Extractor API base URL")
    YTSP_API_TIMEOUT: int = Field(30, description="API HTTP timeout in seconds")
    ENABLE_API_FALLBACK: bool = Field(True, description="Fallback to local yt-dlp if API fails")

    # Neon PostgreSQL Database
    DATABASE_URL: str = Field(..., description="Neon PostgreSQL connection URI")
    DB_POOL_MIN_SIZE: int = Field(2, description="Min asyncpg pool connections")
    DB_POOL_MAX_SIZE: int = Field(10, description="Max asyncpg pool connections")

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
    DB_METADATA_TTL_DAYS: int = Field(7, description="PostgreSQL metadata cache TTL in days")
    DB_SEARCH_TTL_HOURS: int = Field(24, description="PostgreSQL search cache TTL in hours")

    # Optional HTTP Server
    ENABLE_HEALTH_SERVER: bool = Field(True, description="Run lightweight FastAPI health/metrics server")
    API_HOST: str = Field("0.0.0.0", description="API server host")
    API_PORT: int = Field(8080, description="API server port")


# Singleton instance
settings = Settings()

# Ensure directories exist
Path(settings.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(settings.MTPROTO_SESSION_PATH).parent.mkdir(parents=True, exist_ok=True)
