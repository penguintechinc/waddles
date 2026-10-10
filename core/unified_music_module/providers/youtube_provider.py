"""
YouTube music provider implementation.

This module provides integration with YouTube Data API v3 for searching and
retrieving video information. Playback is handled via browser source (iframe).
"""

import os
import re
import logging
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from .base_provider import BaseMusicProvider, MusicTrack


logger = logging.getLogger(__name__)


class YouTubeProviderError(Exception):
    """Base exception for YouTube provider errors."""
    pass


class YouTubeAuthenticationError(YouTubeProviderError):
    """Raised when YouTube authentication fails."""
    pass


class YouTubeAPIError(YouTubeProviderError):
    """Raised when YouTube API requests fail."""
    pass


class _StripHttpxQueryFilter(logging.Filter):
    """Remove the query string from httpx's ``HTTP Request: GET <url>`` INFO lines.

    The YouTube API takes the user's search text (``q=``) and the API key (``key=``)
    in the query string, and httpx logs the full URL for every request at INFO.
    Query strings are user data / secrets, so none may reach the log stream.
    """

    @staticmethod
    def _strip(value: Any) -> Any:
        """Return `value` without its query string if it is a URL, else unchanged."""
        if isinstance(value, httpx.URL):
            return value.copy_with(query=None)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.split("?", 1)[0]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record's URL argument(s) in place; never drops the record."""
        if isinstance(record.args, tuple):
            record.args = tuple(self._strip(arg) for arg in record.args)
        return True


