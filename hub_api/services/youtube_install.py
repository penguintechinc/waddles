"""YouTube OAuth connect flow: authorization code + PKCE, state verified, tokens stored sealed.

The redirect-style install for the ``youtube`` provider
(:mod:`services.provider_connections.youtube`). Two halves, split the way every other OAuth flow
in hub-api splits them:

* :func:`begin_install` -- run by an authenticated community admin (the blueprint has already
  authorised them, resolved the tenant from the JWT and passed the feature gate). Picks the
  tenant's Google OAuth application (per-tenant bot credentials,
  docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md: the global tenant uses the
  platform app from ``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET``; any other tenant brings its
  own, supplied here or stored by an earlier install), mints a single-use CSRF ``state`` plus a PKCE
  verifier, and returns Google's consent URL. The redirect URI is the server-configured callback,
  never a client value (no open redirect).
* :func:`complete_install` -- run by the PUBLIC callback. The browser carries no bearer token, so
  the single-use ``state`` (Redis ``GETDEL``) is the ONLY thing binding the request back to a
  tenant, community and admin; nothing tenant-shaped is ever read from the query string. It
  exchanges the code (with the PKCE verifier), then creates the connection through the framework
  (:meth:`ProviderConnectionService.create`: token verified, channel resolved, tokens sealed) --
  or, when that channel is already connected, rotates the credentials of the community's existing
  connection (the reconnect path after a ``refresh_rejected``).

The PKCE verifier and any tenant-supplied client secret ride in the state sealed
(AES-256-GCM, AAD bound to tenant + community), never plaintext. No code, token, verifier or client
secret is ever logged; failures carry a fixed machine code only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlencode

import httpx
from flask_core.auth import DEFAULT_TENANT_SLUG
from quart import current_app

from config import HubAPIConfig
from services.credential_resolver import (
    DefaultCredentialResolver,
    TransportUnavailable,
    store_tenant_credentials,
)
from services.oauth_connection_state import _redis_client
from services.platform_integrations_crypto import PlatformCredentialCryptoError
from services.provider_connections import ProviderConnectionService
from services.provider_connections import errors as pc_errors
from services.provider_connections.vault import SEAL_AAD, open_secret, seal_secret
from services.provider_connections.youtube import (
    GOOGLE_AUTHORIZE_URL,
    GOOGLE_TOKEN_URL,
    PROVIDER_ID,
    SCOPE_FORCE_SSL,
    AppCredentials,
    AppCredentialsUnavailable,
    guard_google_url,
)

logger = logging.getLogger(__name__)

_STATE_KEY_PREFIX: Final = "oauth:youtube_install:state:"
_DISCOVER_KEY_PREFIX: Final = "youtube:discover:cooldown:"
#: Seconds between two discovery calls for one connection (each costs a YouTube quota unit).
DISCOVER_COOLDOWN_S: Final = 30
_DEFAULT_TTL_S: Final = 600
#: Sanity ceiling on a tenant-supplied OAuth client id/secret (Google's are ~72 / ~35 chars).
MAX_CLIENT_FIELD_LENGTH: Final = 512
_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
_MAX_RESPONSE_BYTES: Final = 65_536
#: Swaps the socket of the code exchange (tests monkeypatch it to an ``httpx.MockTransport``); the
#: host allowlist, SSRF guard, size bound and error mapping still run for real.
_transport: httpx.AsyncBaseTransport | None = None

#: Fixed, safe machine codes the callback page may carry (never provider or user text).
ERR_STATE: Final = "invalid_or_expired_state"
ERR_DENIED: Final = "access_denied"
ERR_NO_CODE: Final = "missing_code"
ERR_APP: Final = "app_not_configured"
ERR_EXCHANGE: Final = "exchange_failed"
ERR_NO_REFRESH: Final = "no_refresh_token"
ERR_SCOPES: Final = "insufficient_scopes"
ERR_NO_CHANNEL: Final = "channel_not_found"
ERR_UNAVAILABLE: Final = "provider_unavailable"
ERR_SAVE: Final = "save_failed"


class YoutubeInstallError(Exception):
    """An install step failed. ``code`` is one of the fixed ``ERR_*`` strings, safe to render."""

    def __init__(self, code: str) -> None:
        """Record the safe machine ``code``."""
        super().__init__(code)
        self.code = code


@dataclass(slots=True, frozen=True)
class InstallState:
    """Decoded, single-use context an install ``state`` token was minted to carry."""

    tenant_id: int
    community_id: int
    user_id: int
    redirect_uri: str
    client_id: str
    code_verifier: str
    #: The OAuth client secret the admin typed (non-global tenant); ``None`` = resolve it again.
    supplied_client_secret: str | None


def _cfg() -> HubAPIConfig:
    return cast(HubAPIConfig, current_app.config["HUB_API_CONFIG"])


def _ttl_seconds() -> int:
    return int(getattr(_cfg(), "connections_state_ttl_s", _DEFAULT_TTL_S))


def callback_redirect_uri() -> str:
    """Fixed, server-configured callback URL -- never client-suppliable (anti open-redirect)."""
    return f"{_cfg().connections_callback_base_url}/api/v1/provider-connections/youtube/callback"


def _state_aad(tenant_id: int, community_id: int) -> bytes:
    """Bind the sealed state blob to the tenant + community it was minted for."""
    return f"youtube_install_state|{tenant_id}|{community_id}".encode()


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 ``S256`` code challenge for ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _is_default_tenant(dal: Any, tenant_id: int) -> bool:
    """Whether ``tenant_id`` is the default/global tenant (the one that uses the env app)."""
    row = dal(dal.tenants.id == tenant_id).select(dal.tenants.slug).first()
    return row is not None and row.slug == DEFAULT_TENANT_SLUG


async def resolve_app_credentials(dal: Any, *, tenant_id: int) -> tuple[str, str]:
    """The tenant's own Google OAuth application ``(client_id, client_secret)``.

    Global tenant -> ``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET`` env; any other tenant -> its
    stored, decrypted ``tenant_platform_apps`` row. A tenant never silently falls back to the global
    application. Raises :class:`AppCredentialsUnavailable` when there is none.
    """
    try:
        resolved = await DefaultCredentialResolver().resolve(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=_is_default_tenant(dal, tenant_id),
            platform=PROVIDER_ID,
        )
    except TransportUnavailable as exc:
        raise AppCredentialsUnavailable("no YouTube OAuth application for this tenant") from exc
    client_id = resolved.payload.get("client_id")
    client_secret = resolved.payload.get("client_secret")
    if not (
        isinstance(client_id, str)
        and client_id
        and isinstance(client_secret, str)
        and client_secret
    ):
        raise AppCredentialsUnavailable("incomplete YouTube OAuth application for this tenant")
    return client_id, client_secret


def make_app_credentials(dal: Any) -> AppCredentials:
    """Adapter-facing resolver bound to ``dal`` (what the YouTube adapter refreshes with)."""

    async def _resolve(tenant_id: int) -> tuple[str, str]:
        return await resolve_app_credentials(dal, tenant_id=tenant_id)

    return _resolve


async def begin_install(
    dal: Any,
    *,
    tenant_id: int,
    is_default_tenant: bool,
    community_id: int,
    user_id: int,
    client_id: str | None,
    client_secret: str | None,
) -> str:
    """Mint state + PKCE and return Google's consent URL for this tenant's OAuth application.

    ``client_id`` / ``client_secret`` are the tenant's own application, supplied together or not at
    all; the global tenant may not supply them (it uses the platform app). Raises
    :class:`YoutubeInstallError` ``app_not_configured`` when no application can be found.
    """
    if (client_id is None) != (client_secret is None):
        raise YoutubeInstallError(ERR_APP)
    if client_id is not None and is_default_tenant:
        raise YoutubeInstallError(ERR_APP)
    if client_id is None:
        try:
            client_id, _ = await resolve_app_credentials(dal, tenant_id=tenant_id)
        except AppCredentialsUnavailable as exc:
            raise YoutubeInstallError(ERR_APP) from exc
    verifier = secrets.token_urlsafe(64)
    redirect_uri = callback_redirect_uri()
    sealed = seal_secret(
        json.dumps({"v": verifier, "s": client_secret}),
        aad=_state_aad(tenant_id, community_id),
    )
    token = secrets.token_urlsafe(32)
    payload = {
        "tenant_id": tenant_id,
        "community_id": community_id,
        "user_id": user_id,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "sealed": sealed,
    }
    await _redis_client().set(f"{_STATE_KEY_PREFIX}{token}", json.dumps(payload), ex=_ttl_seconds())
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE_FORCE_SSL,
        "state": token,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


async def consume_state(state: str) -> InstallState | None:
    """Atomically fetch-and-delete ``state``; ``None`` if absent/expired/replayed/tampered.

    ``GETDEL`` makes it single-use even under concurrent requests. A payload that fails to decode
    or whose sealed blob does not authenticate under its own tenant + community AAD is treated as
    absent (fail closed, no partial result).
    """
    if not state or len(state) > 128:
        return None
    try:
        raw = await _redis_client().getdel(f"{_STATE_KEY_PREFIX}{state}")
    except Exception:  # noqa: BLE001 - a Redis hiccup must fail the flow, not crash it
        logger.exception("youtube_install.state_consume_failed")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        tenant_id = int(data["tenant_id"])
        community_id = int(data["community_id"])
        inner = json.loads(
            open_secret(
                str(data["sealed"]),
                aad=_state_aad(tenant_id, community_id),
                seal_version=SEAL_AAD,
            )
        )
        supplied = inner.get("s")
        return InstallState(
            tenant_id=tenant_id,
            community_id=community_id,
            user_id=int(data["user_id"]),
            redirect_uri=str(data["redirect_uri"]),
            client_id=str(data["client_id"]),
            code_verifier=str(inner["v"]),
            supplied_client_secret=supplied if isinstance(supplied, str) else None,
        )
    except (KeyError, TypeError, ValueError, PlatformCredentialCryptoError):
        logger.warning("youtube_install.state_malformed")
        return None


async def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> tuple[str, str | None]:
    """Exchange the authorization ``code`` (+ PKCE verifier) for ``(access, refresh | None)``.

    Raises :class:`YoutubeInstallError` ``exchange_failed`` on any transport/status/shape failure;
    never logs the code, verifier, secret or tokens.
    """
    token_url = GOOGLE_TOKEN_URL
    try:
        await guard_google_url(token_url)
    except pc_errors.ProviderUnavailable as exc:
        raise YoutubeInstallError(ERR_EXCHANGE) from exc
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, transport=_transport, follow_redirects=False
        ) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("youtube_install.token_request_failed")
        raise YoutubeInstallError(ERR_EXCHANGE) from exc
    if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
        logger.warning("youtube_install.token_request_non_200 status=%d", response.status_code)
        raise YoutubeInstallError(ERR_EXCHANGE)
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise YoutubeInstallError(ERR_EXCHANGE) from exc
    access = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access, str) or not access:
        raise YoutubeInstallError(ERR_EXCHANGE)
    refresh = payload.get("refresh_token")
    return access, refresh if isinstance(refresh, str) and refresh else None


async def complete_install(
    dal: Any,
    service: ProviderConnectionService,
    *,
    state: str,
    code: str | None,
    provider_error: str | None,
) -> None:
    """Finish the flow for the public callback; raises :class:`YoutubeInstallError` on any failure.

    The ``state`` is consumed FIRST and unconditionally -- even a denied or code-less callback
    burns it, so one state can never be replayed. The tenant, community and acting admin all come
    from that state, never from the request.
    """
    pending = await consume_state(state)
    if pending is None:
        raise YoutubeInstallError(ERR_STATE)
    if provider_error:
        raise YoutubeInstallError(ERR_DENIED)
    if not code:
        raise YoutubeInstallError(ERR_NO_CODE)
    if pending.supplied_client_secret is not None:
        client_secret = pending.supplied_client_secret
    else:
        try:
            resolved_id, client_secret = await resolve_app_credentials(
                dal, tenant_id=pending.tenant_id
            )
        except AppCredentialsUnavailable as exc:
            raise YoutubeInstallError(ERR_APP) from exc
        if resolved_id != pending.client_id:
            # The stored application changed between authorize and callback: refuse, never mix.
            raise YoutubeInstallError(ERR_APP)
    access, refresh = await exchange_code(
        client_id=pending.client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=pending.redirect_uri,
        code_verifier=pending.code_verifier,
    )
    if refresh is None:
        # Hourly access tokens with no refresh token would die within the hour: refuse the install.
        raise YoutubeInstallError(ERR_NO_REFRESH)
    await _store_connection(service, pending, access, refresh)
    if pending.supplied_client_secret is not None:
        try:
            store_tenant_credentials(
                dal,
                tenant_id=pending.tenant_id,
                is_global_tenant=False,
                platform=PROVIDER_ID,
                payload={"client_id": pending.client_id, "client_secret": client_secret},
                installed_by_user_id=pending.user_id,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed code, details never surfaced
            logger.warning("youtube_install.app_store_failed tenant_id=%d", pending.tenant_id)
            raise YoutubeInstallError(ERR_SAVE) from exc


async def _store_connection(
    service: ProviderConnectionService, pending: InstallState, access: str, refresh: str
) -> None:
    """Create the connection, or rotate the credentials of the already-connected channel."""
    try:
        await service.create(
            tenant_id=pending.tenant_id,
            community_id=pending.community_id,
            actor_user_id=pending.user_id,
            provider=PROVIDER_ID,
            access_token=access,
            refresh_token=refresh,
            metadata={},
            enabled=True,
        )
    except pc_errors.ConnectionConflict:
        if await _reconnect(service, pending, access, refresh):
            return
        raise YoutubeInstallError(ERR_SAVE) from None
    except pc_errors.InsufficientScopes as exc:
        raise YoutubeInstallError(ERR_SCOPES) from exc
    except pc_errors.ResourceUnresolvable as exc:
        raise YoutubeInstallError(ERR_NO_CHANNEL) from exc
    except (pc_errors.ProviderUnavailable, pc_errors.ProviderQuotaExceeded) as exc:
        raise YoutubeInstallError(ERR_UNAVAILABLE) from exc
    except pc_errors.ProviderConnectionError as exc:
        raise YoutubeInstallError(ERR_SAVE) from exc


async def _reconnect(
    service: ProviderConnectionService, pending: InstallState, access: str, refresh: str
) -> bool:
    """Rotate credentials on this community's YouTube connection for the same channel.

    The framework refuses (conflict) to rotate onto a connection whose channel differs, so trying
    the community's YouTube connections in turn can only ever succeed for the matching one.
    """
    views = await service.list(tenant_id=pending.tenant_id, community_id=pending.community_id)
    for view in views:
        if view.binding.connection.platform != PROVIDER_ID:
            continue
        try:
            await service.update(
                tenant_id=pending.tenant_id,
                community_id=pending.community_id,
                actor_user_id=pending.user_id,
                connection_id=view.binding.connection.id,
                enabled=None,
                metadata=None,
                access_token=access,
                refresh_token=refresh,
            )
        except pc_errors.ProviderConnectionError:
            continue
        return True
    return False


async def acquire_discover_slot(*, tenant_id: int, connection_id: int) -> bool:
    """Take this connection's discovery cooldown slot; ``False`` when it is still cooling down.

    Discovery spends YouTube Data API quota (10,000 units/day per project by default), so one
    connection may trigger it at most once per :data:`DISCOVER_COOLDOWN_S` across all replicas.
    """
    key = f"{_DISCOVER_KEY_PREFIX}{tenant_id}:{connection_id}"
    taken = await _redis_client().set(key, "1", nx=True, ex=DISCOVER_COOLDOWN_S)
    return bool(taken)
