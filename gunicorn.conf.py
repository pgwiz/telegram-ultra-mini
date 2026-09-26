"""Gunicorn configuration file for Render and cloud deployments.

Automatically binds to 0.0.0.0:$PORT so Render health checks and
port detectors immediately detect the service.
"""

import os

# Bind to Render's assigned dynamic port (or fallback to 8080)
port = os.getenv("PORT", "8080")
bind = f"0.0.0.0:{port}"

# Single worker is mandatory for Telegram bots to prevent duplicate polling conflicts
workers = 1

# Default worker class for ASGI / FastAPI apps
worker_class = "uvicorn.workers.UvicornWorker"

# Generous timeout for long streaming transfers
timeout = 120
keepalive = 5
