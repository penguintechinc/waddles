"""A second, non-Twitch provider ("fakechat") used to prove the framework is provider-agnostic."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from services.provider_connections.adapter import (
    INSTALL_KIND_CREDENTIALS,
    Account,
    AdapterRegistry,
    AdapterSpec,
    CredentialField,
    Credentials,
    MetadataField,
    RefreshedTokens,
    ResolvedResource,
)
from services.provider_connections.errors import CredentialsRejected, ResourceUnresolvable

FAKE_SPEC = AdapterSpec(
    provider="fakechat",
    display_name="FakeChat",
    platform="fakechat",
    resource_type="fake_room",
    install_kind=INSTALL_KIND_CREDENTIALS,
    capabilities=frozenset({"chat_ingest", "events"}),
    required_scopes=("read",),
    credential_fields=(CredentialField("access_token", True, "API key"),),
    metadata_fields=(MetadataField("room", True, "room name", pattern=r"[a-z]{2,12}"),),
    public_metadata_keys=frozenset({"room", "scopes", "capabilities"}),
    runtime_metadata_keys=frozenset({"room", "scopes", "capabilities", "server"}),
    refreshable=False,
)


@dataclass(slots=True)
class FakeChatAdapter:
    """Accepts any token starting with ``tok``; rooms ``lobby`` and ``arena`` exist."""

    spec: AdapterSpec = FAKE_SPEC
    expires_at: datetime | None = None
    rooms: dict[str, str] = field(default_factory=lambda: {"lobby": "r-1", "arena": "r-2"})

    def normalize_credentials(self, credentials: Credentials) -> Credentials:
        """No normalisation needed."""
        return credentials

    def granted_capabilities(self, scopes: Sequence[str]) -> frozenset[str]:
        """``read`` grants chat ingest."""
        return frozenset({"chat_ingest"}) if "read" in scopes else frozenset()

    async def validate_credentials(self, credentials: Credentials) -> Account:
        """Accept ``tok*`` tokens."""
        if not credentials.access_token.startswith("tok"):
            raise CredentialsRejected("FakeChat rejected the key")
        return Account("acct-1", "bot", "app-1", ("read",), self.expires_at)

    async def resolve_resource(
        self, credentials: Credentials, account: Account, metadata: Mapping[str, str]
    ) -> ResolvedResource:
        """Look the room up in the fake directory."""
        room_id = self.rooms.get(metadata["room"])
        if room_id is None:
            raise ResourceUnresolvable("no such room")
        return ResolvedResource(room_id, {"room": metadata["room"], "server": "eu-1"})

    async def refresh(self, refresh_token: str, facts: Mapping[str, Any]) -> RefreshedTokens:
        """Never refreshable."""
        raise AssertionError("fakechat is not refreshable")


def make_fake_registry(adapter: FakeChatAdapter | None = None) -> AdapterRegistry:
    """Registry holding only FakeChat."""
    return AdapterRegistry([adapter or FakeChatAdapter()])
