"""Kick adapter: the chat bot presence Waddles needs on one Kick channel.

A Kick connection is a bot account's OAuth user token (Authorization Code + PKCE, scopes
``user:read channel:read chat:write``) bound to ONE channel. The adapter resolves, at connect time
and through the hub (never from the guest or the browser), every id the Kick data plane keys on:

* ``self_user_id``         -- the token owner (``GET /public/v1/users``); the self-echo filter
  key.
* ``broadcaster_user_id``  -- the channel owner (``GET /public/v1/channels?slug=``); the webhook
  route binding and the connection's ``resource_id``.
* ``chatroom_id``          -- the Pusher chatroom (``GET kick.com/api/v2/channels/<slug>``). The
  public API does not expose it, so this is the same lookup ``svc_ingest``'s Pusher receiver
  already makes; its ``user_id`` (when present) must agree with the public API's broadcaster id,
  otherwise the slug resolved to two different channels and the connect is refused.

Token facts (``client_id`` / scopes / expiry) come from ``POST /public/v1/token/introspect``. Kick
access tokens are short-lived and refresh tokens ROTATE, so the framework refreshes under a row
lock (single flight per connection) before expiry; this adapter only supplies the exchange, using
the platform's own Kick OAuth app (``KICK_CLIENT_ID`` / ``KICK_CLIENT_SECRET``) and refusing
(:class:`RefreshNotConfigured`) a token minted by any other app instead of sending Kick a request
that is certain to be rejected. At most ONE Kick connection per community
(``max_connections_per_community``; the data plane refuses an ambiguous binding).

Every URL is a fixed constant (the slug is pattern-validated and percent-quoted) and goes through
the shared SSRF guard; responses are size-bounded and redirects never followed. A token is only
ever sent in a request header -- never in a URL, a log line, a span or an error message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

import httpx

from services.errors import ApiError
from services.oauth_providers import PROVIDERS, ProviderNotConfigured, client_credentials
from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
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
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    ResourceUnresolvable,
)
from services.provider_connections.telemetry import provider_call
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)


class _SuppressKickRequestLines(logging.Filter):
    """Drop httpx's INFO ``HTTP Request: GET <full url>`` line for Kick hosts.

    The channel slug is an admin-supplied handle that must not reach the log pipeline (PII-free
    logs). Narrow on purpose (Kick hosts only) so httpx stays visible for every other call.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` (suppress) for httpx records that mention a Kick host."""
        return "kick.com" not in record.getMessage()


def _install_log_filter() -> None:
    """Attach the filter to the ``httpx`` logger exactly once."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _SuppressKickRequestLines) for f in httpx_logger.filters):
        httpx_logger.addFilter(_SuppressKickRequestLines())


_install_log_filter()

PROVIDER_ID: Final = "kick"
RESOURCE_TYPE_CHANNEL: Final = "kick_channel"

KICK_INTROSPECT_URL: Final = "https://api.kick.com/public/v1/token/introspect"
KICK_USERS_URL: Final = "https://api.kick.com/public/v1/users"
KICK_CHANNELS_URL: Final = "https://api.kick.com/public/v1/channels"
KICK_CHATROOM_URL: Final = "https://kick.com/api/v2/channels/{slug}"
KICK_TOKEN_URL: Final = "https://id.kick.com/oauth/token"  # noqa: S105 - URL, not a credential

SCOPE_USER_READ: Final = "user:read"
SCOPE_CHANNEL_READ: Final = "channel:read"
SCOPE_CHAT_WRITE: Final = "chat:write"
CAPABILITY_CHAT_INGEST: Final = "chat_ingest"
CAPABILITY_ACTION_RELAY: Final = "action_relay"

_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
_MAX_RESPONSE_BYTES: Final = 262_144
#: Kick ids are positive integers; 20 digits is the wire contract the host already enforces.
_ID_RE: Final = re.compile(r"[1-9][0-9]{0,19}")
_SLUG_PATTERN: Final = r"[a-z0-9_-]{1,50}"

KICK_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="Kick",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_CHANNEL,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_CHAT_INGEST, CAPABILITY_ACTION_RELAY}),
    required_scopes=(SCOPE_USER_READ, SCOPE_CHANNEL_READ),
    credential_fields=(
        CredentialField(
            "access_token",
            True,
            "User access token of the bot account (scopes: user:read, channel:read, "
            "optionally chat:write).",
        ),
        CredentialField(
            "refresh_token",
            True,
            "Refresh token for the access token; Kick rotates it on every use, and without it "
            "the connection stops when the access token expires.",
        ),
    ),
    metadata_fields=(
        MetadataField(
            "channel_slug",
            True,
            "Slug of the Kick channel whose chat the bot joins (the part after kick.com/).",
            pattern=_SLUG_PATTERN,
            max_length=50,
            lowercase=True,
        ),
    ),
    public_metadata_keys=frozenset(
        {
            "channel_slug",
            "broadcaster_user_id",
            "chatroom_id",
            "self_user_id",
            "bot_login",
            "scopes",
            "capabilities",
        }
    ),
    runtime_metadata_keys=frozenset(
        {
            "channel_slug",
            "broadcaster_user_id",
            "chatroom_id",
            "self_user_id",
            "bot_login",
            "scopes",
            "capabilities",
        }
    ),
    refreshable=True,
    requires_ownership=True,
    # No Kick account-linking flow exists yet (oauth_service.VALID_PLATFORMS): only a token
    # minted by the broadcaster proves ownership until one does.
    ownership_identity_platform="kick",
    binding_metadata=(
        BindingKey("chatroom_id", "chatroom_id", required=True),
        BindingKey("broadcaster_user_id", "broadcaster_user_id", required=True),
        BindingKey("self_user_id", "self_user_id", required=True),
        BindingKey("channel_slug", "channel_slug"),
    ),
    max_connections_per_community=1,
)


class KickAdapter:
    """Kick implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = KICK_SPEC

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        app_credentials: Callable[[], tuple[str, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build the adapter.

        Args:
            transport: Swap the socket (tests inject an ``httpx.MockTransport``); the SSRF guard,
                request construction, size bound and error mapping still run for real.
            app_credentials: ``() -> (client_id, client_secret)`` of the platform's Kick OAuth app;
                defaults to the ``KICK_CLIENT_ID`` / ``KICK_CLIENT_SECRET`` env pair.
            clock: Injectable UTC clock for deterministic expiry arithmetic.
        """
        self._transport = transport
        self._app_credentials = app_credentials or _env_app_credentials
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- ProviderAdapter -------------------------------------------------------------------

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Strip an accidental ``Bearer `` prefix; Kick tokens are otherwise used verbatim."""
        token = credentials.access_token
        if token[:7].lower() == "bearer ":
            token = token[7:].lstrip()
        return Credentials(access_token=token, refresh_token=credentials.refresh_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """``channel:read`` grants chat ingest; ``chat:write`` grants posting to chat."""
        granted: set[str] = set()
        if SCOPE_CHANNEL_READ in scopes:
            granted.add(CAPABILITY_CHAT_INGEST)
        if SCOPE_CHAT_WRITE in scopes:
            granted.add(CAPABILITY_ACTION_RELAY)
        return frozenset(granted)

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Introspect the token, then read its owner: who, which app, what scopes, until when."""
        headers = {"Authorization": f"Bearer {credentials.access_token}"}
        intro = await self._send("introspect", "POST", KICK_INTROSPECT_URL, headers=headers)
        if intro.status_code == 401:
            raise CredentialsRejected("Kick rejected the access token")
        if intro.status_code != 200:
            logger.warning("kick.introspect_unexpected_status status=%d", intro.status_code)
            raise ProviderUnavailable("Kick could not validate the token right now")
        data = _json_object(intro).get("data")
        if not isinstance(data, dict) or data.get("active") is not True:
            raise CredentialsRejected("the Kick token is not active")
        client_id = data.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise ProviderUnavailable("Kick returned an unexpected introspect response")
        scopes = _scopes(data.get("scope"))
        missing = [scope for scope in self.spec.required_scopes if scope not in scopes]
        if missing:
            raise InsufficientScopes(
                f"the token is missing required Kick scope(s): {', '.join(missing)}"
            )
        users = await self._send("users", "GET", KICK_USERS_URL, headers=headers)
        if users.status_code == 401:
            raise CredentialsRejected("Kick rejected the access token")
        if users.status_code != 200:
            logger.warning("kick.users_unexpected_status status=%d", users.status_code)
            raise ProviderUnavailable("Kick could not look up the token owner right now")
        user = _first(_json_object(users).get("data"))
        user_id = _numeric_id(user.get("user_id")) if user is not None else None
        name = user.get("name") if user is not None else None
        if user_id is None or not isinstance(name, str) or not name:
            # A token with no user (an app token) cannot sit in chat.
            raise CredentialsRejected("the token is not a Kick user access token")
        return Account(
            account_id=user_id,
            account_label=name,
            app_id=client_id,
            scopes=scopes,
            expires_at=_epoch(data.get("exp")),
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """Resolve the channel's broadcaster id and chatroom id; every failure is explicit."""
        slug = metadata["channel_slug"]
        response = await self._send(
            "channel",
            "GET",
            KICK_CHANNELS_URL,
            headers={"Authorization": f"Bearer {credentials.access_token}"},
            params={"slug": slug},
        )
        if response.status_code == 401:
            raise CredentialsRejected("Kick rejected the access token")
        if response.status_code == 400:
            raise ResourceUnresolvable("that is not a valid Kick channel slug")
        if response.status_code != 200:
            logger.warning("kick.channel_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Kick could not look up the channel right now")
        entry = _first(_json_object(response).get("data"))
        broadcaster_id = _numeric_id(entry.get("broadcaster_user_id")) if entry else None
        if entry is None or broadcaster_id is None or str(entry.get("slug", "")).lower() != slug:
            raise ResourceUnresolvable("that Kick channel does not exist")

        chat = await self._send(
            "chatroom", "GET", KICK_CHATROOM_URL.format(slug=quote(slug, safe=""))
        )
        if chat.status_code == 404:
            raise ResourceUnresolvable("that Kick channel has no chatroom")
        if chat.status_code != 200:
            logger.warning("kick.chatroom_unexpected_status status=%d", chat.status_code)
            raise ProviderUnavailable("Kick could not look up the channel chatroom right now")
        body = _json_object(chat)
        room = body.get("chatroom")
        chatroom_id = _numeric_id(room.get("id")) if isinstance(room, dict) else None
        if chatroom_id is None:
            raise ResourceUnresolvable("that Kick channel has no chatroom")
        owner = body.get("user_id")
        if owner is not None and _numeric_id(owner) != broadcaster_id:
            raise ResourceUnresolvable("Kick returned inconsistent channel identifiers")
        return ResolvedResource(
            resource_id=broadcaster_id,
            owner_platform_user_id=broadcaster_id,
            owner_proven_by_token=broadcaster_id == account.account_id,
            facts={
                "channel_slug": slug,
                "broadcaster_user_id": broadcaster_id,
                "chatroom_id": chatroom_id,
                "self_user_id": account.account_id,
                "bot_login": account.account_label,
                "client_id": account.app_id,
            },
        )

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Exchange the (rotating) refresh token using the platform's own Kick OAuth application."""
        try:
            client_id, client_secret = self._app_credentials()
        except ProviderNotConfigured as exc:
            raise RefreshNotConfigured(
                "the platform has no Kick OAuth application configured to refresh this token"
            ) from exc
        minted_by = facts.get("client_id")
        if isinstance(minted_by, str) and minted_by and minted_by != client_id:
            raise RefreshNotConfigured(
                "this token was issued by a different Kick application than the one the "
                "platform has configured, so it cannot be refreshed"
            )
        response = await self._send(
            "refresh",
            "POST",
            KICK_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if response.status_code in (400, 401):
            raise RefreshRejected("Kick rejected the refresh token")
        if response.status_code != 200:
            logger.warning("kick.refresh_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Kick could not refresh the token right now")
        payload = _json_object(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderUnavailable("Kick returned an unexpected refresh response")
        new_refresh = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int | float)
            or expires_in <= 0
        ):
            # A refreshed token with no lifetime would never be refreshed again: refuse it.
            raise ProviderUnavailable("Kick returned an unexpected refresh response")
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else None,
            expires_at=self._clock() + timedelta(seconds=int(expires_in)),
            scopes=_scopes(payload.get("scope")),
        )

    # -- internals -------------------------------------------------------------------------

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
        """One guarded, timed, size-bounded request; transport failure -> ProviderUnavailable."""
        try:
            await validate_outbound_url(url, allowed_schemes=("https",))
        except ApiError as exc:
            raise ProviderUnavailable(
                "the outbound request was blocked by the network guard"
            ) from exc
        with provider_call(PROVIDER_ID, call):
            try:
                async with httpx.AsyncClient(
                    timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
                ) as client:
                    response = await client.request(
                        method, url, headers=headers, params=params, data=data
                    )
            except httpx.HTTPError as exc:
                logger.warning("kick.request_failed call=%s", call)
                raise ProviderUnavailable("Kick could not be reached") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("Kick returned an unexpectedly large response")
        logger.debug("kick.call_done call=%s status=%d", call, response.status_code)
        return response


def _epoch(exp: object) -> datetime | None:
    """Absolute expiry from Kick's epoch-seconds ``exp``; ``None`` when absent/invalid."""
    if isinstance(exp, bool) or not isinstance(exp, int | float) or exp <= 0:
        return None
    return datetime.fromtimestamp(float(exp), tz=UTC)


def _scopes(raw: object) -> tuple[str, ...]:
    """Kick reports scopes as one space-separated string (a list is tolerated)."""
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, list):
        return tuple(s for s in raw if isinstance(s, str))
    return ()


def _numeric_id(value: object) -> str | None:
    """Canonical positive-integer id string (int or digit string), else ``None``."""
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, int) else value
    if isinstance(text, str) and _ID_RE.fullmatch(text):
        return text
    return None


def _first(data: object) -> dict[str, Any] | None:
    """First object of a Kick ``data`` array (or the object itself), else ``None``."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body or raise :class:`ProviderUnavailable` (never echoes the body)."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("Kick returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailable("Kick returned a malformed response")
    return payload


def _env_app_credentials() -> tuple[str, str]:
    """The platform Kick OAuth app from env (raises ``ProviderNotConfigured`` when unset)."""
    return client_credentials(PROVIDERS[PROVIDER_ID])
