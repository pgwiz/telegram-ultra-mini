"""String and display formatting utilities."""


def format_duration(seconds: int) -> str:
    """Format seconds into MM:SS or HH:MM:SS."""
    if not seconds or seconds < 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_file_size(size_bytes: int) -> str:
    """Format bytes into readable string (KB, MB, GB)."""
    if not size_bytes or size_bytes < 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def clean_caption(title: str, artist: str, duration: int, quality: str) -> str:
    """Generate clean, consistent audio caption."""
    dur_str = format_duration(duration)
    return (
        f"🎧 <b>{title}</b>\n"
        f"👤 <i>{artist}</i>\n"
        f"⏱ <code>{dur_str}</code> | ⚙️ <code>{quality}</code>"
    )
