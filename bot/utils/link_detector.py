"""URL & identifier detector for YouTube and Spotify."""

import re
from typing import Optional, Tuple


# Regex patterns
YOUTUBE_VIDEO_REGEX = re.compile(
    r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)([a-zA-Z0-9_-]{11})',
    re.IGNORECASE
)

YOUTUBE_PLAYLIST_REGEX = re.compile(
    r'(?:https?://)?(?:www\.|music\.)?youtube\.com/playlist\?list=([a-zA-Z0-9_-]+)',
    re.IGNORECASE
)

SPOTIFY_TRACK_REGEX = re.compile(
    r'(?:https?://)?open\.spotify\.com/(?:intl-[a-z]+/)?track/([a-zA-Z0-9]{22})',
    re.IGNORECASE
)

SPOTIFY_PLAYLIST_REGEX = re.compile(
    r'(?:https?://)?open\.spotify\.com/(?:intl-[a-z]+/)?(?:playlist|album)/([a-zA-Z0-9]{22})',
    re.IGNORECASE
)


def extract_media_info(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Examines text and extracts: (platform, media_type, identifier)
    Examples:
        - "https://youtu.be/dQw4w9WgXcQ" -> ("youtube", "video", "dQw4w9WgXcQ")
        - "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" -> ("spotify", "track", "4cOdK2wGLETKBW3PvgPWqT")
        - "dQw4w9WgXcQ" (11-char ID) -> ("youtube", "video", "dQw4w9WgXcQ")
    """
    text = text.strip()

    # YouTube Video
    yt_match = YOUTUBE_VIDEO_REGEX.search(text)
    if yt_match:
        return "youtube", "video", yt_match.group(1)

    # YouTube Playlist
    yt_pl_match = YOUTUBE_PLAYLIST_REGEX.search(text)
    if yt_pl_match:
        return "youtube", "playlist", yt_pl_match.group(1)

    # Spotify Track
    sp_match = SPOTIFY_TRACK_REGEX.search(text)
    if sp_match:
        return "spotify", "track", sp_match.group(1)

    # Spotify Playlist / Album
    sp_pl_match = SPOTIFY_PLAYLIST_REGEX.search(text)
    if sp_pl_match:
        return "spotify", "playlist", sp_pl_match.group(1)

    # Direct 11-char YouTube Video ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', text):
        return "youtube", "video", text

    return None, None, None
