"""Game-server adapter: a community's registered game server (host, port, RCON password).

The data plane's ``gameserver-net`` host capability (``core/bundle_host_gameserver_net``) is the ONLY
consumer: the guest bundle names a server by the opaque ``server_id`` UUID and the host resolves the
sealed credential here, so neither the address nor the RCON password ever reaches a guest.

* **Credential** -- one sealed string ``host|port|rcon_password`` (the framework's single
  ``access_token`` slot, sealed at rest like every connection credential, never returned by any admin
  route, never logged). The host and port are sealed WITH the password on purpose: an admin response
  can never disclose where a community's game server lives. ``host`` is a DNS name or IP literal,
  ``port`` 1-65535, the password printable ASCII without whitespace (possibly empty for a server that
  is only queried over A2S / Minecraft ping). Split on the first two ``|`` so the password may itself
  contain ``|``.
* **Policy at write time** -- the host must resolve ONLY to publicly routable addresses
  (:func:`services.url_guard.is_private_host`, fail closed on resolution failure). This is a
  convenience check; the authoritative guard is the host capability's resolve-once-and-connect-to-that-IP
  SSRF guard, which runs on every connection.
* **Resource** -- ``metadata.server_id`` (a UUID minted by the gateway route), non-secret; it is the
  connection's ``resource_id`` and what a bundle's ``register`` binds policy to.

Not refreshable; no outbound call is made here (admin-supplied hosts are never probed from the write
path by this adapter -- the explicit ``test`` route does that, pinned and guarded).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

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
    RefreshNotConfigured,
)
from services.provider_connections.gameserver_probe import resolve_pinned

PROVIDER_ID: Final = "gameserver"
RESOURCE_TYPE_SERVER: Final = "gameserver"
CAPABILITY_GAMESERVER_NET: Final = "gameserver_net"

#: Canonical lowercase UUID (the only spelling the host directory accepts).
SERVER_ID_PATTERN: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GAME_PATTERN: Final = r"[a-z0-9_]{1,32}"
LABEL_PATTERN: Final = r"[A-Za-z0-9 _.\-]{1,64}"
MAX_PASSWORD_LEN: Final = 1024

_HOST_RE: Final = re.compile(r"[A-Za-z0-9.\-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]")

GAMESERVER_SPEC: Final = AdapterSpec(
    provider=PROVIDER_ID,
    display_name="Game server",
    platform=PROVIDER_ID,
    resource_type=RESOURCE_TYPE_SERVER,
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({CAPABILITY_GAMESERVER_NET}),
    required_scopes=(),
    credential_fields=(
        CredentialField(
            name="access_token",
            required=True,
            description=(
                "host|port|rcon_password (sealed; host and port are never shown again). "
                "The password may be empty for a server that is only queried, not administered."
            ),
        ),
    ),
    metadata_fields=(
        MetadataField(
            name="server_id",
            required=True,
            description="Opaque UUID naming this server to bundles (minted by the gateway)",
            pattern=SERVER_ID_PATTERN,
            max_length=36,
        ),
        MetadataField(
            name="game",
            required=False,
            description="Game family label, e.g. source or minecraft (display only)",
            pattern=GAME_PATTERN,
            max_length=32,
            lowercase=True,
        ),
        MetadataField(
            name="label",
            required=False,
            description="Human label shown to the community (display only)",
            pattern=LABEL_PATTERN,
            max_length=64,
        ),
    ),
    public_metadata_keys=frozenset({"server_id", "game", "label"}),
    runtime_metadata_keys=frozenset({"server_id", "game"}),
    refreshable=False,
    binding_metadata=(
        BindingKey(wire="server_id", stored="server_id", required=True),
        BindingKey(wire="game", stored="game"),
    ),
)


def parse_sealed_target(token: str) -> tuple[str, int, str]:
    """Split ``host|port|password``; raise :class:`InvalidConnectionInput` (value never echoed)."""
    parts = token.split("|", 2)
    if len(parts) != 3:
        raise InvalidConnectionInput("credential must be host|port|rcon_password")
    host, port_text, password = parts
    if not _HOST_RE.fullmatch(host):
        raise InvalidConnectionInput("game-server host is not a valid DNS name or IP literal")
    if not (port_text.isascii() and port_text.isdigit() and 1 <= int(port_text) <= 65535):
        raise InvalidConnectionInput("game-server port must be 1-65535")
    if len(password) > MAX_PASSWORD_LEN:
        raise InvalidConnectionInput("rcon password is too long")
    return host, int(port_text), password


def build_sealed_target(host: str, port: int, password: str) -> str:
    """Compose the sealed credential string the host capability parses (validated on the way in)."""
    if "|" in host:
        raise InvalidConnectionInput("game-server host is not a valid DNS name or IP literal")
    token = f"{host}|{port}|{password}"
    parse_sealed_target(token)
    return token


class GameServerAdapter:
    """Game-server implementation of :class:`~services.provider_connections.adapter.ProviderAdapter`."""

    spec: AdapterSpec = GAMESERVER_SPEC

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """Validate the sealed ``host|port|password`` shape; keep it verbatim."""
        parse_sealed_target(credentials.access_token)
        return Credentials(access_token=credentials.access_token)

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """No OAuth scopes: the credential grants exactly the spec's capability."""
        return self.spec.capabilities

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Refuse a host that is, or resolves to, a non-public address (no probing)."""
        host, port, _password = parse_sealed_target(credentials.access_token)
        if await resolve_pinned(host, port) is None:
            raise CredentialsRejected("the game-server address must be publicly routable")
        return Account(
            account_id="gameserver",
            account_label="Game server",
            app_id="",
            scopes=(),
            expires_at=None,
        )

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """The server is whatever ``server_id`` the gateway minted; nothing to look up remotely."""
        server_id = metadata["server_id"]
        facts: dict[str, Any] = {"server_id": server_id}
        for key in ("game", "label"):
            if metadata.get(key):
                facts[key] = metadata[key]
        return ResolvedResource(resource_id=server_id, facts=facts)

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """A sealed host/port/password has no refresh flow."""
        raise RefreshNotConfigured("game-server connections are not refreshable")
