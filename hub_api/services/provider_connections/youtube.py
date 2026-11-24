"""YouTube adapter: a community's own YouTube channel + the live chat its bot talks in.

Models the *outbound chat presence* the data plane's YouTube path needs (``chat.send`` in
core/svc_ingest ``outbound_creds_youtube.rs`` / ``youtube_slots.rs``):

* **Credential** -- the channel owner's Google OAuth2 token pair (scope
  ``youtube.force-ssl``, the least scope that can post to a live chat). The access token is
  verified with Google's ``tokeninfo`` (POST body, so the token never sits in a URL), which also
  reports the issuing OAuth client (``aud``), the granted scopes and the lifetime. The token is
  minted by the hub-api OAuth install flow (``services/youtube_install.py``: authorization code +
  PKCE, state verified); this adapter only ever sees the finished pair.
* **Resource** -- the authorised channel, resolved with ``channels.list?mine=true`` (1 quota unit).
  Its id is the connection's ``resource_id``.
* **Binding** -- ``live_chat_id``: the live chat the community's bot posts into. NON-secret
  resource metadata, ``^[A-Za-z0-9_-]{1,128}$`` (the exact contract ``is_live_chat_id`` enforces in
  the executor). It is optional at create (the OAuth callback cannot know it) but the binding is
  *incomplete* -- the data plane neither lists nor resolves it -- until it is set, either typed in
  by an admin (``PATCH``) or discovered (:meth:`YoutubeAdapter.discover_metadata`,
  ``liveBroadcasts.list?mine=true``, 1 quota unit).
* **Refresh** -- Google access tokens last ~1 hour and the refresh token is long-lived (Google
  normally omits it from a refresh response; if it ever rotates the framework stores the new one).
  Refreshing needs the OAuth *application* that minted the token: per-tenant bot credentials
  (docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md) -- the tenant is read from the
  framework-stamped ``tenant_id`` fact and the application from the injected resolver. A token
  minted by a different application is refused (:class:`RefreshNotConfigured`) rather than sent to
  Google to be rejected, which would wrongly mark the connection dead. ``invalid_grant`` is the one
  answer that means the refresh token is dead (revoked, expired -- Google expires refresh tokens of
  "Testing"-mode apps after 7 days): the framework then marks the connection ``expired`` with
  reason ``refresh_rejected`` (the admin reconnects).

Every URL is a fixed constant whose host is in :data:`ALLOWED_HOSTS` and still goes through the
shared SSRF guard. Responses are size-bounded, redirects are never followed, and a token is only
ever placed in a request header or form body -- never in a URL, a log line, a span or an error
message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from services.errors import ApiError
from services.provider_connections.adapter import (
    INSTALL_KIND_OAUTH_CONNECT,
    Account,
    AdapterSpec,
    BindingKey,
    CredentialField,
    Credentials,
    MetadataField,
    RefreshedTokens,
    ResolvedResource,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InsufficientScopes,
    OwnershipNotProven,
    ProviderQuotaExceeded,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    ResourceUnresolvable,
)
from services.provider_connections.telemetry import ownership_counter, provider_call
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

PROVIDER_ID: Final = "youtube"
RESOURCE_TYPE_CHANNEL: Final = "youtube_channel"
CAPABILITY_CHAT_INGEST: Final = "chat_ingest"
CAPABILITY_ACTION_RELAY: Final = "action_relay"

#: The one scope the install requests: read + post live chat, nothing else (no ``readonly``).
SCOPE_FORCE_SSL: Final = "https://www.googleapis.com/auth/youtube.force-ssl"

GOOGLE_AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a credential
GOOGLE_TOKENINFO_URL: Final = "https://oauth2.googleapis.com/tokeninfo"
YOUTUBE_CHANNELS_URL: Final = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_BROADCASTS_URL: Final = "https://www.googleapis.com/youtube/v3/liveBroadcasts"

#: Every host this adapter (and its install flow) may ever contact. A URL outside this set is a
#: programming error and is refused before the SSRF guard even resolves it.
ALLOWED_HOSTS: Final = frozenset(
    {"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}
)

#: The binding contract, byte for byte what the executor's ``is_live_chat_id`` accepts.
LIVE_CHAT_ID_PATTERN: Final = r"[A-Za-z0-9_-]{1,128}"
_LIVE_CHAT_ID_RE: Final = re.compile(LIVE_CHAT_ID_PATTERN)
_CHANNEL_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")

_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
#: Google answers here are a few KiB; ``liveBroadcasts`` with 50 items stays well under this.
_MAX_RESPONSE_BYTES: Final = 524_288
#: One page of broadcasts is one quota unit; the cap keeps discovery to exactly one call.
_BROADCAST_PAGE: Final = 50

#: ``() -> (client_id, client_secret)`` for a tenant's own Google OAuth application.
AppCredentials = Callable[[int], Awaitable[tuple[str, str]]]


class AppCredentialsUnavailable(RuntimeError):  # noqa: N818 - a configuration signal, not an "error" type
    """The tenant has no usable Google OAuth application (none configured / unreadable)."""


async def _no_app_credentials(tenant_id: int) -> tuple[str, str]:
    """Default resolver: refuses loudly -- a build without a composition root never guesses."""
    raise AppCredentialsUnavailable("no YouTube OAuth application resolver is configured")


YOUTUBE_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="YouTube",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_CHANNEL,
    install_kind=INSTALL_KIND_OAUTH_CONNECT,
    capabilities=frozenset({CAPABILITY_CHAT_INGEST, CAPABILITY_ACTION_RELAY}),
    required_scopes=(SCOPE_FORCE_SSL,),
    credential_fields=(
        CredentialField(
            "access_token",
            True,
            "Google OAuth2 access token of the channel owner (scope youtube.force-ssl). "
            "Normally obtained through the OAuth connect flow, not pasted.",
        ),
        CredentialField(
            "refresh_token",
            False,
            "Google refresh token; without it the connection stops when the access token "
            "expires (about 1 hour).",
        ),
    ),
    metadata_fields=(
        MetadataField(
            "live_chat_id",
            False,
            "Id of the live chat the community's bot posts into (letters, digits, '-' and '_'). "
            "Discoverable from the channel's active broadcast; the binding is incomplete "
            "(not served to the data plane) until it is set.",
            pattern=LIVE_CHAT_ID_PATTERN,
            max_length=128,
        ),
    ),
    public_metadata_keys=frozenset(
        {"live_chat_id", "channel_id", "channel_title", "scopes", "capabilities"}
    ),
    runtime_metadata_keys=frozenset({"live_chat_id", "channel_id", "scopes", "capabilities"}),
    refreshable=True,
    requires_ownership=True,
    binding_metadata=(BindingKey("live_chat_id", "live_chat_id", required=True),),
)


class YoutubeAdapter:
    """YouTube implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = YOUTUBE_SPEC

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        app_credentials: AppCredentials | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build the adapter.

        Args:
            transport: Swap the socket (tests inject an ``httpx.MockTransport``); the host
                allowlist, SSRF guard, size bound and error mapping still run for real.
            app_credentials: ``tenant_id -> (client_id, client_secret)`` of that tenant's own
                Google OAuth application (global tenant: the platform app from env).
            clock: Injectable UTC clock for deterministic expiry arithmetic.
        """
        self._transport = transport
        self._app_credentials: AppCredentials = app_credentials or _no_app_credentials
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- ProviderAdapter -------------------------------------------------------------------

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Strip an accidental ``Bearer `` prefix pasted along with the token."""
        token = credentials.access_token
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return Credentials(access_token=token, refresh_token=credentials.refresh_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """``youtube.force-ssl`` covers reading and posting live chat: it grants both."""
        return self.spec.capabilities if SCOPE_FORCE_SSL in scopes else frozenset()

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """``POST /tokeninfo`` -- which OAuth client minted this token, its scopes and lifetime."""
        response = await self._send(
            "validate",
            "POST",
            GOOGLE_TOKENINFO_URL,
            data={"access_token": credentials.access_token},
        )
        if response.status_code in (400, 401):
            raise CredentialsRejected("Google rejected the access token")
        if response.status_code != 200:
            logger.warning("youtube.validate_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Google could not validate the token right now")
        payload = _json_object(response)
        client_id = payload.get("aud")
        if not isinstance(client_id, str) or not client_id:
            raise ProviderUnavailable("Google returned an unexpected validate response")
        raw_scope = payload.get("scope")
        scopes = tuple(raw_scope.split()) if isinstance(raw_scope, str) else ()
        if SCOPE_FORCE_SSL not in scopes:
            raise InsufficientScopes(
                "the token is missing the required Google scope youtube.force-ssl"
            )
        subject = payload.get("sub")
        return Account(
            account_id=subject if isinstance(subject, str) and subject else client_id,
            account_label="",
            app_id=client_id,
            scopes=scopes,
            expires_at=self._expiry(payload.get("expires_in")),
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """``channels.list?mine=true`` -- the authorised channel (the connection's resource)."""
        response = await self._send(
            "resolve",
            "GET",
            YOUTUBE_CHANNELS_URL,
            headers=_bearer(credentials),
            params={"part": "snippet", "mine": "true", "maxResults": "1"},
        )
        _raise_for_youtube_error(response, "resolve")
        items = _json_object(response).get("items")
        first = items[0] if isinstance(items, list) and items else None
        channel_id = first.get("id") if isinstance(first, dict) else None
        if not isinstance(channel_id, str) or not _CHANNEL_ID_RE.fullmatch(channel_id):
            raise ResourceUnresolvable("that Google account has no YouTube channel")
        snippet = first.get("snippet") if isinstance(first, dict) else None
        title = snippet.get("title") if isinstance(snippet, dict) else None
        facts: dict[str, Any] = {"channel_id": channel_id, "client_id": account.app_id}
        if isinstance(title, str) and title:
            facts["channel_title"] = title[:100]
        live_chat_id = metadata.get("live_chat_id")
        if live_chat_id:
            await self._assert_owned_live_chat(credentials, channel_id, live_chat_id)
        # ``channels.list?mine=true`` is scoped to the token's own channel: the token proves it.
        return ResolvedResource(
            resource_id=channel_id,
            facts=facts,
            owner_platform_user_id=channel_id,
            owner_proven_by_token=True,
        )

    async def _assert_owned_live_chat(
        self, credentials: Credentials, channel_id: str, live_chat_id: str
    ) -> None:
        """Refuse (403) a ``live_chat_id`` that is not one of ``channel_id``'s own broadcasts.

        ``live_chat_id`` is admin-typed; without this an admin could point their connection at
        ANOTHER channel's live chat. One ``liveBroadcasts.list?mine=true`` page (1 quota unit);
        only broadcasts whose ``snippet.channelId`` is exactly the connected channel count.
        """
        response = await self._send(
            "verify_chat",
            "GET",
            YOUTUBE_BROADCASTS_URL,
            headers=_bearer(credentials),
            params={"part": "snippet,status", "mine": "true", "maxResults": str(_BROADCAST_PAGE)},
        )
        _raise_for_youtube_error(response, "verify_chat")
        items = _json_object(response).get("items")
        for item in items if isinstance(items, list) else []:
            snippet = item.get("snippet") if isinstance(item, dict) else None
            if (
                isinstance(snippet, dict)
                and snippet.get("channelId") == channel_id
                and snippet.get("liveChatId") == live_chat_id
            ):
                return
        ownership_counter.add(1, {"provider": PROVIDER_ID, "outcome": "live_chat_rejected"})
        logger.warning("youtube.live_chat_not_owned -- live_chat_id is not the channel's own")
        raise OwnershipNotProven(
            "that live chat does not belong to one of the connected channel's own broadcasts",
            code="live_chat_not_owned",
        )

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Exchange the refresh token using the tenant's own Google OAuth application."""
        tenant_id = facts.get("tenant_id")
        if isinstance(tenant_id, bool) or not isinstance(tenant_id, int):
            raise RefreshNotConfigured("this connection does not record which tenant owns it")
        try:
            client_id, client_secret = await self._app_credentials(tenant_id)
        except AppCredentialsUnavailable as exc:
            raise RefreshNotConfigured(
                "the tenant has no Google OAuth application configured to refresh this token"
            ) from exc
        minted_by = facts.get("client_id")
        if isinstance(minted_by, str) and minted_by and minted_by != client_id:
            raise RefreshNotConfigured(
                "this token was issued by a different Google application than the one "
                "configured, so it cannot be refreshed"
            )
        response = await self._send(
            "refresh",
            "POST",
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if response.status_code in (400, 401):
            error = _oauth_error(response)
            if error == "invalid_grant":
                raise RefreshRejected("Google rejected the refresh token")
            if error in ("invalid_client", "unauthorized_client"):
                raise RefreshNotConfigured("Google rejected the configured OAuth application")
            logger.warning("youtube.refresh_unexpected_error status=%d", response.status_code)
            raise ProviderUnavailable("Google could not refresh the token right now")
        if response.status_code != 200:
            logger.warning("youtube.refresh_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Google could not refresh the token right now")
        payload = _json_object(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderUnavailable("Google returned an unexpected refresh response")
        new_refresh = payload.get("refresh_token")
        raw_scope = payload.get("scope")
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else None,
            expires_at=self._expiry(payload.get("expires_in")),
            scopes=tuple(raw_scope.split()) if isinstance(raw_scope, str) else (),
        )

    # -- discovery (MetadataDiscoverer) ----------------------------------------------------

    async def discover_metadata(
        self, credentials: Credentials, facts: Mapping[str, Any]
    ) -> Mapping[str, str]:
        """``liveBroadcasts.list?mine=true`` -- the live chat of the channel's active broadcast.

        Exactly one provider call (1 quota unit), one page. Only a broadcast owned by the
        connection's own channel that is ``live`` (or ``testing``) counts; when several are live
        the most recently started wins. Raises :class:`ResourceUnresolvable` when none qualifies.
        """
        response = await self._send(
            "discover",
            "GET",
            YOUTUBE_BROADCASTS_URL,
            headers=_bearer(credentials),
            params={
                "part": "snippet,status",
                "mine": "true",
                "maxResults": str(_BROADCAST_PAGE),
            },
        )
        _raise_for_youtube_error(response, "discover")
        items = _json_object(response).get("items")
        channel_id = facts.get("channel_id")
        best: tuple[int, str, str] | None = None  # (rank, started_at, chat id)
        for item in items if isinstance(items, list) else []:
            candidate = _live_candidate(item, channel_id)
            if candidate is not None and (best is None or candidate[:2] > best[:2]):
                best = candidate
        if best is None:
            raise ResourceUnresolvable("the channel has no active live broadcast right now")
        return {"live_chat_id": best[2]}

    # -- internals -------------------------------------------------------------------------

    def _expiry(self, expires_in: object) -> datetime | None:
        """``now + expires_in`` seconds (Google sends a number or a numeric string)."""
        if isinstance(expires_in, str):
            expires_in = int(expires_in) if expires_in.isdecimal() else 0
        if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
            return None
        seconds = int(expires_in)
        return self._clock() + timedelta(seconds=seconds) if seconds > 0 else None

    async def _send(
        self,
        call: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One allow-listed, guarded, size-bounded request; failure -> ProviderUnavailable."""
        await guard_google_url(url)
        with provider_call(PROVIDER_ID, call):
            try:
                async with httpx.AsyncClient(
                    timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
                ) as client:
                    response = await client.request(
                        method, url, headers=headers, params=params, data=data
                    )
            except httpx.HTTPError as exc:
                logger.warning("youtube.request_failed call=%s", call)
                raise ProviderUnavailable("Google could not be reached") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("Google returned an unexpectedly large response")
        logger.debug("youtube.call_done call=%s status=%d", call, response.status_code)
        return response


async def guard_google_url(url: str) -> None:
    """Refuse a URL whose host is not in :data:`ALLOWED_HOSTS`, then run the shared SSRF guard.

    Shared with the install flow so the authorization-code exchange obeys the same allowlist.
    """
    if urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ProviderUnavailable("the outbound request is not to an allowed Google host")
    try:
        await validate_outbound_url(url, allowed_schemes=("https",))
    except ApiError as exc:
        raise ProviderUnavailable("the outbound request was blocked by the network guard") from exc


def _bearer(credentials: Credentials) -> dict[str, str]:
    """Authorization header for a Google API call (the only place the token is placed)."""
    return {"Authorization": f"Bearer {credentials.access_token}"}


def _live_candidate(item: object, channel_id: object) -> tuple[int, str, str] | None:
    """``(rank, started_at, live_chat_id)`` for a qualifying active broadcast, else ``None``."""
    if not isinstance(item, dict):
        return None
    snippet = item.get("snippet")
    status = item.get("status")
    if not isinstance(snippet, dict) or not isinstance(status, dict):
        return None
    lifecycle = status.get("lifeCycleStatus")
    if lifecycle not in ("live", "testing"):
        return None
    if isinstance(channel_id, str) and snippet.get("channelId") not in (None, channel_id):
        return None  # another channel's broadcast: never bind it to this connection
    chat_id = snippet.get("liveChatId")
    if not isinstance(chat_id, str) or not _LIVE_CHAT_ID_RE.fullmatch(chat_id):
        return None
    started = snippet.get("actualStartTime")
    return (1 if lifecycle == "live" else 0, started if isinstance(started, str) else "", chat_id)


def _error_reasons(response: httpx.Response) -> set[str]:
    """The ``reason`` codes of a Google API error body (never echoed, only compared)."""
    payload = _json_object(response)  # a non-JSON error body is a malformed answer: unavailable
    error = payload.get("error")
    errors = error.get("errors") if isinstance(error, dict) else None
    return {
        str(entry["reason"])
        for entry in (errors if isinstance(errors, list) else [])
        if isinstance(entry, dict) and "reason" in entry
    }


def _raise_for_youtube_error(response: httpx.Response, call: str) -> None:
    """Map a non-200 YouTube Data API answer onto the typed framework errors."""
    if response.status_code == 200:
        return
    if response.status_code == 401:
        raise CredentialsRejected("Google rejected the access token")
    reasons = _error_reasons(response)
    if response.status_code in (403, 429):
        if reasons & {"quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"}:
            raise ProviderQuotaExceeded("the YouTube API quota is exhausted; try again later")
        if "liveStreamingNotEnabled" in reasons:
            raise ResourceUnresolvable("live streaming is not enabled on that YouTube channel")
        if reasons & {"insufficientPermissions", "forbidden"}:
            raise InsufficientScopes("the token is not allowed to read this YouTube channel")
    logger.warning("youtube.unexpected_status call=%s status=%d", call, response.status_code)
    raise ProviderUnavailable("YouTube could not answer right now")


def _oauth_error(response: httpx.Response) -> str:
    """The OAuth ``error`` code of a token-endpoint failure (a short enum string, else ``""``)."""
    payload = _json_object(response)  # a non-JSON error body is a malformed answer: unavailable
    code = payload.get("error")
    return code if isinstance(code, str) and len(code) <= 64 else ""


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body or raise :class:`ProviderUnavailable` (never echoes the body)."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("Google returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailable("Google returned a malformed response")
    return payload
