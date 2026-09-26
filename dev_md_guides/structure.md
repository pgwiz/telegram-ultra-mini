# Codebase Architecture & Directory Dependency Graph
_Last regenerated: 2026-09-26 08:03:43 UTC by dev-md-compactor_

## Project Manifests & Build Tools
- `requirements.txt`

## Module Directory Topography

### `bot/`
- **Files (9)**: `__init__.py, api_client.py, cache.py, config.py, database.py, downloader.py, main.py, mtproto.py` (+1 more)
- **Discovered Module Dependencies**: `aiogram, aiosqlite, asyncio, asyncpg, bot, cachetools, contextlib, datetime, fastapi, hashlib, httpx, json, logging, os, pathlib, pydantic, pydantic_settings, sys, telethon, typing, uuid, uvicorn, yt_dlp`

### `bot/handlers/`
- **Files (7)**: `__init__.py, admin.py, callbacks.py, download.py, playlist.py, search.py, start.py`
- **Discovered Module Dependencies**: `aiogram, asyncio, bot, logging, time`

### `bot/utils/`
- **Files (3)**: `__init__.py, formatters.py, link_detector.py`
- **Discovered Module Dependencies**: `re, typing`

### `downloads/`
- **Files (10)**: `24445e15.mp3, 2bf05257.mp3, 45ea0ed1.jpg, 45ea0ed1.mp3, 7b480e8f.webm.part, 7b480e8f.webp, c17550a3.mp3, e0f57bb4.jpg` (+2 more)

### `root/`
- **Files (8)**: `agent.md, changelog.md, memory.md, readme.md, requirements.txt, skills-lock.json, test_verification.py, wsgi.py`
- **Discovered Module Dependencies**: `a2wsgi, asyncio, bot, os, sys`

### `your_application/`
- **Files (2)**: `__init__.py, wsgi.py`
- **Discovered Module Dependencies**: `wsgi`

## Environment & Directory Catalog Reference
- Central Catalog: `dev_md_guides/directory.md` (sample committed as `dev_md_guides/directory.md.sample`).
- All workspace paths, servers, backend links, frontend links, and external endpoints are centralized in this catalog.
- Invariant: Never hardcode local filesystem paths or network URLs directly across project markdown files.

## Credentials & Secrets Reference
- Central Secrets Schema: `dev_md_guides/credentials.md` (sample committed as `dev_md_guides/credentials.md.sample`).
- Real secrets and sensitive tokens are kept strictly local in `credentials.md` and MUST NEVER be committed to GitHub.
- Invariant: Never commit credentials to GitHub; if the user ever specifies committing credentials, the agent must first explicitly warn the user about critical security risks.

## Architectural Invariants & Boundary Rules
- Internal modules should adhere to defined dependency boundaries without cyclic imports.
- Configuration, secrets, and environment overrides must not be hardcoded in application logic.
- Zero Hardcoded Endpoints: Do not hardcode machine directories, server IPs, backend links, or frontend links across markdown docs; resolve and reference them via directory.md (only directory.md.sample is committed to version control).
- Zero Credential Exposure: Never commit credentials.md or real secrets to git/GitHub. Only credentials.md.sample with sanitized placeholders is tracked. If the user explicitly asks to commit credentials, issue a critical security warning and require confirmation before proceeding.
