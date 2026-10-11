"""
Spotify music provider implementation.

This module implements the BaseMusicProvider interface for Spotify Web API.
Supports OAuth2 authentication, playback control, search, and playlist management.
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

# httpx logs every request URL at INFO -- ``q=`` search text and device ids in the query,
# caller-supplied track/playlist ids in the path. Pin the httpx logger to WARNING (and strip
# the query as defence in depth) so none of it reaches the log stream. Idempotent.
install_httpx_log_guards()


class SpotifyAuthError(Exception):
    """Raised when Spotify authentication fails."""
    pass


class SpotifyAPIError(Exception):
    """Raised when Spotify API requests fail."""
    pass


# Provider-owned exceptions: their messages are static or built from `describe_error`,
# so `describe_error` may carry their text through (httpx errors never are).
_OWN_ERRORS = (SpotifyAuthError, SpotifyAPIError)


def _describe(exc: BaseException) -> str:
    """Describe `exc` for a log line / raised error: type, status, reason -- never its text."""
    return describe_error(exc, own_errors=_OWN_ERRORS)


class SpotifyProvider(BaseMusicProvider):
    """Spotify music provider implementation.

    This provider implements the BaseMusicProvider interface for Spotify Web API.
    It handles OAuth2 authentication, token refresh, and all playback operations.

    Environment variables required:
        SPOTIFY_CLIENT_ID: Your Spotify application client ID
        SPOTIFY_CLIENT_SECRET: Your Spotify application client secret
        SPOTIFY_REDIRECT_URI: OAuth2 redirect URI configured in your app

    Attributes:
        PROVIDER_NAME: The name identifier for this provider ('spotify')
    """

    PROVIDER_NAME = "spotify"

    # Spotify API endpoints
    API_BASE_URL = "https://api.spotify.com/v1"
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    # OAuth scopes needed for full functionality
    SCOPES = [
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-currently-playing",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-library-read",
    ]

    def __init__(self):
        """Initialize the Spotify provider.

        Reads configuration from environment variables and sets up the HTTP client.

        Raises:
            ValueError: If required environment variables are missing.
        """
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI")

        if not all([self.client_id, self.client_secret, self.redirect_uri]):
            raise ValueError(
                "Missing required environment variables: "
                "SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI"
            )

        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        self.active_device_id: Optional[str] = None

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
            Token response containing access_token, refresh_token, and expires_in.

        Raises:
            SpotifyAuthError: If token exchange fails.
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
            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            return token_data
        except httpx.HTTPStatusError as e:
            desc = _describe(e)
            logger.error("Spotify token exchange failed: %s", desc)
            raise SpotifyAuthError(f"Failed to exchange code: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during Spotify token exchange: %s", desc)
            raise SpotifyAuthError(f"Token exchange error: {desc}") from None

    async def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns:
            True if token refresh was successful, False otherwise.

        Raises:
            SpotifyAuthError: If token refresh fails and no refresh_token is available.
        """
        if not self.refresh_token:
            logger.error("No refresh token available")
            raise SpotifyAuthError("No refresh token available")

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

            expires_in = token_data.get("expires_in", 3600)
            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

            logger.info("Access token refreshed successfully")
            return True
        except httpx.HTTPStatusError as e:
            desc = _describe(e)
            logger.error("Spotify token refresh failed: %s", desc)
            raise SpotifyAuthError(f"Failed to refresh token: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during Spotify token refresh: %s", desc)
            raise SpotifyAuthError(f"Token refresh error: {desc}") from None

    async def _ensure_valid_token(self):
        """Ensure we have a valid access token, refreshing if necessary.

        Raises:
            SpotifyAuthError: If not authenticated or token refresh fails.
        """
        if not self.access_token:
            raise SpotifyAuthError("Not authenticated. Call authenticate() first.")

        # Refresh if token expires in less than 5 minutes
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
        """Make an authenticated request to the Spotify API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (without base URL)
            params: Query parameters
            json_data: JSON body data
            retry_auth: Whether to retry once after refreshing token

        Returns:
            JSON response data, or None for 204 responses

        Raises:
            SpotifyAPIError: If the API request fails
        """
        await self._ensure_valid_token()

        url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            response = await self.http_client.request(
                method=method,
                url=url,
                headers=headers,
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
            logger.error("Spotify API error: %s", desc)
            raise SpotifyAPIError(f"API request failed: {desc}") from None
        except Exception as e:
            desc = _describe(e)
            logger.error("Unexpected error during Spotify API request: %s", desc)
            raise SpotifyAPIError(f"Request error: {desc}") from None

    @staticmethod
    def parse_track_url(url: str) -> Optional[str]:
        """Extract track ID from a Spotify URL.

        Supports formats like:
        - https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp
        - https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp?si=...
        - spotify:track:3n3Ppam7vgaVa1iaRUc9Lp

        Args:
            url: The Spotify URL or URI

        Returns:
            The track ID if found, None otherwise
        """
        # Match https://open.spotify.com/track/TRACK_ID
        match = re.search(r'open\.spotify\.com/track/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)

        # Match spotify:track:TRACK_ID
        match = re.search(r'spotify:track:([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)

        return None

    def _convert_track(self, track_data: Dict[str, Any]) -> MusicTrack:
        """Convert Spotify track data to MusicTrack object.

        Args:
            track_data: Raw track data from Spotify API

        Returns:
            MusicTrack object
        """
        artists = ", ".join([artist["name"] for artist in track_data.get("artists", [])])
        album = track_data.get("album", {})
        images = album.get("images", [])
        album_art_url = images[0]["url"] if images else ""

        return MusicTrack(
            track_id=track_data["id"],
            name=track_data["name"],
            artist=artists,
            album=album.get("name", "Unknown Album"),
            album_art_url=album_art_url,
            duration_ms=track_data.get("duration_ms", 0),
            provider=self.PROVIDER_NAME,
            uri=track_data["uri"],
            metadata={
                "popularity": track_data.get("popularity"),
                "explicit": track_data.get("explicit"),
                "external_urls": track_data.get("external_urls", {}),
            },
        )

    # BaseMusicProvider interface implementation

    async def search(self, query: str, limit: int = 10) -> List[MusicTrack]:
        """Search for tracks matching the query.

        Args:
            query: The search query string
            limit: Maximum number of results (1-50)

        Returns:
            List of MusicTrack objects matching the search
        """
        limit = max(1, min(50, limit))  # Clamp to Spotify's limits

        params = {
            "q": query,
            "type": "track",
            "limit": limit,
        }

        result = await self._make_request("GET", "/search", params=params)
        tracks = result.get("tracks", {}).get("items", [])

        return [self._convert_track(track) for track in tracks]

    async def get_track(self, track_id: str) -> Optional[MusicTrack]:
        """Get track information by track ID.

        Args:
            track_id: The Spotify track ID

        Returns:
            MusicTrack object if found, None otherwise
        """
        try:
            result = await self._make_request("GET", f"/tracks/{track_id}")
            return self._convert_track(result)
        except SpotifyAPIError:
            return None

    async def play(self, track_id: str) -> bool:
        """Start playing a track.

        Args:
            track_id: The Spotify track ID

        Returns:
            True if playback started successfully
        """
        json_data = {
            "uris": [f"spotify:track:{track_id}"]
        }

        # Include device_id if one is set
        params = {}
        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("PUT", "/me/player/play", params=params, json_data=json_data)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to start playback: %s", desc)
            return False

    async def pause(self) -> bool:
        """Pause the currently playing track.

        Returns:
            True if pause was successful
        """
        params = {}
        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("PUT", "/me/player/pause", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to pause: %s", desc)
            return False

    async def resume(self) -> bool:
        """Resume playback of the currently paused track.

        Returns:
            True if resume was successful
        """
        params = {}
        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("PUT", "/me/player/play", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to resume: %s", desc)
            return False

    async def skip(self) -> bool:
        """Skip to the next track in the queue.

        Returns:
            True if skip was successful
        """
        params = {}
        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("POST", "/me/player/next", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to skip: %s", desc)
            return False

    async def get_now_playing(self) -> Optional[MusicTrack]:
        """Get information about the currently playing track.

        Returns:
            MusicTrack object for the current track, or None if nothing is playing
        """
        try:
            result = await self._make_request("GET", "/me/player/currently-playing")

            if not result or not result.get("item"):
                return None

            return self._convert_track(result["item"])
        except SpotifyAPIError:
            return None

    async def is_authenticated(self) -> bool:
        """Check if the provider is authenticated.

        Returns:
            True if authenticated with valid token
        """
        if not self.access_token:
            return False

        # Check if token is expired
        if self.token_expires_at and datetime.now() >= self.token_expires_at:
            # Try to refresh
            try:
                await self.refresh_access_token()
                return True
            except SpotifyAuthError:
                return False

        return True

    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """Authenticate with the music provider.

        Args:
            credentials: Dictionary containing either:
                - 'code': Authorization code to exchange for tokens
                - 'access_token' and optionally 'refresh_token': Pre-existing tokens

        Returns:
            True if authentication was successful
        """
        try:
            if "code" in credentials:
                # Exchange authorization code for tokens
                await self.exchange_code(credentials["code"])
                return True
            elif "access_token" in credentials:
                # Use provided tokens directly
                self.access_token = credentials["access_token"]
                self.refresh_token = credentials.get("refresh_token")

                # Calculate expiry time if provided
                expires_in = credentials.get("expires_in", 3600)
                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)

                return True
            else:
                logger.error("Invalid credentials format")
                return False
        except Exception as e:
            desc = _describe(e)
            logger.error("Spotify authentication failed: %s", desc)
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
            logger.error("Spotify health check failed: %s", desc)
            return False

    # Additional Spotify-specific methods

    async def get_devices(self) -> List[Dict[str, Any]]:
        """Get list of available Spotify devices.

        Returns:
            List of device dictionaries with id, name, type, is_active, etc.
        """
        result = await self._make_request("GET", "/me/player/devices")
        return result.get("devices", [])

    async def set_device(self, device_id: str) -> bool:
        """Set the active playback device.

        Args:
            device_id: The Spotify device ID to use for playback

        Returns:
            True if device was set successfully
        """
        json_data = {
            "device_ids": [device_id],
            "play": False,  # Don't automatically start playback
        }

        try:
            await self._make_request("PUT", "/me/player", json_data=json_data)
            self.active_device_id = device_id
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to set device: %s", desc)
            return False

    async def get_playlists(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get user's playlists.

        Args:
            limit: Maximum number of playlists to return (1-50)

        Returns:
            List of playlist dictionaries with id, name, description, etc.
        """
        limit = max(1, min(50, limit))
        params = {"limit": limit}

        result = await self._make_request("GET", "/me/playlists", params=params)
        return result.get("items", [])

    async def get_playlist_tracks(
        self, playlist_id: str, limit: int = 100
    ) -> List[MusicTrack]:
        """Get tracks from a playlist.

        Args:
            playlist_id: The Spotify playlist ID
            limit: Maximum number of tracks to return (1-100)

        Returns:
            List of MusicTrack objects from the playlist
        """
        limit = max(1, min(100, limit))
        params = {"limit": limit}

        result = await self._make_request(
            "GET", f"/playlists/{playlist_id}/tracks", params=params
        )

        items = result.get("items", [])
        tracks = []

        for item in items:
            track_data = item.get("track")
            if track_data:
                tracks.append(self._convert_track(track_data))

        return tracks

    async def add_to_queue(self, track_id: str) -> bool:
        """Add a track to the playback queue.

        Args:
            track_id: The Spotify track ID

        Returns:
            True if track was added successfully
        """
        params = {"uri": f"spotify:track:{track_id}"}

        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("POST", "/me/player/queue", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to add to queue: %s", desc)
            return False

    async def get_playback_state(self) -> Optional[Dict[str, Any]]:
        """Get the current playback state.

        Returns:
            Dictionary with playback state including device, track, progress, etc.
        """
        try:
            result = await self._make_request("GET", "/me/player")
            return result
        except SpotifyAPIError:
            return None

    async def set_volume(self, volume_percent: int) -> bool:
        """Set the playback volume.

        Args:
            volume_percent: Volume level (0-100)

        Returns:
            True if volume was set successfully
        """
        volume_percent = max(0, min(100, volume_percent))
        params = {"volume_percent": volume_percent}

        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("PUT", "/me/player/volume", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to set volume: %s", desc)
            return False

    async def seek(self, position_ms: int) -> bool:
        """Seek to a position in the currently playing track.

        Args:
            position_ms: Position in milliseconds

        Returns:
            True if seek was successful
        """
        params = {"position_ms": position_ms}

        if self.active_device_id:
            params["device_id"] = self.active_device_id

        try:
            await self._make_request("PUT", "/me/player/seek", params=params)
            return True
        except SpotifyAPIError as e:
            desc = _describe(e)
            logger.error("Failed to seek: %s", desc)
            return False
