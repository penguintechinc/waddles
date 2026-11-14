"""Discord adapter: a community's bot token + the one guild it serves.

Capabilities: ``chat_ingest`` (the gateway inbound feed the connector runtime opens with the bot
token) and ``action_relay``. Models the *outbound bot presence* the data plane's Discord host
duties need (``dm.send``, ``role.add``, ``role.remove`` in core/svc_ingest
``sender_exec/discord_duties.rs``):

* **Credential** -- the bot token, verified with Discord's ``GET /users/@me`` using the ``Bot``
  scheme (a user/OAuth token has no ``bot`` flag and is rejected). Bot tokens never expire and have
  no refresh token, so the connection is non-refreshable.
* **Resource** -- the guild the bot serves, given as ``metadata.guild_id`` and verified with
  ``GET /guilds/{id}`` (a bot that is not a member of the guild gets 403/404 -> unresolvable). The
  guild snowflake is the connection's ``resource_id``.
* **Managed roles** -- ``metadata.managed_roles``: the allowlist of roles the host may grant or
  revoke, *key -> role snowflake*. NON-secret resource metadata (an admin-curated mapping, never a
  credential). The API accepts an object (or the canonical string); it is validated here and stored
  / served in the canonical wire form the data plane parses -- ``key:snowflake,key:snowflake``,
  sorted by key. Keys are stricter than ``^[a-z0-9_]{1,32}$`` (they must start with a letter) so
  every accepted key is also one the executor's ``is_role_key`` accepts: a key the executor rejects
  would make it refuse the WHOLE binding (fail closed), bricking an otherwise valid connection.

Admin-only by construction: the connection routes already demand ``connections:admin`` plus a
DB-backed check that the caller administers THIS community (``authorize_community``).

Every URL is a fixed constant behind the shared SSRF guard, responses are size-bounded, redirects
are never followed, and the token only ever sits in a request header -- never in a URL, a log line,
a span or an error message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import httpx

from services.errors import ApiError
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
    InvalidConnectionInput,
    ProviderUnavailable,
    RefreshNotConfigured,
    ResourceUnresolvable,
)
from services.provider_connections.telemetry import provider_call
from services.url_guard import validate_outbound_url

logger = logging.getLogger(__name__)

PROVIDER_ID: Final = "discord"
RESOURCE_TYPE_GUILD: Final = "discord_guild"
CAPABILITY_CHAT_INGEST: Final = "chat_ingest"
CAPABILITY_ACTION_RELAY: Final = "action_relay"

DISCORD_API_BASE: Final = "https://discord.com/api/v10"

#: Most roles one binding may allow (the executor accepts 64; hub-api is deliberately tighter).
MAX_MANAGED_ROLES: Final = 32
#: Real Discord snowflakes are 17-20 digits; stricter than the executor's ``<= 20 digits``.
SNOWFLAKE_PATTERN: Final = r"[0-9]{17,20}"
_SNOWFLAKE_RE: Final = re.compile(SNOWFLAKE_PATTERN)
#: Starts with a letter -- the executor's ``is_role_key`` (``[a-z][a-z0-9_-]{0,31}``) is the
#: binding contract, and this is the intersection with the documented ``[a-z0-9_]{1,32}``.
_ROLE_KEY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: Longest canonical ``key:snowflake,...`` string: 32 entries of 32 + 1 + 20, plus separators.
_MANAGED_ROLES_MAX_LENGTH: Final = MAX_MANAGED_ROLES * (32 + 1 + 20 + 1)

_TIMEOUT: Final = httpx.Timeout(15.0, connect=5.0)
#: Discord answers are a few KiB (a guild object); anything past this is not Discord.
_MAX_RESPONSE_BYTES: Final = 262_144


def normalize_managed_roles(field_name: str, raw: Any) -> str:
    """Validate a managed-roles mapping and return its canonical ``key:snowflake,...`` string.

    Accepts a ``{key: role_snowflake}`` object or the canonical string itself. Raises
    :class:`InvalidConnectionInput` naming only the *field*, never echoing a value, for: a wrong
    type, more than :data:`MAX_MANAGED_ROLES` entries, a bad key, a non-numeric / mis-sized role
    id (including JSON numbers -- they lose precision), or a duplicate key. An empty mapping is
    ``""`` (the field is then simply not stored).
    """
    if isinstance(raw, str):
        pairs: list[tuple[Any, Any]] = []
        if raw.strip():
            for part in raw.split(","):
                key, sep, value = part.strip().partition(":")
                if not sep:
                    raise InvalidConnectionInput(
                        f"metadata field {field_name!r} has an invalid format"
                    )
                pairs.append((key, value))
    elif isinstance(raw, Mapping):
        pairs = list(raw.items())
    else:
        raise InvalidConnectionInput(f"metadata field {field_name!r} must be an object")
    if len(pairs) > MAX_MANAGED_ROLES:
        raise InvalidConnectionInput(f"metadata field {field_name!r} has too many entries")
    seen: dict[str, str] = {}
    for key, value in pairs:
        if not isinstance(key, str) or not _ROLE_KEY_RE.fullmatch(key):
            raise InvalidConnectionInput(f"metadata field {field_name!r} has an invalid key")
        if not isinstance(value, str) or not _SNOWFLAKE_RE.fullmatch(value):
            raise InvalidConnectionInput(f"metadata field {field_name!r} has an invalid role id")
        if key in seen:
            raise InvalidConnectionInput(f"metadata field {field_name!r} has a duplicate key")
        seen[key] = value
    return ",".join(f"{key}:{seen[key]}" for key in sorted(seen))


DISCORD_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="Discord",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_GUILD,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_CHAT_INGEST, CAPABILITY_ACTION_RELAY}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            "access_token",
            True,
            "Bot token of the Discord application's bot user (never a user token).",
        ),
    ),
    metadata_fields=(
        MetadataField(
            "guild_id",
            True,
            "Snowflake of the Discord guild (server) the bot serves; the bot must be a member.",
            pattern=SNOWFLAKE_PATTERN,
            max_length=20,
        ),
        MetadataField(
            "managed_roles",
            False,
            "Roles the bot may grant or revoke, as an object of role key -> role snowflake "
            "(keys [a-z][a-z0-9_]{0,31}, at most 32). Stored as 'key:snowflake,...'.",
            max_length=_MANAGED_ROLES_MAX_LENGTH,
            normalizer=normalize_managed_roles,
        ),
    ),
    public_metadata_keys=frozenset(
        {
            "guild_id",
            "application_id",
            "managed_roles",
            "bot_user_id",
            "bot_username",
            "scopes",
            "capabilities",
        }
    ),
    runtime_metadata_keys=frozenset(
        {"guild_id", "application_id", "managed_roles", "bot_user_id", "scopes", "capabilities"}
    ),
    refreshable=False,
    binding_metadata=(
        BindingKey("guild_id", "guild_id", required=True),
        BindingKey("managed_roles", "managed_roles"),
        BindingKey("application_id", "application_id"),
    ),
)


class DiscordAdapter:
    """Discord implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = DISCORD_SPEC

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Build the adapter (``transport`` swaps the socket in tests; the guard still runs)."""
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Strip an accidental ``Bot `` prefix pasted along with the token."""
        token = credentials.access_token
        if token.lower().startswith("bot "):
            token = token[4:].strip()
        return Credentials(access_token=token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """A bot token carries no OAuth scopes: it grants exactly the spec's capabilities."""
        return self.spec.capabilities

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """``GET /users/@me`` with the ``Bot`` scheme -- must be a bot user, not a person."""
        response = await self._send("validate", f"{DISCORD_API_BASE}/users/@me", credentials)
        if response.status_code == 401:
            raise CredentialsRejected("Discord rejected the bot token")
        if response.status_code != 200:
            logger.warning("discord.validate_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Discord could not validate the token right now")
        payload = _json_object(response)
        user_id = payload.get("id")
        username = payload.get("username")
        if not (isinstance(user_id, str) and _SNOWFLAKE_RE.fullmatch(user_id)):
            raise ProviderUnavailable("Discord returned an unexpected validate response")
        if payload.get("bot") is not True:
            raise CredentialsRejected("the token does not belong to a Discord bot user")
        return Account(
            account_id=user_id,
            account_label=username if isinstance(username, str) else "",
            app_id=user_id,
            scopes=(),
            expires_at=None,
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """``GET /guilds/{id}`` -- proves the bot is a member of the guild it will act in."""
        guild_id = metadata["guild_id"]
        response = await self._send("resolve", f"{DISCORD_API_BASE}/guilds/{guild_id}", credentials)
        if response.status_code == 401:
            raise CredentialsRejected("Discord rejected the bot token")
        if response.status_code in (403, 404):
            raise ResourceUnresolvable("the bot is not a member of that Discord guild")
        if response.status_code != 200:
            logger.warning("discord.resolve_unexpected_status status=%d", response.status_code)
            raise ProviderUnavailable("Discord could not look up the guild right now")
        if _json_object(response).get("id") != guild_id:
            raise ProviderUnavailable("Discord returned an unexpected guild")
        facts: dict[str, Any] = {
            "guild_id": guild_id,
            "bot_user_id": account.account_id,
            "bot_username": account.account_label,
        }
        return ResolvedResource(resource_id=guild_id, facts=facts)

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Bot tokens do not refresh; reaching this is a framework bug, refused loudly."""
        raise RefreshNotConfigured("Discord bot tokens are not refreshable")

    async def _send(self, call: str, url: str, credentials: Credentials) -> httpx.Response:
        """One guarded, timed, size-bounded GET; transport failure -> ProviderUnavailable."""
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
                    response = await client.get(
                        url, headers={"Authorization": f"Bot {credentials.access_token}"}
                    )
            except httpx.HTTPError as exc:
                logger.warning("discord.request_failed call=%s", call)
                raise ProviderUnavailable("Discord could not be reached") from exc
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ProviderUnavailable("Discord returned an unexpectedly large response")
        logger.debug("discord.call_done call=%s status=%d", call, response.status_code)
        return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Decode a JSON object body or raise :class:`ProviderUnavailable` (never echoes the body)."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderUnavailable("Discord returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise ProviderUnavailable("Discord returned a malformed response")
    return payload
