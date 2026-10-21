"""Stream Extractor API Client for ytsp-api.pgwiz.cloud with smart caching and retries."""

import asyncio
import json
import logging
import re
from typing import Optional, Dict, Any, List
import httpx
from bot.config import settings
from bot.cache import cache

logger = logging.getLogger(__name__)


class StreamApiClient:
    def __init__(self):
        self.base_url = settings.YTSP_API_BASE_URL.rstrip('/')
        self.timeout = settings.YTSP_API_TIMEOUT
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or initialize persistent async HTTP client tied to current event loop."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or getattr(self, "_loop", None) != loop:
            if self._client and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._loop = loop
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
                follow_redirects=True,
                headers={"User-Agent": "TelegramUltraMini/1.0"}
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request_with_retry(self, method: str, path: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Execute HTTP request with 3 retries and exponential backoff."""
        client = await self.get_client()
        retries = 3
        delay = 1.0

        for attempt in range(1, retries + 1):
            try:
                response = await client.request(method, path, **kwargs)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code in (404, 400):
                    logger.warning(f"API request {path} returned client error: {response.status_code}")
                    return None
                else:
                    logger.warning(f"API {path} status {response.status_code}, attempt {attempt}/{retries}")
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning(f"API network error for {path}: {e}, attempt {attempt}/{retries}")

            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2

        logger.error(f"API request failed after {retries} attempts: {path}")
        return None

    # ── 1. Video / Track Stream Extraction ─────────────────────────────────

    async def get_stream_info(self, video_id_or_url: str, quality: str = "audio") -> Optional[Dict[str, Any]]:
        """
        Extract stream metadata and playable stream URL for a YouTube video or Spotify track.
        Uses /stream/:videoId or /get.
        """
        cache_key = cache.hash_key("stream", f"{video_id_or_url}:{quality}")
        cached_result = await cache.get(cache_key)
        if cached_result:
            return cached_result

        is_spotify = "spotify" in video_id_or_url or len(video_id_or_url) == 22
        if is_spotify:
            sp_url = video_id_or_url if video_id_or_url.startswith("http") else f"https://open.spotify.com/track/{video_id_or_url}"
            data = await self._request_with_retry("GET", "/get", params={"ytl": sp_url, "quality": quality}, timeout=45.0)
        else:
            # Path endpoint /stream/:videoId
            endpoint = f"/stream/{video_id_or_url}"
            params = {"quality": quality}
            data = await self._request_with_retry("GET", endpoint, params=params)

            if not data or not data.get("streamUrl"):
                # Fallback to /get?ytl=...
                data = await self._request_with_retry("GET", "/get", params={"ytl": video_id_or_url, "quality": quality})

        if data:
            # Cache stream metadata in DB for 7 days
            ttl = settings.DB_METADATA_TTL_DAYS * 86400
            await cache.set(cache_key, data, ttl)

        return data

    # ── 2. Search Music Catalog (Spotify / YouTube matched) ────────────────

    async def search_tracks(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search tracks using /api/search/spotify.
        Returns array of results with videoId, title, artist, duration, thumbnail.
        """
        cache_key = cache.hash_key("search", f"{query}:{limit}")
        cached_result = await cache.get(cache_key)
        if cached_result and "results" in cached_result:
            return cached_result["results"]

        params = {"query": query, "type": "track", "limit": limit}
        data = await self._request_with_retry("GET", "/api/search/spotify", params=params)

        if data and "results" in data:
            # Cache search results for 24 hours
            ttl = settings.DB_SEARCH_TTL_HOURS * 3600
            await cache.set(cache_key, data, ttl)
            return data["results"]

        return []

    # ── 3. Extract Playlist Info & Tracks (Spotify & YouTube) ─────────────

    async def get_playlist_info(self, platform: str, identifier: str, limit: int = 100) -> Dict[str, Any]:
        """
        Extract full playlist metadata (title, author, total count) and track list.
        Supports 100+ tracks for Spotify (keyless embed + stream resolver) and YouTube.
        """
        cache_key = cache.hash_key("pl_info", f"{platform}:{identifier}:{limit}")
        cached = await cache.get(cache_key)
        if cached and "tracks" in cached:
            return cached

        title = "Playlist"
        author = "Various Artists"
        total_tracks = 0
        tracks: List[Dict[str, Any]] = []

        if platform == "spotify":
            # 1. Fetch full tracklist from keyless Spotify embed (returns up to 100 tracks in 200ms)
            embed_tracks = []
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.get(f"https://open.spotify.com/embed/playlist/{identifier}", headers=headers, follow_redirects=True)
                    if r.status_code == 200:
                        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
                        if m:
                            data = json.loads(m.group(1))
                            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                            title = entity.get("name") or "Spotify Playlist"
                            owner = entity.get("owner")
                            author = owner.get("name") if isinstance(owner, dict) else (owner or "Spotify")
                            embed_tracks = entity.get("trackList", [])
                            total_tracks = len(embed_tracks)
            except Exception as e:
                logger.warning(f"Error fetching Spotify embed for {identifier}: {e}")

            # 2. Fast single attempt to fetch pre-resolved videoId mappings from streaming API
            api_tracks = []
            try:
                client = await self.get_client()
                api_res = await client.get(f"/api/spotify/playlist/{identifier}", params={"limit": 25}, timeout=8.0)
                if api_res.status_code == 200:
                    api_data = api_res.json()
                    api_tracks = api_data.get("tracks") or api_data.get("results") or []
                    if not title or title == "Spotify Playlist":
                        title = api_data.get("title") or api_data.get("name") or "Spotify Playlist"
                    if not total_tracks:
                        total_tracks = api_data.get("totalTracks") or len(api_tracks)
            except Exception as e:
                logger.debug(f"Streaming API playlist lookup skipped: {e}")

            # 3. Combine tracks up to requested limit
            if embed_tracks:
                for idx, t in enumerate(embed_tracks[:limit]):
                    video_id = None
                    if idx < len(api_tracks):
                        video_id = api_tracks[idx].get("videoId")
                    tracks.append({
                        "title": t.get("title") or f"Track {idx+1}",
                        "name": t.get("title") or f"Track {idx+1}",
                        "artist": t.get("subtitle") or "Unknown Artist",
                        "duration": t.get("duration", 0),
                        "videoId": video_id,
                        "source": "spotify",
                        "spotify_uri": t.get("uri")
                    })
            elif api_tracks:
                tracks = api_tracks[:limit]
                total_tracks = len(tracks)

        else:
            # YouTube Playlist Extraction
            # 1. Try remote API endpoint
            try:
                data = await self._request_with_retry("GET", "/api/youtube/playlist", params={"id": identifier, "limit": limit}, timeout=20.0)
                if data:
                    tracks = data.get("tracks") or data.get("items") or []
                    title = data.get("title") or "YouTube Playlist"
                    author = data.get("author") or data.get("uploader") or "YouTube"
                    total_tracks = data.get("total") or len(tracks)
            except Exception:
                pass

            # 2. Emergency fallback: yt-dlp flat playlist extraction
            if not tracks:
                try:
                    target_url = identifier if "http" in identifier else f"https://www.youtube.com/playlist?list={identifier}"
                    proc = await asyncio.create_subprocess_exec(
                        "yt-dlp", "--flat-playlist", "--dump-single-json", target_url,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"yt-dlp playlist extraction timed out for {identifier}")
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                        stdout = b""

                    if proc.returncode == 0 and stdout:
                        yt_data = json.loads(stdout.decode(errors="ignore"))
                        title = yt_data.get("title") or "YouTube Playlist"
                        author = yt_data.get("uploader") or yt_data.get("channel") or "YouTube"
                        entries = yt_data.get("entries", [])
                        total_tracks = len(entries)
                        for e in entries[:limit]:
                            vid = e.get("id")
                            if vid:
                                tracks.append({
                                    "title": e.get("title") or "Track",
                                    "name": e.get("title") or "Track",
                                    "artist": e.get("uploader") or author,
                                    "duration": e.get("duration", 0),
                                    "videoId": vid,
                                    "url": f"https://www.youtube.com/watch?v={vid}",
                                    "source": "youtube"
                                })
                except Exception as e:
                    logger.error(f"Fallback yt-dlp playlist extraction failed for {identifier}: {e}")

        result = {
            "title": title or "Playlist",
            "author": author or "Various Artists",
            "total_tracks": total_tracks or len(tracks),
            "tracks": tracks,
            "platform": platform,
            "identifier": identifier
        }

        if tracks:
            # Cache metadata for 7 days
            await cache.set(cache_key, result, settings.DB_METADATA_TTL_DAYS * 86400)

        return result

    async def get_spotify_playlist(self, playlist_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Extract Spotify playlist or album tracks (supports 100+ tracks)."""
        info = await self.get_playlist_info("spotify", playlist_id, limit=limit)
        return info.get("tracks", [])

    async def get_youtube_playlist(self, playlist_id_or_url: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Extract YouTube playlist / album / mix tracks (supports 100+ tracks)."""
        info = await self.get_playlist_info("youtube", playlist_id_or_url, limit=limit)
        return info.get("tracks", [])

    # ── 5. Download & Package Audio Track via API ──────────────────────────

    async def request_download(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Request download package using /download endpoint.
        Returns download URLs for packaged MP3/ZIP.
        """
        endpoint = "/download"
        json_body = {"url": url}
        return await self._request_with_retry("POST", endpoint, json=json_body)


api_client = StreamApiClient()
