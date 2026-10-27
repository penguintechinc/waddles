"""
SoundCloud music provider implementation.

This module implements the BaseMusicProvider interface for SoundCloud API.
Supports OAuth2 authentication, playback control, search, and stream URL handling.
"""

import os
import re
import logging
from typing import Any, Dict, Optional, List
from datetime import datetime, timedelta
from urllib.parse import urlencode
import asyncio

import httpx

from ._log_safety import describe_error, install_httpx_log_guards
from .base_provider import BaseMusicProvider, MusicTrack


logger = logging.getLogger(__name__)

# httpx logs every request URL at INFO. SoundCloud authenticates with ``oauth_token=`` in
# the query string, search text rides in ``q=`` and user ids sit in the path: pin the httpx
# logger to WARNING (and strip the query as defence in depth) so none of it reaches the
# log stream. Idempotent.
install_httpx_log_guards()


class SoundCloudAuthError(Exception):
    """Raised when SoundCloud authentication fails."""
    pass


class SoundCloudAPIError(Exception):
    """Raised when SoundCloud API requests fail."""
    pass


# Provider-owned exceptions: their messages are static or built from `describe_error`,
# so `describe_error` may carry their text through (httpx errors never are).
_OWN_ERRORS = (SoundCloudAuthError, SoundCloudAPIError)


def _describe(exc: BaseException) -> str:
    """Describe `exc` for a log line / raised error: type, status, reason -- never its text."""
    return describe_error(exc, own_errors=_OWN_ERRORS)


