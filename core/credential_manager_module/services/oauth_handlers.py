"""OAuth handlers for platform-specific token refresh.

Provides abstract handler interface and concrete implementations for:
- Twitch
- Discord
- Slack
- YouTube/Google
- Spotify
- Kick
"""

from __future__ import annotations

import base64
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from flask_core.safe_logging import describe_exc

logger = logging.getLogger(__name__)

_SLACK_ERROR_RE = re.compile(r"[a-z0-9_]{1,64}")


class OAuthRefreshError(Exception):
    """Base exception for OAuth refresh failures.

    SECURITY: the message is always built from `describe_exc()` (exception type,
    HTTP status, fixed category) or a fixed string -- never from the underlying
    exception's text, which can carry the request URL/body (`client_secret`,
    `refresh_token`). Callers log `str(exc)` of this type freely.
    """

    pass


def _wrap_refresh_failure(platform: str, exc: Exception) -> OAuthRefreshError:
    """Log a redacted `platform` refresh failure and return the error to raise.

    Replaces the old `f"...{str(e)}"` pattern: `str(e)` of an HTTP/JSON error can
    embed the request URL or body, which contains the OAuth `client_secret` and
    `refresh_token`. Only type/code/category reach the log and the new message.
    """
    detail = describe_exc(exc)
    logger.error("%s token refresh failed: %s", platform, detail)
    return OAuthRefreshError(f"{platform} refresh failed: {detail}")


class BaseOAuthHandler(ABC):
    """Abstract base class for OAuth handlers."""

    TIMEOUT = 10  # seconds

    @abstractmethod
    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh OAuth token.

        Args:
            refresh_token: Refresh token to use.
            client_id: OAuth client ID.
            client_secret: OAuth client secret.
            config_data: Platform-specific configuration.

        Returns:
            Dictionary with:
            - access_token: New access token
            - refresh_token: New refresh token (if provided)
            - expires_in: Token lifetime in seconds
            - token_type: Token type (usually 'Bearer')
            - scope: Space-separated scopes or comma-separated list

        Raises:
            OAuthRefreshError: If refresh fails.
        """
        pass

    async def _post_form(
        self, url: str, data: dict, headers: Optional[dict] = None
    ) -> dict:
        """Make async HTTP POST request with form-encoded body.

        Args:
            url: Endpoint URL.
            data: Form data to send.
            headers: Optional custom headers.

        Returns:
            Parsed JSON response.

        Raises:
            OAuthRefreshError: On network or HTTP error.
        """
        if headers is None:
            headers = {}

        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(url, data=data, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            # `from None`: the cause (httpx error) carries the request URL/body;
            # keep it out of any rendered traceback chain.
            raise OAuthRefreshError(f"HTTP request failed: {describe_exc(e)}") from None
        except Exception as e:
            raise OAuthRefreshError(f"Request error: {describe_exc(e)}") from None


def _slack_error_code(value: object) -> str:
    """Return Slack's snake_case `error` code, or a fixed label if it is anything else.

    The code is remote-controlled response data that ends up in `OAuthRefreshError`
    (and so in logs); only a short lowercase identifier is passed through.
    """
    if isinstance(value, str) and _SLACK_ERROR_RE.fullmatch(value):
        return value
    return "Unknown error"


class TwitchOAuthHandler(BaseOAuthHandler):
    """Twitch OAuth token refresh handler."""

    TOKEN_URL = "https://id.twitch.tv/oauth2/token"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh Twitch token."""
        try:
            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            return {
                "access_token": response.get("access_token"),
                "refresh_token": response.get("refresh_token", refresh_token),
                "expires_in": response.get("expires_in", 3600),
                "token_type": response.get("token_type", "Bearer"),
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("Twitch", e) from None


class DiscordOAuthHandler(BaseOAuthHandler):
    """Discord OAuth token refresh handler."""

    TOKEN_URL = "https://discord.com/api/v10/oauth2/token"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh Discord token."""
        try:
            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            return {
                "access_token": response.get("access_token"),
                "refresh_token": response.get("refresh_token", refresh_token),
                "expires_in": response.get("expires_in", 604800),
                "token_type": response.get("token_type", "Bearer"),
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("Discord", e) from None


class SlackOAuthHandler(BaseOAuthHandler):
    """Slack OAuth token refresh handler."""

    TOKEN_URL = "https://slack.com/api/oauth.v2.access"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh Slack token."""
        try:
            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            if not response.get("ok"):
                raise OAuthRefreshError(_slack_error_code(response.get("error")))

            return {
                "access_token": response.get("access_token"),
                "refresh_token": response.get("refresh_token", refresh_token),
                "expires_in": response.get("expires_in", 43200),
                "token_type": "Bearer",
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("Slack", e) from None


class YouTubeOAuthHandler(BaseOAuthHandler):
    """YouTube/Google OAuth token refresh handler."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh YouTube/Google token."""
        try:
            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            return {
                "access_token": response.get("access_token"),
                "refresh_token": refresh_token,  # Google doesn't return new refresh token
                "expires_in": response.get("expires_in", 3600),
                "token_type": response.get("token_type", "Bearer"),
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("YouTube", e) from None


class SpotifyOAuthHandler(BaseOAuthHandler):
    """Spotify OAuth token refresh handler."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh Spotify token."""
        try:
            # Spotify requires Basic auth
            auth_str = f"{client_id}:{client_secret}"
            auth_bytes = auth_str.encode("utf-8")
            auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")

            headers = {
                "Authorization": f"Basic {auth_base64}",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers=headers,
            )

            return {
                "access_token": response.get("access_token"),
                "refresh_token": response.get("refresh_token", refresh_token),
                "expires_in": response.get("expires_in", 3600),
                "token_type": response.get("token_type", "Bearer"),
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("Spotify", e) from None


class KickOAuthHandler(BaseOAuthHandler):
    """Kick OAuth token refresh handler."""

    TOKEN_URL = "https://id.kick.com/oauth/token"

    async def refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        config_data: Optional[dict] = None,
    ) -> dict:
        """Refresh Kick token."""
        try:
            response = await self._post_form(
                self.TOKEN_URL,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            return {
                "access_token": response.get("access_token"),
                "refresh_token": response.get("refresh_token", refresh_token),
                "expires_in": response.get("expires_in", 3600),
                "token_type": response.get("token_type", "Bearer"),
                "scope": response.get("scope", ""),
            }
        except OAuthRefreshError:
            raise
        except Exception as e:
            raise _wrap_refresh_failure("Kick", e) from None


def get_handler(platform: str) -> BaseOAuthHandler:
    """Get OAuth handler for platform.

    Args:
        platform: Platform name (twitch, discord, slack, youtube, spotify, kick).

    Returns:
        OAuth handler instance.

    Raises:
        ValueError: If platform not supported.
    """
    handlers: dict[str, BaseOAuthHandler] = {
        "twitch": TwitchOAuthHandler(),
        "discord": DiscordOAuthHandler(),
        "slack": SlackOAuthHandler(),
        "youtube": YouTubeOAuthHandler(),
        "spotify": SpotifyOAuthHandler(),
        "kick": KickOAuthHandler(),
    }

    if platform not in handlers:
        raise ValueError(f"Unsupported platform: {platform}")

    return handlers[platform]
