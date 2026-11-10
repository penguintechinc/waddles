"""YouTube fixtures: fake Google, fake Redis (state store), real service + per-tenant app creds.

Imported (by name) into the YouTube test modules -- pytest resolves a fixture from the importing
module's namespace. Real-path: the service, vault, store, adapter, install flow, credential
resolver and blueprints are all real; only the Google socket (``httpx.MockTransport``), DNS (public
addresses for the Google hosts, via the autouse ``_pc_environment`` fixture) and Redis are faked.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydal import Field
from quart import Quart

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from blueprints.v1.youtube_connections import youtube_connections_bp
from services.credential_resolver import store_tenant_credentials
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.youtube import YoutubeAdapter
from services.youtube_install import make_app_credentials
from tests.provider_connections.conftest import FakeClock, make_registry
from tests.provider_connections.fake_google import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    FakeChannel,
    FakeGoogle,
)
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.grpc_internal_client import GrpcRoutingClient

YT_CHANNEL = FakeChannel("UCabc123_-xyz", "Penguin Plays")
YT_OTHER_CHANNEL = FakeChannel("UCother456_-qrs", "Other Cast")
YT_CHAT = "Cg0KC2xpdmVDaGF0MDE"


class FakeRedis:
    """Async stand-in for the Valkey commands the install flow uses (``set`` + ``getdel``)."""

    def __init__(self) -> None:
        """Start empty; ``store`` maps key -> (value, expires-at monotonic | None)."""
        self.store: dict[str, tuple[str, float | None]] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        """``SET key value [EX n] [NX]`` -- ``None`` when ``nx`` and the key is live."""
        live = self.store.get(key)
        if nx and live is not None and (live[1] is None or live[1] > time.monotonic()):
            return None
        self.store[key] = (value, time.monotonic() + ex if ex is not None else None)
        return True

    async def get(self, key: str) -> str | None:
        """Non-destructive read; an expired entry reads as absent."""
        entry = self.store.get(key)
        if entry is None or (entry[1] is not None and entry[1] < time.monotonic()):
            return None
        return entry[0]

    async def getdel(self, key: str) -> str | None:
        """Atomic get-and-delete; an expired entry reads as absent."""
        entry = self.store.pop(key, None)
        if entry is None or (entry[1] is not None and entry[1] < time.monotonic()):
            return None
        return entry[0]

    def force_expire(self, key: str) -> None:
        """Make ``key`` already-expired."""
        value, _ = self.store[key]
        self.store[key] = (value, time.monotonic() - 1)


@pytest.fixture
def fake_google() -> FakeGoogle:
    """A fake Google (no broadcasts until a test adds some)."""
    return FakeGoogle()


@pytest.fixture
def yt_tokens(fake_google: FakeGoogle) -> tuple[str, str | None]:
    """A fresh ``(access, refresh)`` pair for :data:`YT_CHANNEL` (force-ssl)."""
    return fake_google.mint(channel=YT_CHANNEL)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Route the install flow's Redis client to an in-memory fake."""
    redis = FakeRedis()
    monkeypatch.setattr("services.youtube_install._redis_client", lambda: redis)
    return redis


@pytest.fixture
def yt_db(pc_db: Any) -> Any:
    """``pc_db`` + the ``tenant_platform_apps`` table + a default (``global``) tenant lane."""
    dal = pc_db.dal
    dal.define_table(
        "tenant_platform_apps",
        Field("tenant_id", "integer", notnull=True),
        Field("platform", "string", length=50, notnull=True),
        Field("credentials_ciphertext", "text", notnull=True),
        Field("key_ref", "string", length=255),
        Field("installed_by_user_id", "integer"),
        Field("created_at", "datetime"),
        Field("updated_at", "datetime"),
    )
    pc_db.global_tenant_id = int(
        dal.tenants.insert(slug="global", display_name="Global", is_active=True, is_global=True)
    )
    pc_db.global_community_id = int(
        dal.communities.insert(name="global-main", tenant_id=pc_db.global_tenant_id)
    )
    pc_db.global_admin_id = int(dal.hub_users.insert(username="global-owner"))
    dal.tenant_admins.insert(
        tenant_id=pc_db.global_tenant_id, user_id=pc_db.global_admin_id, role="tenant-owner"
    )
    dal.commit()
    return pc_db


@pytest.fixture
def yt_service(
    yt_db: Any, fake_google: FakeGoogle, fake_twitch: FakeTwitch, clock: FakeClock
) -> ProviderConnectionService:
    """Real service; YouTube adapter on the fake socket with REAL per-tenant app resolution."""
    registry = make_registry(fake_twitch, clock)
    registry.register(
        YoutubeAdapter(
            transport=fake_google.transport,
            app_credentials=make_app_credentials(yt_db.dal),
            clock=clock,
        )
    )
    return ProviderConnectionService(yt_db.install_dal, registry, PlatformKeyVault(), clock=clock)


@pytest.fixture
def acme_google_app(yt_db: Any) -> None:
    """acme-corp's OWN Google OAuth application, stored the way an earlier install would."""
    store_tenant_credentials(
        yt_db.dal,
        tenant_id=yt_db.seed.tenant_id,
        is_global_tenant=False,
        platform="youtube",
        payload={"client_id": APP_CLIENT_ID, "client_secret": APP_CLIENT_SECRET},
        installed_by_user_id=yt_db.seed.admin_user_id,
    )


@pytest.fixture
def yt_app(
    pc_app: Quart,
    yt_db: Any,
    yt_service: ProviderConnectionService,
    fake_redis: FakeRedis,
    fake_google: FakeGoogle,
    acme_google_app: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Quart:
    """``pc_app`` + the YouTube blueprint, the YouTube-capable service and the fake code socket."""
    pc_app.register_blueprint(youtube_connections_bp)
    pc_app.config[SERVICE_CONFIG_KEY] = yt_service
    monkeypatch.setattr("services.youtube_install._transport", fake_google.transport)
    return pc_app


@pytest.fixture
def yt_client(yt_app: Quart) -> Any:
    """Test client over :func:`yt_app`; internal data-plane reads go over real gRPC."""
    return GrpcRoutingClient(yt_app.test_client())
