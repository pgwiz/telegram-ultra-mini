"""Audio Downloader Engine.

Primary: ytsp-api.pgwiz.cloud stream proxy and packaging endpoints
Fallback: yt-dlp (emergency fallback if API is unreachable)
"""

import os
import uuid
import logging
import asyncio
from typing import Optional, Tuple, Dict, Any
from pathlib import Path
import httpx
from bot.config import settings
from bot.api_client import api_client

logger = logging.getLogger(__name__)


def parse_duration_seconds(duration_val: Any) -> int:
    """Convert duration string (e.g., '3:33' or '01:15:30') or number to integer seconds."""
    if isinstance(duration_val, (int, float)):
        return int(duration_val)
    if not isinstance(duration_val, str) or not duration_val:
        return 0
    
    parts = duration_val.strip().split(':')
    try:
        if len(parts) == 1:
            return int(parts[0])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0
    return 0


class Downloader:
    def __init__(self):
        self.download_dir = Path(settings.DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_DL)

    async def download_track(
        self,
        identifier: str,
        quality: str = "audio_high",
        force_fallback: bool = False
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """
        Download track using ytsp-api (Primary) or yt-dlp (Fallback).
        Returns: (file_path, thumbnail_path, metadata)
        """
        async with self.semaphore:
            if not force_fallback:
                try:
                    logger.info(f"Attempting download via ytsp-api for {identifier} (quality: {quality})")
                    result = await self._download_via_api(identifier, quality)
                    if result[0]:
                        return result
                    logger.warning("API download returned empty, evaluating fallback...")
                except Exception as e:
                    logger.warning(f"Error during API download for {identifier}: {e}")

            if settings.ENABLE_API_FALLBACK or force_fallback:
                logger.info(f"Attempting emergency fallback download via yt-dlp for {identifier}")
                try:
                    return await self._download_via_ytdlp(identifier, quality)
                except Exception as e:
                    logger.error(f"Fallback yt-dlp failed for {identifier}: {e}")

            return None, None, {}

    # ── Primary: API Stream Download ───────────────────────────────────────

    async def _download_via_api(
        self,
        identifier: str,
        quality: str
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """Download track directly from stream extractor API."""
        # 1. Fetch stream metadata from /stream/:videoId or /get
        meta = await api_client.get_stream_info(identifier, quality=quality)
        if meta:
            proxy_url = meta.get("proxy_url")
            stream_url = meta.get("streamUrl")
            target_url = None

            if proxy_url:
                target_url = f"{settings.YTSP_API_BASE_URL.rstrip('/')}{proxy_url}" if proxy_url.startswith('/') else proxy_url
            elif stream_url:
                target_url = stream_url

            if target_url:
                title = meta.get("title") or "Unknown Track"
                artist = meta.get("uploader") or meta.get("artist") or "Unknown Artist"
                duration_secs = parse_duration_seconds(meta.get("duration"))
                thumbnail_url = meta.get("thumbnail")

                task_id = uuid.uuid4().hex[:8]
                file_path = str(self.download_dir / f"{task_id}.mp3")
                thumb_path = str(self.download_dir / f"{task_id}.jpg") if thumbnail_url else None

                try:
                    logger.info(f"Streaming audio chunks from API: {target_url}")
                    stream_timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)
                    async with httpx.AsyncClient(timeout=stream_timeout, follow_redirects=True) as stream_client:
                        async with stream_client.stream("GET", target_url) as response:
                            if response.status_code in (200, 206):
                                with open(file_path, "wb") as f:
                                    async for chunk in response.aiter_bytes(chunk_size=65536):
                                        f.write(chunk)

                                # Optional: fetch thumbnail
                                if thumb_path and thumbnail_url:
                                    try:
                                        t_resp = await stream_client.get(thumbnail_url, timeout=10.0)
                                        if t_resp.status_code == 200:
                                            with open(thumb_path, "wb") as tf:
                                                tf.write(t_resp.content)
                                    except Exception:
                                        thumb_path = None

                                metadata = {
                                    "title": title,
                                    "artist": artist,
                                    "duration": duration_secs,
                                    "videoId": meta.get("videoId") or identifier,
                                    "source": "api_stream"
                                }
                                return file_path, thumb_path, metadata
                except Exception as stream_err:
                    logger.warning(f"Stream proxy download failed: {stream_err}")

        # 2. Alternative: Try /download endpoint
        try:
            target_url_or_id = identifier if "http" in identifier else f"https://www.youtube.com/watch?v={identifier}"
            dl_res = await api_client.request_download(target_url_or_id)
            if dl_res and dl_res.get("success") and dl_res.get("files"):
                first_file = dl_res["files"][0]
                dl_path = first_file.get("download_url")
                if dl_path:
                    full_dl_url = f"{settings.YTSP_API_BASE_URL.rstrip('/')}{dl_path}" if dl_path.startswith('/') else dl_path
                    task_id = uuid.uuid4().hex[:8]
                    file_path = str(self.download_dir / f"{task_id}.mp3")

                    logger.info(f"Downloading packaged MP3 from API: {full_dl_url}")
                    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as dl_client:
                        resp = await dl_client.get(full_dl_url)
                        if resp.status_code == 200:
                            with open(file_path, "wb") as f:
                                f.write(resp.content)
                            
                            metadata = {
                                "title": first_file.get("name", "Audio Track").replace(".mp3", ""),
                                "artist": "Artist",
                                "duration": 0,
                                "videoId": identifier,
                                "source": "api_download"
                            }
                            return file_path, None, metadata
        except Exception as dl_err:
            logger.warning(f"API /download alternative failed: {dl_err}")

        return None, None, {}

    # ── Fallback: yt-dlp Emergency Download ────────────────────────────────

    async def _download_via_ytdlp(
        self,
        identifier: str,
        quality: str
    ) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """Emergency fallback using yt-dlp module."""
        import yt_dlp

        task_id = uuid.uuid4().hex[:8]
        out_tmpl = str(self.download_dir / f"{task_id}.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_tmpl,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'writethumbnail': False,
            'quiet': True,
            'no_warnings': True,
        }

        def run_ytdlp():
            target = identifier if "http" in identifier else f"https://www.youtube.com/watch?v={identifier}"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target, download=True)
                return info

        info = await asyncio.to_thread(run_ytdlp)
        file_path = str(self.download_dir / f"{task_id}.mp3")
        thumb_path = None

        metadata = {
            "title": info.get("title") or "Unknown Track",
            "artist": info.get("uploader") or "Unknown Artist",
            "duration": int(info.get("duration") or 0),
            "videoId": info.get("id") or identifier,
            "source": "ytdlp_fallback"
        }
        return file_path, thumb_path, metadata


downloader = Downloader()
