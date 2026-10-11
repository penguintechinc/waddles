"""Twitch adapter: the first provider on the connection framework.

Models the *chat bot presence* Waddles needs: a bot account (for example ``waddlebots``) whose user
access token lets Waddles read -- and, with ``chat:edit``, post to -- one channel's chat.

* **Credential** -- the bot account's user access token (+ optional refresh token). Verified with
  Twitch's ``GET /oauth2/validate`` (no Helix scope needed), which also reports the bot's login/id,
  the OAuth application that minted the token, its scopes and its lifetime. An app access token has
  no user and is rejected.
* **Resource** -- the channel to join, given as ``metadata.channel_login`` and resolved to its
  stable Twitch user id with Helix ``GET /users?login=``. That id is the connection's
  ``resource_id`` (the token belongs to the *bot*, the resource is the *channel*, so one bot token
  can serve many channels and many tokens can serve one channel's different communities).
* **Refresh** -- Twitch user tokens last ~4 hours and rotate the refresh token on every use, so the
  framework refreshes under a row lock. Refreshing needs the OAuth *application* that minted the
  token; this adapter only ever uses the platform's own app (``TWITCH_CLIENT_ID`` /
  ``TWITCH_CLIENT_SECRET``) and refuses (:class:`RefreshNotConfigured`) a token minted by any other
  app instead of sending Twitch a request it is certain to reject -- which would wrongly mark the
  connection dead.

Every URL is a fixed constant and still goes through the shared SSRF guard (defence in depth, like
``oauth_providers``). Responses are size-bounded and redirects are never followed. A token is only
ever placed in a request header, never in a URL, a log line, a span or an error message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from services.errors import ApiError
from services.oauth_providers import PROVIDERS, ProviderNotConfigured, client_credentials
from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    Account,
    AdapterSpec,
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


class _SuppressTwitchRequestLines(logging.Filter):
    """Drop httpx's INFO ``HTTP Request: GET <full url>`` line for Twitch hosts.

    httpx logs every request URL, query string included. For Twitch that is
    ``/helix/users?login=<channel>`` -- an admin-supplied handle that must not reach the log
    pipeline (PII-free logs). Tokens are never in a URL here, so this is about the handle only. The
    filter is deliberately narrow (Twitch hosts only) so it does not blind httpx for every other
    outbound call in hub-api.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` (suppress) for httpx records that mention a Twitch host."""
        return "twitch.tv" not in record.getMessage()


def _install_log_filter() -> None:
    """Attach the filter to the ``httpx`` logger exactly once."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _SuppressTwitchRequestLines) for f in httpx_logger.filters):
        httpx_logger.addFilter(_SuppressTwitchRequestLines())


_install_log_filter()

PROVIDER_ID: Final = "twitch"
RESOURCE_TYPE_CHANNEL: Final = "twitch_channel"

TWITCH_VALIDATE_URL: Final = "https://id.twitch.tv/oauth2/validate"
TWITCH_TOKEN_URL: Final = "https://id.twitch.tv/oauth2/token"  # noqa: S105 - URL, not a credential
TWITCH_HELIX_USERS_URL: Final = "https://api.twitch.tv/helix/users"

SCOPE_CHAT_READ: Final = "chat:read"
SCOPE_CHAT_EDIT: Final = "chat:edit"
CAPABILITY_CHAT_INGEST: Final = "chat_ingest"
CAPABILITY_ACTION_RELAY: Final = "action_relay"

#: Connect within 5 s, whole request within 15 s -- matches the rest of hub-api's outbound calls.
_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
#: Twitch answers are a few hundred bytes; anything past this is not Twitch.
_MAX_RESPONSE_BYTES: Final = 262_144
_OAUTH_PREFIX: Final = "oauth:"

TWITCH_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="Twitch",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_CHANNEL,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_CHAT_INGEST, CAPABILITY_ACTION_RELAY}),
    required_scopes=(SCOPE_CHAT_READ,),
    credential_fields=(
        CredentialField(
            "access_token",
            True,
            "User access token of the bot account (scopes: chat:read, optionally chat:edit).",
        ),
        CredentialField(
            "refresh_token",
            False,
            "Refresh token for the access token; without it the connection stops when the "
            "access token expires (about 4 hours).",
        ),
    ),
    metadata_fields=(
        MetadataField(
            "channel_login",
            True,
            "Login name of the Twitch channel whose chat the bot joins (a leading # is ignored).",
            pattern=r"[a-z0-9_]{3,25}",
            max_length=26,
            lowercase=True,
            strip_prefix="#",
        ),
    ),
    public_metadata_keys=frozenset(
        {
            "channel_login",
            "channel_id",
            "bot_login",
            "bot_user_id",
            "scopes",
            "capabilities",
        }
    ),
    runtime_metadata_keys=frozenset(
        {
            "channel_login",
            "channel_id",
            "bot_login",
            "bot_user_id",
            "client_id",
            "scopes",
            "capabilities",
        }
    ),
    refreshable=True,
)


class TwitchAdapter:
    """Twitch implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = TWITCH_SPEC

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
            app_credentials: ``() -> (client_id, client_secret)`` of the platform's Twitch OAuth
                app; defaults to the ``TWITCH_CLIENT_ID`` / ``TWITCH_CLIENT_SECRET`` env pair.
            clock: Injectable UTC clock for deterministic expiry arithmetic.
        """
        self._transport = transport
        self._app_credentials = app_credentials or _env_app_credentials
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- ProviderAdapter -------------------------------------------------------------------

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Strip the ``oauth:`` prefix IRC-style token generators prepend."""
        token = credentials.access_token
        if token.lower().startswith(_OAUTH_PREFIX):
            token = token[len(_OAUTH_PREFIX) :]
        return Credentials(access_token=token, refresh_token=credentials.refresh_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """``chat:read`` grants chat ingest; ``chat:edit`` grants posting to chat."""
        granted: set[str] = set()
        if SCOPE_CHAT_READ in scopes:
            granted.add(CAPABILITY_CHAT_INGEST)
        if SCOPE_CHAT_EDIT in scopes:
            granted.add(CAPABILITY_ACTION_RELAY)
        return frozenset(granted)

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """``GET /oauth2/validate`` -- who owns this token, which app minted it, what it may do."""
        response = await self._send(
            "validate",
            "GET",
            TWITCH_VALIDATE_URL,
            headers={"Authorization": f"OAuth {credentials.access_token}"},
        )
        if response.status_code == 401:
            raise CredentialsRejected("Twitch rejected the access token")
        if response.status_code != 200:
            logger.warning("twitch.validate_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Twitch could not validate the token right now")
        payload = _json_object(response)
        user_id = payload.get("user_id")
        login = payload.get("login")
        client_id = payload.get("client_id")
        if not (isinstance(user_id, str) and user_id and isinstance(login, str) and login):
            # An app access token validates fine but has no user: it cannot sit in chat.
            raise CredentialsRejected("the token is not a Twitch user access token")
        if not isinstance(client_id, str) or not client_id:
            raise ProviderUnavailable("Twitch returned an unexpected validate response")
        raw_scopes = payload.get("scopes")
        scopes = (
            tuple(s for s in raw_scopes if isinstance(s, str))
            if isinstance(raw_scopes, list)
            else ()
        )
        missing = [scope for scope in self.spec.required_scopes if scope not in scopes]
        if missing:
            raise InsufficientScopes(
                f"the token is missing required Twitch scope(s): {', '.join(missing)}"
            )
        return Account(
            account_id=user_id,
            account_label=login,
            app_id=client_id,
            scopes=scopes,
            expires_at=self._expiry(payload.get("expires_in")),
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """Helix ``GET /users?login=`` -- the channel's stable user id (the connection resource)."""
        channel = metadata["channel_login"]
        response = await self._send(
            "resolve",
            "GET",
            TWITCH_HELIX_USERS_URL,
            headers={
                "Authorization": f"Bearer {credentials.access_token}",
                "Client-Id": account.app_id,
            },
            params={"login": channel},
        )
        if response.status_code == 401:
            raise CredentialsRejected("Twitch rejected the access token")
        if response.status_code == 400:
            raise ResourceUnresolvable("that is not a valid Twitch channel name")
        if response.status_code != 200:
            logger.warning("twitch.resolve_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Twitch could not look up the channel right now")
        data = _json_object(response).get("data")
        match = None
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and str(entry.get("login", "")).lower() == channel:
                    match = entry
                    break
        channel_id = match.get("id") if match is not None else None
        if not isinstance(channel_id, str) or not channel_id:
            raise ResourceUnresolvable("that Twitch channel does not exist")
        return ResolvedResource(
            resource_id=channel_id,
            facts={
                "channel_login": channel,
                "channel_id": channel_id,
                "bot_login": account.account_label,
                "bot_user_id": account.account_id,
                "client_id": account.app_id,
            },
        )

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Exchange the refresh token using the platform's own Twitch OAuth application."""
        try:
            client_id, client_secret = self._app_credentials()
        except ProviderNotConfigured as exc:
            raise RefreshNotConfigured(
                "the platform has no Twitch OAuth application configured to refresh this token"
            ) from exc
        minted_by = facts.get("client_id")
        if isinstance(minted_by, str) and minted_by and minted_by != client_id:
            raise RefreshNotConfigured(
                "this token was issued by a different Twitch application than the one the "
                "platform has configured, so it cannot be refreshed"
            )
        response = await self._send(
            "refresh",
            "POST",
            TWITCH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if response.status_code in (400, 401):
            raise RefreshRejected("Twitch rejected the refresh token")
        if response.status_code != 200:
            logger.warning("twitch.refresh_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Twitch could not refresh the token right now")
        payload = _json_object(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderUnavailable("Twitch returned an unexpected refresh response")
        new_refresh = payload.get("refresh_token")
        raw_scopes = payload.get("scope")
        scopes = (
            tuple(s for s in raw_scopes if isinstance(s, str))
            if isinstance(raw_scopes, list)
            else ()
        )
        return RefreshedTokens(
            access_token=access_token,
            refresh_token=new_refresh if isinstance(new_refresh, str) and new_refresh else None,
            expires_at=self._expiry(payload.get("expires_in")),
            scopes=scopes,
        )

    # -- internals -------------------------------------------------------------------------

    def _expiry(self, expires_in: object) -> datetime | None:
        """``now + expires_in`` seconds; ``None`` when Twitch reports no (or a zero) lifetime."""
        if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
            return None
        if expires_in <= 0:
            return None
        return self._clock() + timedelta(seconds=int(expires_in))

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
                logger.warning("twitch.request_failed call=%s", call)
                raise ProviderUnavailable("Twitch could not be reached") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("Twitch returned an unexpectedly large response")
        logger.debug("twitch.call_done call=%s status=%d", call, response.status_code)
        return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body or raise :class:`ProviderUnavailable` (never echoes the body)."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("Twitch returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailable("Twitch returned a malformed response")
    return payload


def _env_app_credentials() -> tuple[str, str]:
    """The platform Twitch OAuth app from env (raises ``ProviderNotConfigured`` when unset)."""
    return client_credentials(PROVIDERS[PROVIDER_ID])