class SoundCloudProvider(BaseMusicProvider):
    """SoundCloud music provider implementation.

    This provider implements the BaseMusicProvider interface for SoundCloud API.
    It handles OAuth2 authentication, token management, and all playback operations.

    Environment variables required:
        SOUNDCLOUD_CLIENT_ID: Your SoundCloud application client ID
        SOUNDCLOUD_CLIENT_SECRET: Your SoundCloud application client secret
        SOUNDCLOUD_REDIRECT_URI: OAuth2 redirect URI configured in your app

    Attributes:
        PROVIDER_NAME: The name identifier for this provider ('soundcloud')
    """

    PROVIDER_NAME = "soundcloud"

    # SoundCloud API endpoints
    API_BASE_URL = "https://api.soundcloud.com"
    AUTH_URL = "https://soundcloud.com/oauth/authorize"
    TOKEN_URL = "https://soundcloud.com/oauth/token"

    # OAuth scopes needed for full functionality
    SCOPES = [
        "non-expiring",
    ]

    def __init__(self):
        """Initialize the SoundCloud provider.

        Reads configuration from environment variables and sets up the HTTP client.

        Raises:
            ValueError: If required environment variables are missing.
        """
        self.client_id = os.getenv("SOUNDCLOUD_CLIENT_ID")
        self.client_secret = os.getenv("SOUNDCLOUD_CLIENT_SECRET")
        self.redirect_uri = os.getenv("SOUNDCLOUD_REDIRECT_URI")

        if not all([self.client_id, self.client_secret, self.redirect_uri]):
            raise ValueError(
                "Missing required environment variables: "
                "SOUNDCLOUD_CLIENT_ID, SOUNDCLOUD_CLIENT_SECRET, SOUNDCLOUD_REDIRECT_URI"
            )

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        self.user_id: Optional[str] = None
        self.current_queue: List[MusicTrack] = []
        self.current_track_index: int = 0
        self.is_playing: bool = False

        # Create async HTTP client
        self.http_client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        """Close the HTTP client. Call this when shutting down."""
        await self.http_client.aclose()

    def get_auth_url(self, state: Optional[str] = None) -> str:
        """Generate the OAuth2 authorization URL.

        Args:
            state: Optional state parameter for CSRF protection.

        Returns:
            The authorization URL to redirect users to.
        """
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.SCOPES),
        }

        if state:
            params["state"] = state

        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for access and refresh tokens.

        Args:
            code: The authorization code received from OAuth2 callback.

        Returns:
            Token response containing access_token and optionally refresh_token.

        Raises:
            SoundCloudAuthError: If token exchange fails.
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            response = await self.http_client.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_data = response.json()

            # Store tokens
            self.access_token = token_data["access_token"]
            self.refresh_token = token_data.get("refresh_token")

            # SoundCloud tokens may not expire (non-expiring scope)
            expires_in = token_data.get("expires_in")
            if expires_in:
                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            return token_data
        except httpx.HTTPStatusError as e:
            desc = _describe(e)
            logger.error("SoundCloud token exchange failed: %s", desc)
            raise SoundCloudAuthError(f"Failed to exchange code: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during SoundCloud token exchange: %s", desc)
            raise SoundCloudAuthError(f"Token exchange error: {desc}") from None

    async def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns:
            True if token refresh was successful, False otherwise.

        Raises:
            SoundCloudAuthError: If token refresh fails and no refresh_token is available.
        """
        if not self.refresh_token:
            logger.warning("No refresh token available, SoundCloud token may be non-expiring")
            return True

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            response = await self.http_client.post(self.TOKEN_URL, data=data)
            response.raise_for_status()
            token_data = response.json()

            self.access_token = token_data["access_token"]
            # Refresh token might be rotated
            if "refresh_token" in token_data:
                self.refresh_token = token_data["refresh_token"]

            expires_in = token_data.get("expires_in")
            if expires_in:
                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            logger.info("Access token refreshed successfully")
            return True
        except httpx.HTTPStatusError as e:
            desc = _describe(e)
            logger.error("SoundCloud token refresh failed: %s", desc)
            raise SoundCloudAuthError(f"Failed to refresh token: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during SoundCloud token refresh: %s", desc)
            raise SoundCloudAuthError(f"Token refresh error: {desc}") from None

    async def _ensure_valid_token(self):
        """Ensure we have a valid access token, refreshing if necessary.

        Raises:
            SoundCloudAuthError: If not authenticated or token refresh fails.
        """
        if not self.access_token:
            raise SoundCloudAuthError("Not authenticated. Call authenticate() first.")

        # Refresh if token expires in less than 5 minutes (if expiration is set)
        if self.token_expires_at and datetime.now() >= self.token_expires_at - timedelta(minutes=5):
            await self.refresh_access_token()

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Make an authenticated request to the SoundCloud API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (without base URL)
            params: Query parameters
            json_data: JSON body data
            retry_auth: Whether to retry once after refreshing token

        Returns:
            JSON response data, or None for 204 responses

        Raises:
            SoundCloudAPIError: If the API request fails
        """
        await self._ensure_valid_token()

        url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"

        # Add OAuth token to parameters
        if params is None:
            params = {}
        params["oauth_token"] = self.access_token

        try:
            response = await self.http_client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
            )

            # Handle 401 by refreshing token and retrying once
            if response.status_code == 401 and retry_auth:
                logger.info("Received 401, refreshing token and retrying")
                await self.refresh_access_token()
                return await self._make_request(
                    method, endpoint, params, json_data, retry_auth=False
                )

            response.raise_for_status()

            # Some endpoints return 204 No Content
            if response.status_code == 204:
                return None

            return response.json()
        except httpx.HTTPStatusError as e:
            desc = _describe(e)
            logger.error("SoundCloud API error: %s", desc)
            raise SoundCloudAPIError(f"API request failed: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during SoundCloud API request: %s", desc)
            raise SoundCloudAPIError(f"Request error: {desc}") from None

    @staticmethod
    def parse_track_url(url: str) -> Optional[str]:
        """Extract track ID from a SoundCloud URL.

        Supports formats like:
        - https://soundcloud.com/artist/track-name
        - https://soundcloud.com/artist/sets/playlist-name
        - soundcloud://users:123/sounds:456

        Args:
            url: The SoundCloud URL or URI

        Returns:
            The track URL or ID if found, None otherwise
        """
        # Match https://soundcloud.com/artist/track-name (full URL)
        match = re.search(r'soundcloud\.com/([^/]+)/([^/?]+)', url)
        if match:
            return url.split('?')[0]  # Remove query params

        # Match soundcloud:// URIs
        if url.startswith('soundcloud://'):
            return url

        return None

    def _convert_track(self, track_data: Dict[str, Any]) -> MusicTrack:
        """Convert SoundCloud track data to MusicTrack object.

        Args:
            track_data: Raw track data from SoundCloud API

        Returns:
            MusicTrack object
        """
        user = track_data.get("user", {})
        artwork_url = track_data.get("artwork_url", "")

        # Use high resolution artwork if available
        if artwork_url:
            artwork_url = artwork_url.replace("-large.jpg", "-t500x500.jpg")

        return MusicTrack(
            track_id=str(track_data["id"]),
            name=track_data.get("title", "Unknown Title"),
            artist=user.get("username", "Unknown Artist"),
            album=user.get("username", "Unknown Artist"),
            album_art_url=artwork_url,
            duration_ms=track_data.get("duration", 0),
            provider=self.PROVIDER_NAME,
            uri=track_data.get("uri", f"soundcloud://tracks:{track_data['id']}"),
            metadata={
                "url": track_data.get("permalink_url"),
                "stream_url": track_data.get("stream_url"),
                "playback_count": track_data.get("playback_count", 0),
                "likes_count": track_data.get("likes_count", 0),
                "description": track_data.get("description", ""),
                "genre": track_data.get("genre", ""),
                "downloadable": track_data.get("downloadable", False),
                "user_id": user.get("id"),
            },
        )

    async def _get_stream_url(self, track_id: str) -> Optional[str]:
        """Get the stream URL for a track.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            The stream URL if available, None otherwise
        """
        try:
            result = await self._make_request("GET", f"/tracks/{track_id}")
            if result:
                stream_url = result.get("stream_url")
                if stream_url:
                    # Add OAuth token to stream URL
                    separator = "&" if "?" in stream_url else "?"
                    return f"{stream_url}{separator}oauth_token={self.access_token}"
            return None
        except SoundCloudAPIError:
            return None

    # BaseMusicProvider interface implementation

    async def search(self, query: str, limit: int = 10) -> List[MusicTrack]:
        """Search for tracks matching the query.

        Args:
            query: The search query string
            limit: Maximum number of results (1-200)

        Returns:
            List of MusicTrack objects matching the search
        """
        limit = max(1, min(200, limit))  # Clamp to SoundCloud's limits

        params = {
            "q": query,
            "kind": "tracks",
            "limit": limit,
        }

        try:
            result = await self._make_request("GET", "/tracks", params=params)
            if not result:
                return []

            # SoundCloud returns list directly for search
            tracks = result if isinstance(result, list) else result.get("items", [])
            return [self._convert_track(track) for track in tracks]
        except SoundCloudAPIError:
            return []

    async def get_track(self, track_id: str) -> Optional[MusicTrack]:
        """Get track information by track ID.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            MusicTrack object if found, None otherwise
        """
        try:
            result = await self._make_request("GET", f"/tracks/{track_id}")
            if result:
                return self._convert_track(result)
            return None
        except SoundCloudAPIError:
            return None

    async def play(self, track_id: str) -> bool:
        """Start playing a track.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            True if playback started successfully
        """
        try:
            # Get track information first
            track = await self.get_track(track_id)
            if not track:
                logger.error("Track not found")
                return False

            # Get stream URL
            stream_url = await self._get_stream_url(track_id)
            if not stream_url:
                logger.error("Could not get stream URL for track")
                return False

            # Clear queue and add track
            self.current_queue = [track]
            self.current_track_index = 0
            self.is_playing = True

            logger.info("Started playing track (queue_len=%d)", len(self.current_queue))
            return True
        except Exception as e:
            desc = _describe(e)
            logger.error("Failed to start playback: %s", desc)
            return False

    async def pause(self) -> bool:
        """Pause the currently playing track.

        Returns:
            True if pause was successful
        """
        if not self.is_playing:
            logger.warning("No track is currently playing")
            return False

        self.is_playing = False
        logger.info("Paused playback")
        return True

    async def resume(self) -> bool:
        """Resume playback of the currently paused track.

        Returns:
            True if resume was successful
        """
        if self.is_playing:
            logger.warning("Already playing")
            return False

        if not self.current_queue or self.current_track_index >= len(self.current_queue):
            logger.error("No track to resume")
            return False

        self.is_playing = True
        logger.info("Resumed playback")
        return True

    async def skip(self) -> bool:
        """Skip to the next track in the queue.

        Returns:
            True if skip was successful
        """
        if not self.current_queue:
            logger.warning("Queue is empty")
            return False

        if self.current_track_index + 1 >= len(self.current_queue):
            logger.info("No more tracks in queue")
            return False

        self.current_track_index += 1
        logger.info("Skipped to track index %d", self.current_track_index)
        return True

    async def get_now_playing(self) -> Optional[MusicTrack]:
        """Get information about the currently playing track.

        Returns:
            MusicTrack object for the current track, or None if nothing is playing
        """
        if not self.is_playing or not self.current_queue:
            return None

        if self.current_track_index < len(self.current_queue):
            return self.current_queue[self.current_track_index]

        return None

    async def is_authenticated(self) -> bool:
        """Check if the provider is authenticated.

        Returns:
            True if authenticated with valid token
        """
        if not self.access_token:
            return False

        # Check if token is expired (if expiration is set)
        if self.token_expires_at and datetime.now() >= self.token_expires_at:
            # Try to refresh
            try:
                await self.refresh_access_token()
                return True
            except SoundCloudAuthError:
                return False

        return True

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate with the music provider.

        Args:
            credentials: Dictionary containing either:
                - 'code': Authorization code to exchange for tokens
                - 'access_token': Pre-existing OAuth token

        Returns:
            True if authentication was successful
        """
        try:
            if "code" in credentials:
                # Exchange authorization code for tokens
                await self.exchange_code(credentials["code"])

                # Get user info after authentication
                try:
                    user_info = await self._make_request("GET", "/me")
                    if user_info:
                        self.user_id = str(user_info.get("id"))
                except SoundCloudAPIError:
                    logger.warning("Could not fetch user info after authentication")

                return True
            elif "access_token" in credentials:
                # Use provided token directly
                self.access_token = credentials["access_token"]
                self.refresh_token = credentials.get("refresh_token")

                # Calculate expiry time if provided
                expires_in = credentials.get("expires_in")
                if expires_in:
                    self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

                return True
            else:
                logger.error("Invalid credentials format")
                return False
        except Exception as e:
            desc = _describe(e)
            logger.error("SoundCloud authentication failed: %s", desc)
            return False

    async def health_check(self) -> bool:
        """Check if the provider service is healthy and accessible.

        Returns:
            True if the service is healthy
        """
        try:
            # Try to get user profile as a health check
            await self._make_request("GET", "/me")
            return True
        except Exception as e:
            desc = _describe(e)
            logger.error("SoundCloud health check failed: %s", desc)
            return False

    # Additional SoundCloud-specific methods

    async def add_to_queue(self, track_id: str) -> bool:
        """Add a track to the playback queue.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            True if track was added successfully
        """
        try:
            track = await self.get_track(track_id)
            if not track:
                logger.error("Track not found")
                return False

            self.current_queue.append(track)
            logger.info("Added track to queue (queue_len=%d)", len(self.current_queue))
            return True
        except Exception as e:
            desc = _describe(e)
            logger.error("Failed to add to queue: %s", desc)
            return False

    async def get_queue(self) -> List[MusicTrack]:
        """Get the current playback queue.

        Returns:
            List of MusicTrack objects in the queue
        """
        return self.current_queue.copy()

    async def clear_queue(self) -> bool:
        """Clear the playback queue.

        Returns:
            True if queue was cleared successfully
        """
        try:
            self.current_queue.clear()
            self.current_track_index = 0
            self.is_playing = False
            logger.info("Cleared queue")
            return True
        except Exception as e:
            desc = _describe(e)
            logger.error("Failed to clear queue: %s", desc)
            return False

    async def get_stream_url(self, track_id: str) -> Optional[str]:
        """Get the stream URL for a track for direct playback.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            The stream URL if available, None otherwise
        """
        return await self._get_stream_url(track_id)

    async def get_user_profile(self) -> Optional[Dict[str, Any]]:
        """Get the authenticated user's profile information.

        Returns:
            Dictionary with user profile data, or None if not authenticated
        """
        try:
            result = await self._make_request("GET", "/me")
            return result
        except SoundCloudAPIError:
            return None

    async def get_user_likes(self, user_id: Optional[str] = None, limit: int = 50) -> List[MusicTrack]:
        """Get a user's liked tracks.

        Args:
            user_id: The SoundCloud user ID (defaults to authenticated user)
            limit: Maximum number of tracks to return (1-200)

        Returns:
            List of MusicTrack objects that the user has liked
        """
        if not user_id and not self.user_id:
            logger.error("No user ID available")
            return []

        user_id = user_id or self.user_id
        limit = max(1, min(200, limit))

        try:
            params = {"limit": limit}
            result = await self._make_request("GET", f"/users/{user_id}/likes", params=params)

            if not result:
                return []

            # API might return paginated results
            tracks = result if isinstance(result, list) else result.get("items", [])
            converted_tracks = []

            for item in tracks:
                # Handle both direct tracks and wrapped items
                track_data = item.get("track") if isinstance(item, dict) and "track" in item else item
                if track_data:
                    converted_tracks.append(self._convert_track(track_data))

            return converted_tracks
        except SoundCloudAPIError:
            return []

    async def get_user_playlists(self, user_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get a user's playlists.

        Args:
            user_id: The SoundCloud user ID (defaults to authenticated user)
            limit: Maximum number of playlists to return (1-200)

        Returns:
            List of playlist dictionaries with id, title, track_count, etc.
        """
        if not user_id and not self.user_id:
            logger.error("No user ID available")
            return []

        user_id = user_id or self.user_id
        limit = max(1, min(200, limit))

        try:
            params = {"limit": limit}
            result = await self._make_request("GET", f"/users/{user_id}/playlists", params=params)

            if not result:
                return []

            # API might return paginated results
            playlists = result if isinstance(result, list) else result.get("items", [])
            return playlists
        except SoundCloudAPIError:
            return []

    async def get_playlist_tracks(
        self, playlist_id: str, limit: int = 200
    ) -> List[MusicTrack]:
        """Get tracks from a playlist.

        Args:
            playlist_id: The SoundCloud playlist ID
            limit: Maximum number of tracks to return (1-200)

        Returns:
            List of MusicTrack objects from the playlist
        """
        limit = max(1, min(200, limit))

        try:
            params = {"limit": limit}
            result = await self._make_request(
                "GET", f"/playlists/{playlist_id}", params=params
            )

            if not result:
                return []

            tracks = result.get("tracks", [])
            return [self._convert_track(track) for track in tracks if track]
        except SoundCloudAPIError:
            return []

    async def get_track_stream_url(self, track_id: str) -> Optional[str]:
        """Get the stream URL for a track for direct playback.

        This is a convenience alias for get_stream_url.

        Args:
            track_id: The SoundCloud track ID

        Returns:
            The stream URL if available, None otherwise
        """
        return await self._get_stream_url(track_id)

    def get_track_web_url(self, track_permalink: str, artist_username: str) -> str:
        """Build a web URL for a track.

        Args:
            artist_username: The artist's username
            track_permalink: The track's permalink

        Returns:
            The web URL for the track
        """
        return f"https://soundcloud.com/{artist_username}/{track_permalink}"
