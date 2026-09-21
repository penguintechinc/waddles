"""Token refresh service - polls for expiring tokens and refreshes them.

Supports OAuth2 token refresh for:
- Twitch (client_credentials and authorization_code)
- Discord (authorization_code)
- Slack (token rotation)
- YouTube/Google (authorization_code)
- Spotify (authorization_code)
- Kick (authorization_code)

Uses platform-specific OAuth handlers from oauth_handlers module.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import redis.asyncio as aioredis
import httpx

from .oauth_handlers import OAuthRefreshError, get_handler
from .token_crypto import decrypt_if_needed, encrypt_value

logger = logging.getLogger(__name__)


class RefreshService:
    """Background service that refreshes expiring OAuth tokens."""

    __slots__ = (
        "_db_url", "_redis_url", "_redis_prefix", "_refresh_buffer",
        "_poll_interval", "_max_retries", "_retry_backoff_base",
        "_pool", "_redis", "_http", "_task", "_running",
        "_last_cycle", "_total_refreshed", "_total_errors",
    )

    def __init__(
        self,
        database_url: str,
        redis_url: str,
        redis_prefix: str = "credentials:",
        refresh_buffer: int = 300,
        poll_interval: int = 60,
        max_retries: int = 3,
        retry_backoff_base: int = 5,
    ) -> None:
        # Convert pydal-style URL back for asyncpg
        self._db_url = database_url.replace("postgres://", "postgresql://")
        self._redis_url = redis_url
        self._redis_prefix = redis_prefix
        self._refresh_buffer = refresh_buffer
        self._poll_interval = poll_interval
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._pool: Optional[asyncpg.Pool] = None
        self._redis: Optional[aioredis.Redis] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_cycle: Optional[datetime] = None
        self._total_refreshed = 0
        self._total_errors = 0

    async def start(self) -> None:
        """Start the refresh service background loop."""
        self._pool = await asyncpg.create_pool(
            self._db_url, min_size=2, max_size=5
        )
        self._redis = aioredis.from_url(
            self._redis_url, decode_responses=True
        )
        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Refresh service started")

    async def stop(self) -> None:
        """Stop the refresh service gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http:
            await self._http.aclose()
        if self._redis:
            await self._redis.aclose()
        if self._pool:
            await self._pool.close()
        logger.info("Refresh service stopped")

    def get_status(self) -> dict[str, Any]:
        """Return service status."""
        return {
            "running": self._running,
            "last_cycle": (
                self._last_cycle.isoformat() if self._last_cycle else None
            ),
            "total_refreshed": self._total_refreshed,
            "total_errors": self._total_errors,
        }

    async def get_credential_stats(self) -> dict[str, Any]:
        """Get statistics about tracked credentials."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    platform,
                    integration_type,
                    COUNT(*) as total,
                    COUNT(*) FILTER (
                        WHERE expires_at IS NOT NULL
                        AND expires_at < NOW() + INTERVAL '5 minutes'
                    ) as expiring_soon,
                    COUNT(*) FILTER (
                        WHERE expires_at IS NOT NULL
                        AND expires_at < NOW()
                    ) as expired
                FROM platform_integrations
                WHERE is_active = TRUE
                GROUP BY platform, integration_type
                ORDER BY platform, integration_type
            """)

        return [
            {
                "platform": r["platform"],
                "integration_type": r["integration_type"],
                "total": r["total"],
                "expiring_soon": r["expiring_soon"],
                "expired": r["expired"],
            }
            for r in rows
        ]

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                count = await self.run_refresh_cycle()
                if count > 0:
                    logger.info("Refresh cycle: %d tokens refreshed", count)
                self._last_cycle = datetime.now(timezone.utc)
            except Exception:
                logger.exception("Error in refresh cycle")
                self._total_errors += 1

            await asyncio.sleep(self._poll_interval)

    async def run_refresh_cycle(self) -> int:
        """Run one refresh cycle, returning count of refreshed tokens."""
        threshold = datetime.now(timezone.utc) + timedelta(
            seconds=self._refresh_buffer
        )

        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    id, platform, integration_type, community_id,
                    user_id, access_token, refresh_token, client_id,
                    client_secret, token_type, expires_at, scopes,
                    config_data, is_encrypted
                FROM platform_integrations
                WHERE is_active = TRUE
                  AND refresh_token IS NOT NULL
                  AND expires_at IS NOT NULL
                  AND expires_at < $1
                ORDER BY expires_at ASC
                LIMIT 50
            """, threshold)

        refreshed = 0
        for row in rows:
            success = await self._refresh_token(self._decrypt_integration(dict(row)))
            if success:
                refreshed += 1

        self._total_refreshed += refreshed
        return refreshed

    @staticmethod
    def _decrypt_integration(integration: dict) -> dict:
        """Decrypt `access_token`/`refresh_token`/`client_secret` on a freshly-fetched row.

        SECURITY (HIGH): these fields are AES-256-GCM ciphertext at rest
        for every row this service has refreshed since this fix landed
        (`_update_tokens` below sets `is_encrypted = TRUE` on write); rows
        pre-dating the fix carry `is_encrypted = FALSE`/NULL and pass
        through unchanged (`token_crypto.decrypt_if_needed`'s own
        backward-compat contract). The plaintext values live only in this
        in-memory dict for the duration of one refresh attempt -- never
        logged, never re-serialized as-is.
        """
        is_encrypted = bool(integration.get("is_encrypted"))
        for field in ("access_token", "refresh_token", "client_secret"):
            integration[field] = decrypt_if_needed(integration.get(field), is_encrypted=is_encrypted)
        return integration

    async def _refresh_token(self, integration: dict) -> bool:
        """Refresh a single integration's OAuth token."""
        platform = integration["platform"]
        endpoint = PLATFORM_TOKEN_ENDPOINTS.get(platform)

        if not endpoint:
            logger.warning(
                "No token endpoint for platform: %s (id=%s)",
                platform, integration["id"],
            )
            return False

        for attempt in range(self._max_retries):
            try:
                new_tokens = await self._call_refresh_endpoint(
                    platform, endpoint, integration
                )
                if new_tokens:
                    await self._update_tokens(
                        integration["id"], platform, new_tokens
                    )
                    await self._publish_refresh_event(integration, new_tokens)
                    return True

            except Exception:
                wait = self._retry_backoff_base * (2 ** attempt)
                logger.warning(
                    "Refresh attempt %d/%d failed for %s id=%s, "
                    "retrying in %ds",
                    attempt + 1, self._max_retries,
                    platform, integration["id"], wait,
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(wait)

        logger.error(
            "All refresh attempts failed for %s id=%s",
            platform, integration["id"],
        )
        self._total_errors += 1
        return False

    async def _call_refresh_endpoint(
        self,
        platform: str,
        endpoint: str,
        integration: dict,
    ) -> Optional[dict]:
        """Call the platform's OAuth2 token refresh endpoint.

        Uses platform-specific OAuth handlers that implement proper
        authentication patterns for each service.
        """
        try:
            handler = get_handler(platform)
            new_tokens = await handler.refresh_token(
                refresh_token=integration["refresh_token"],
                client_id=integration.get("client_id", ""),
                client_secret=integration.get("client_secret", ""),
                config_data=integration.get("config_data"),
            )
            return new_tokens
        except OAuthRefreshError as e:
            logger.warning(
                "Token refresh failed for %s id=%s: %s",
                platform, integration.get("id"), str(e),
            )
            return None
        except ValueError as e:
            logger.error(
                "Unsupported platform %s: %s",
                platform, str(e),
            )
            return None
        except Exception as e:
            logger.error(
                "Unexpected error refreshing %s token id=%s: %s",
                platform, integration.get("id"), str(e),
            )
            return None

    async def _update_tokens(
        self,
        integration_id: int,
        platform: str,
        new_tokens: dict,
    ) -> None:
        """Update the database with new tokens."""
        expires_at = None
        if new_tokens.get("expires_in"):
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(new_tokens["expires_in"])
            )

        scopes = None
        if new_tokens.get("scope"):
            scope_str = new_tokens["scope"]
            scopes = (
                scope_str.split(" ")
                if isinstance(scope_str, str)
                else scope_str
            )

        # SECURITY (HIGH): encrypt before persisting -- these UPDATE
        # parameters are the plaintext values fetched from the OAuth
        # provider's refresh response; only their ciphertext ever reaches
        # the database. `is_encrypted = TRUE` marks this row for
        # `_decrypt_integration()` on its next read.
        encrypted_access_token = encrypt_value(new_tokens["access_token"])
        encrypted_refresh_token = encrypt_value(new_tokens["refresh_token"])

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE platform_integrations
                SET access_token = $1,
                    refresh_token = $2,
                    token_type = $3,
                    expires_at = $4,
                    scopes = $5,
                    is_encrypted = TRUE,
                    updated_at = NOW()
                WHERE id = $6
                """,
                encrypted_access_token,
                encrypted_refresh_token,
                new_tokens.get("token_type", "Bearer"),
                expires_at,
                scopes,
                integration_id,
            )

        logger.info(
            "Tokens updated for %s integration id=%s (expires=%s)",
            platform, integration_id,
            expires_at.isoformat() if expires_at else "unknown",
        )

    async def _publish_refresh_event(
        self,
        integration: dict,
        new_tokens: dict,
    ) -> None:
        """Publish Redis pub/sub event for credential refresh."""
        platform = integration["platform"]
        integration_type = integration["integration_type"]

        # Build channel name
        channel_parts = [
            f"{self._redis_prefix}{platform}",
            integration_type,
        ]
        if integration.get("community_id"):
            channel_parts.append(str(integration["community_id"]))
        channel = ":".join(channel_parts) + ":refreshed"

        timestamp = datetime.now(timezone.utc).isoformat()
        await self._redis.publish(channel, timestamp)

        logger.debug(
            "Published refresh event on channel: %s", channel
        )