def _install_httpx_query_redaction() -> None:
    """Attach `_StripHttpxQueryFilter` to the ``httpx`` logger exactly once."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _StripHttpxQueryFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(_StripHttpxQueryFilter())


_install_httpx_query_redaction()

_REASON_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _api_error_reason(response: httpx.Response) -> Optional[str]:
    """Return Google's machine-readable error reason (e.g. ``quotaExceeded``), if safe.

    Only the ``errors[0].reason`` enum is read -- never the free-text ``message``,
    which may echo request parameters (search text, video IDs, the API key).
    """
    try:
        reason = response.json()["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        # body is non-JSON or shaped differently -- expected for proxies / HTML error pages
        logger.debug("YouTube error body has no usable reason: %s", type(exc).__name__)
        return None
    if isinstance(reason, str) and _REASON_RE.fullmatch(reason):
        return reason
    logger.debug("YouTube error reason is not a plain identifier; dropped")
    return None


def _describe_error(exc: BaseException) -> str:
    """Describe `exc` for logs without its message: type, HTTP status, API reason.

    ``str(httpx.HTTPStatusError)`` embeds the full request URL -- the user's
    search text and the ``key=`` API key -- so it must never be logged.
    """
    parts = [type(exc).__name__]
    if isinstance(exc, YouTubeProviderError):
        # our own messages are static / built by `_to_api_error` -- safe to log
        parts.append(f"detail={exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        parts.append(f"status={exc.response.status_code}")
        reason = _api_error_reason(exc.response)
        if reason:
            parts.append(f"reason={reason}")
    return " ".join(parts)


def _to_api_error(exc: httpx.HTTPStatusError | httpx.RequestError) -> YouTubeAPIError:
    """Build the `YouTubeAPIError` for an httpx failure without echoing request data."""
    if isinstance(exc, httpx.HTTPStatusError):
        message = f"YouTube API error: {exc.response.status_code}"
        reason = _api_error_reason(exc.response)
        if reason:
            message += f" - {reason}"
        return YouTubeAPIError(message)
    return YouTubeAPIError(f"YouTube API request failed: {type(exc).__name__}")


class YouTubeProvider(BaseMusicProvider):
    """YouTube music provider implementation.

    This provider integrates with YouTube Data API v3 for searching videos,
    retrieving video details, and parsing YouTube URLs. Playback is handled
    via browser source iframe integration rather than direct playback.

    Environment Variables:
        YOUTUBE_API_KEY: YouTube Data API v3 key
        YOUTUBE_CLIENT_ID: OAuth2 client ID (optional, for future OAuth features)
        YOUTUBE_CLIENT_SECRET: OAuth2 client secret (optional, for future OAuth features)
    """

    PROVIDER_NAME: str = "youtube"

    # YouTube API endpoints
    YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
    YOUTUBE_VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"

    # URL patterns for parsing YouTube links
    URL_PATTERNS = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?.*&v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})',
    ]

    def __init__(self):
        """Initialize the YouTube provider."""
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.client_id = os.getenv("YOUTUBE_CLIENT_ID")
        self.client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")

        self._http_client: Optional[httpx.AsyncClient] = None
        self._authenticated = False
        self._current_track: Optional[MusicTrack] = None
        self._is_playing = False

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self):
        """Close the HTTP client and cleanup resources."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from YouTube URL.

        Supports:
        - https://www.youtube.com/watch?v=xxx
        - https://youtube.com/watch?v=xxx
        - https://youtu.be/xxx
        - http variants

        Args:
            url: YouTube URL to parse

        Returns:
            Video ID if found, None otherwise
        """
        for pattern in self.URL_PATTERNS:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def is_youtube_url(value: str) -> bool:
        """Return True only when `value` is a URL whose host is exactly
        youtube.com, a youtube.com subdomain, or youtu.be.

        Uses `urlparse().hostname` for an exact-match/allowlist check --
        never a substring test (`"youtube.com" in value`), which a crafted
        string such as "https://evil.com/?redirect=youtube.com" or
        "https://notyoutube.com.evil.com/" would incorrectly pass. Mirrors
        the safe-provider-detection pattern in
        `hub_api/services/music_providers/__init__.py::_detect_provider`.

        Args:
            value: candidate URL or bare video ID

        Returns:
            True if `value` is a genuine youtube.com/youtu.be URL
        """
        parsed = urlparse(value)
        hostname = parsed.hostname
        if not hostname and "://" not in value:
            # No scheme (e.g. "youtu.be/xyz" pasted without "https://") --
            # urlparse can't find a host without one; re-parse as
            # scheme-relative so the same host logic below still applies.
            parsed = urlparse(f"//{value}")
            hostname = parsed.hostname
        if not hostname:
            return False
        return (
            hostname == "youtu.be"
            or hostname == "youtube.com"
            or hostname.endswith(".youtube.com")
        )

    def _parse_duration(self, duration_str: str) -> int:
        """Parse ISO 8601 duration string to milliseconds.

        Args:
            duration_str: ISO 8601 duration (e.g., "PT4M13S")

        Returns:
            Duration in milliseconds
        """
        # Simple parser for PT#H#M#S format
        duration_ms = 0

        # Remove PT prefix
        duration_str = duration_str.replace("PT", "")

        # Parse hours
        hours_match = re.search(r'(\d+)H', duration_str)
        if hours_match:
            duration_ms += int(hours_match.group(1)) * 3600000

        # Parse minutes
        minutes_match = re.search(r'(\d+)M', duration_str)
        if minutes_match:
            duration_ms += int(minutes_match.group(1)) * 60000

        # Parse seconds
        seconds_match = re.search(r'(\d+)S', duration_str)
        if seconds_match:
            duration_ms += int(seconds_match.group(1)) * 1000

        return duration_ms

    def _video_to_track(self, video_data: Dict[str, Any]) -> MusicTrack:
        """Convert YouTube API video data to MusicTrack.

        Args:
            video_data: Video data from YouTube API

        Returns:
            MusicTrack object
        """
        video_id = video_data["id"]
        if isinstance(video_id, dict):
            video_id = video_id.get("videoId", "")

        snippet = video_data.get("snippet", {})
        content_details = video_data.get("contentDetails", {})

        # Get thumbnail URL (prefer high quality)
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (
            thumbnails.get("high", {}).get("url") or
            thumbnails.get("medium", {}).get("url") or
            thumbnails.get("default", {}).get("url") or
            ""
        )

        # Parse duration if available
        duration_ms = 0
        if content_details and "duration" in content_details:
            duration_ms = self._parse_duration(content_details["duration"])

        return MusicTrack(
            track_id=video_id,
            name=snippet.get("title", "Unknown Title"),
            artist=snippet.get("channelTitle", "Unknown Channel"),
            album="YouTube",
            album_art_url=thumbnail_url,
            duration_ms=duration_ms,
            provider=self.PROVIDER_NAME,
            uri=self.YOUTUBE_VIDEO_URL.format(video_id=video_id),
            metadata={
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
                "channel_id": snippet.get("channelId", ""),
                "tags": snippet.get("tags", []),
            }
        )

    async def search(self, query: str, limit: int = 10) -> list[MusicTrack]:
        """Search for YouTube videos matching the query.

        Args:
            query: Search query string
            limit: Maximum number of results (default: 10, max: 50)

        Returns:
            List of MusicTrack objects

        Raises:
            YouTubeAPIError: If the API request fails
            YouTubeAuthenticationError: If not authenticated
        """
        if not self.api_key:
            raise YouTubeAuthenticationError("YouTube API key not configured")

        # Limit max results to 50 (YouTube API limit)
        limit = min(limit, 50)

        client = await self._get_http_client()

        try:
            # Search for videos
            search_url = f"{self.YOUTUBE_API_BASE}/search"
            search_params = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": limit,
                "key": self.api_key,
                "videoCategoryId": "10",  # Music category
            }

            logger.debug("Searching YouTube (query_len=%d, limit=%d)", len(query), limit)
            search_response = await client.get(search_url, params=search_params)
            search_response.raise_for_status()
            search_data = search_response.json()

            if "items" not in search_data or not search_data["items"]:
                logger.info("No YouTube results for search (query_len=%d)", len(query))
                return []

            # Get video IDs
            video_ids = [item["id"]["videoId"] for item in search_data["items"]]

            # Get detailed video information (including duration)
            videos_url = f"{self.YOUTUBE_API_BASE}/videos"
            videos_params = {
                "part": "snippet,contentDetails",
                "id": ",".join(video_ids),
                "key": self.api_key,
            }

            videos_response = await client.get(videos_url, params=videos_params)
            videos_response.raise_for_status()
            videos_data = videos_response.json()

            # Convert to MusicTrack objects
            tracks = [
                self._video_to_track(video)
                for video in videos_data.get("items", [])
            ]

            logger.info(
                "Found %d YouTube videos for search (query_len=%d)", len(tracks), len(query)
            )
            return tracks

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("YouTube search failed: %s", _describe_error(e))
            # `from None`: the cause's message is the full request URL (search text + API key)
            raise _to_api_error(e) from None

    async def get_track(self, track_id: str) -> Optional[MusicTrack]:
        """Get YouTube video details by video ID.

        Args:
            track_id: YouTube video ID

        Returns:
            MusicTrack object if found, None otherwise

        Raises:
            YouTubeAPIError: If the API request fails
            YouTubeAuthenticationError: If not authenticated
        """
        if not self.api_key:
            raise YouTubeAuthenticationError("YouTube API key not configured")

        # If track_id is a URL, extract the video ID
        if self.is_youtube_url(track_id):
            video_id = self.extract_video_id(track_id)
            if not video_id:
                logger.warning("Could not extract video ID from the supplied URL")
                return None
            track_id = video_id

        client = await self._get_http_client()

        try:
            videos_url = f"{self.YOUTUBE_API_BASE}/videos"
            videos_params = {
                "part": "snippet,contentDetails",
                "id": track_id,
                "key": self.api_key,
            }

            logger.debug(f"Fetching YouTube video: {track_id}")
            response = await client.get(videos_url, params=videos_params)
            response.raise_for_status()
            data = response.json()

            if "items" not in data or not data["items"]:
                logger.info(f"YouTube video not found: {track_id}")
                return None

            track = self._video_to_track(data["items"][0])
            logger.info(f"Retrieved YouTube video: {track.name}")
            return track

        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.error("YouTube video lookup failed: %s", _describe_error(e))
            # `from None`: the cause's message is the full request URL (video ID + API key)
            raise _to_api_error(e) from None

    async def play(self, track_id: str) -> bool:
        """Start playing a YouTube video via browser source.

        Note: This provider integrates with browser source (iframe) rather than
        direct playback. This method updates the internal state and returns the
        track information for the browser source to handle.

        Args:
            track_id: YouTube video ID or URL

        Returns:
            True if playback state updated successfully

        Raises:
            YouTubeAPIError: If unable to retrieve video information
        """
        track = await self.get_track(track_id)
        if not track:
            logger.warning(f"Cannot play - video not found: {track_id}")
            return False

        self._current_track = track
        self._is_playing = True
        logger.info(f"Started playback: {track.name}")
        return True

    async def pause(self) -> bool:
        """Pause the currently playing track.

        Note: Actual pause control is handled by the browser source.
        This method updates the internal state.

        Returns:
            True if pause state updated successfully
        """
        if not self._is_playing:
            logger.debug("Cannot pause - nothing is playing")
            return False

        self._is_playing = False
        logger.info("Playback paused")
        return True

    async def resume(self) -> bool:
        """Resume playback of the currently paused track.

        Note: Actual resume control is handled by the browser source.
        This method updates the internal state.

        Returns:
            True if resume state updated successfully
        """
        if self._is_playing:
            logger.debug("Cannot resume - already playing")
            return False

        if not self._current_track:
            logger.debug("Cannot resume - no track loaded")
            return False

        self._is_playing = True
        logger.info("Playback resumed")
        return True

    async def skip(self) -> bool:
        """Skip to the next track in the queue.

        Note: Queue management is handled by the unified music module.
        This method clears the current track state.

        Returns:
            True if skip was successful
        """
        if not self._current_track:
            logger.debug("Cannot skip - no track loaded")
            return False

        logger.info(f"Skipped track: {self._current_track.name}")
        self._current_track = None
        self._is_playing = False
        return True

    async def get_now_playing(self) -> Optional[MusicTrack]:
        """Get information about the currently playing track.

        Returns:
            MusicTrack object for current track, or None if nothing is playing
        """
        if self._is_playing and self._current_track:
            return self._current_track
        return None

    async def is_authenticated(self) -> bool:
        """Check if the provider is authenticated.

        Returns:
            True if API key is configured
        """
        return bool(self.api_key)

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate with YouTube.

        For API key authentication, credentials should contain:
        - api_key: YouTube Data API v3 key

        For OAuth2 (future implementation), credentials should contain:
        - client_id: OAuth2 client ID
        - client_secret: OAuth2 client secret
        - access_token: OAuth2 access token (optional)
        - refresh_token: OAuth2 refresh token (optional)

        Args:
            credentials: Dictionary containing authentication credentials

        Returns:
            True if authentication successful
        """
        # API key authentication
        if "api_key" in credentials:
            self.api_key = credentials["api_key"]
            logger.info("YouTube API key configured")

        # OAuth2 credentials (for future implementation)
        if "client_id" in credentials:
            self.client_id = credentials["client_id"]

        if "client_secret" in credentials:
            self.client_secret = credentials["client_secret"]

        # Verify authentication with a simple API call
        if self.api_key:
            try:
                # Test API key with a simple search
                await self.search("test", limit=1)
                self._authenticated = True
                logger.info("YouTube authentication successful")
                return True
            except Exception as e:
                logger.error("YouTube authentication failed: %s", _describe_error(e))
                return False

        return False

    async def health_check(self) -> bool:
        """Check if YouTube API is accessible.

        Returns:
            True if the service is healthy
        """
        if not self.api_key:
            logger.warning("YouTube health check failed - no API key")
            return False

        try:
            # Perform a minimal API request to check connectivity
            client = await self._get_http_client()
            videos_url = f"{self.YOUTUBE_API_BASE}/videos"
            params = {
                "part": "id",
                "id": "dQw4w9WgXcQ",  # Known valid video ID
                "key": self.api_key,
            }

            response = await client.get(videos_url, params=params)
            response.raise_for_status()

            logger.info("YouTube health check passed")
            return True

        except Exception as e:
            logger.error("YouTube health check failed: %s", _describe_error(e))
            return False
