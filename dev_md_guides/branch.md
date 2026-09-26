# Branch State & Worktree Topology

- **Active Branch**: `main`
- **HEAD Commit**: `5b87607` — feat: add plain-text search fallback and native MP4 video download support
- **Tracking Status**: Ahead 0 commits, behind 0 commits relative to origin/main
- **Last Updated**: 2026-09-26 08:50:03 UTC

## Divergence Analysis

### Recent Commits (Local)
- `5b87607` (2026-09-26 11:47:45 +0300): feat: add plain-text search fallback and native MP4 video download support
- `3ed9fae` (2026-09-26 11:35:40 +0300): fix(cache): resolve 'Database' object has no attribute 'pool' and align api_cache schema
- `811e5e9` (2026-09-26 11:28:44 +0300): feat(telegram): automatically register bot command menu on startup
- `8df9598` (2026-09-26 11:22:57 +0300): fix(database): allow graceful fallback for sqlite:// URLs while keeping full Neon resilience
- `3da257b` (2026-09-26 11:17:25 +0300): feat(render): add gunicorn.conf.py for automatic PORT binding and single-worker setup
- `325e047` (2026-09-26 11:15:27 +0300): fix(render): add gunicorn to requirements.txt for Render web service
- `69508bf` (2026-09-26 11:09:50 +0300): feat: dedicated Neon Postgres with cold-start resilience, PgBouncer pooling, and Render WSGI support
- `149c189` (2026-09-26 11:04:09 +0300): feat: add Render WSGI entrypoint, SQLite & Postgres dual support, and dev_md_guides
- `18662b7` (2026-09-26 10:45:25 +0300): feat: initial release of telegram-ultra-mini pure-python bot with channel warehousing and neon postgres

### Uncommitted Working Tree State
- `M bot/main.py`

## Integration Checklist

- [ ] Working tree cleanly committed or stashed before branch switch
- [ ] Rebase / sync with upstream verified
- [ ] Code passes test and lint gates
