"""Fixtures for the provider/connection framework suite.

Real-path principle (testing standards: "mock only the socket"). Everything between the HTTP request
and the database is real: Quart blueprints, ``tenant_middleware`` / ``require_scope`` /
``authorize_community`` (DB-backed community admin check), the real two-gate entitlement client
(only its PostHog/licence *backends* are faked, so the tier catalog genuinely enforces), the real
service, adapter, vault (AES-256-GCM) and store over penguin-dal + pydal on a file-backed sqlite
database, and the real machine-JWT verifier. What is replaced: the Twitch socket
(``httpx.MockTransport`` -- ``fake_twitch.py``), DNS (public addresses for the Twitch hosts so the
real SSRF guard still runs), and the entitlement backends.

The sqlite tables below mirror migration 0035 + 0058 column-for-column;
``test_pg_integration.py`` asserts that mirror against the real migrated Postgres schema so the
two cannot drift.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask_core import entitlement
from flask_core.database import AsyncDAL
from flask_core.service_jwt import (
    ServiceIdentity,
    ServiceJwtIssuer,
    SigningKey,
)
from penguin_dal import AsyncDB
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

from blueprints.v1.provider_connections import (
    SERVICE_CONFIG_KEY,
    provider_connections_bp,
    provider_connections_internal_bp,
)
from config import HubAPIConfig
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.static_secret import (
    AwsLambdaAdapter,
    ExternalHttpAdapter,
    GcpFunctionAdapter,
)
from services.provider_connections.twitch import TwitchAdapter
from services.schema import bind_auth_tables, bind_community_authz_tables
from tests.conftest import SECRET_KEY, TENANT_SLUG, make_user_token
from tests.provider_connections.fake_twitch import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    FakeAccount,
    FakeTwitch,
)
from tests.sso.conftest import Entitlements

#: Valid 256-bit test key -- NEVER a real platform key.
TEST_CREDENTIAL_KEY = "cd" * 32
OTHER_TENANT_SLUG = "other-corp"
ADMIN_USER_ID = 7
BOT = FakeAccount(user_id="9001", login="waddlebots")
CHANNEL_LOGIN = "penguinzplays"
CHANNEL_ID = "7001"
OTHER_CHANNEL_LOGIN = "othercast"
OTHER_CHANNEL_ID = "7002"
SYSTEM_SERVICE_ID = "spiffe://penguintech.io/alpha/svc-ingest"
SERVICE_AUDIENCE = "waddlebot-internal"


@pytest.fixture(autouse=True)
def _pc_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A valid at-rest key, the platform Twitch app, and DNS that resolves Twitch publicly."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", TEST_CREDENTIAL_KEY)
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> Any:
        if host in ("id.twitch.tv", "api.twitch.tv"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake_getaddrinfo)
    yield


class FakeClock:
    """A controllable UTC clock shared by the adapter and the service."""

    def __init__(self) -> None:
        """Start at a fixed instant."""
        self.now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Current fake time."""
        return self.now

    def advance(self, **kwargs: float) -> None:
        """Move time forward (``seconds=``, ``hours=`` ...)."""
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    """Shared fake clock."""
    return FakeClock()


@pytest.fixture
def fake_twitch() -> FakeTwitch:
    """A fake Twitch that knows the bot and two channels."""
    twitch = FakeTwitch()
    twitch.channels = {CHANNEL_LOGIN: CHANNEL_ID, OTHER_CHANNEL_LOGIN: OTHER_CHANNEL_ID}
    return twitch


@pytest.fixture
def bot_tokens(fake_twitch: FakeTwitch) -> tuple[str, str | None]:
    """A fresh ``(access, refresh)`` pair for the ``waddlebots`` bot (chat:read + chat:edit)."""
    return fake_twitch.mint(account=BOT)


def build_pc_metadata() -> MetaData:
    """SQLAlchemy Core mirror of migrations 0035 + 0058 (sqlite-compatible)."""
    metadata = MetaData()
    Table(
        "platform_connections",
        metadata,
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
        Column("tenant_id", Integer, nullable=False),
        Column("platform", String(50), nullable=False),
        Column("resource_type", String(20), nullable=False),
        Column("resource_id", String(255), nullable=False),
        # migration 0064: nullable -- a static-secret row has no OAuth token (sealed_secret instead).
        Column("access_token", Text),
        Column("refresh_token", Text),
        Column("status", String(20), nullable=False, server_default="active"),
        Column("installed_by_user_id", Integer),
        Column("resource_metadata", JSON, nullable=False, server_default="{}"),
        Column("expires_at", DateTime(timezone=True)),
        Column("status_reason", String(40)),
        # migration 0063: the row's AAD identity + which seal layout its tokens use.
        Column("public_id", String(36), nullable=False, unique=True),
        Column("seal_version", Integer, nullable=False, server_default="0"),
        # migration 0064: int kind authority (1 oauth, 2 external_http, 3 aws_lambda, 4 gcp_function)
        # + the AAD-sealed static-secret document.
        Column("connection_kind", Integer, nullable=False, server_default="1"),
        Column("sealed_secret", Text),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
        UniqueConstraint("tenant_id", "platform", "resource_id"),
        CheckConstraint("status IN ('active', 'revoked', 'expired')"),
        CheckConstraint(
            "resource_type IN ('discord_guild', 'twitch_channel', 'fake_room', "
            "'external_http', 'aws_lambda', 'gcp_function')"
        ),
        CheckConstraint("connection_kind BETWEEN 1 AND 4"),
        CheckConstraint(
            "(connection_kind = 1 AND access_token IS NOT NULL AND sealed_secret IS NULL) OR "
            "(connection_kind <> 1 AND access_token IS NULL AND refresh_token IS NULL "
            "AND sealed_secret IS NOT NULL)"
        ),
    )
    Table(
        "community_connection_access",
        metadata,
        Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
        Column("community_id", Integer, nullable=False),
        Column(
            "connection_id",
            BigInteger().with_variant(Integer, "sqlite"),
            ForeignKey("platform_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("status", String(20), nullable=False, server_default="pending"),
        Column("enabled", Boolean, nullable=False, server_default="1"),
        Column("requested_by_user_id", Integer),
        Column("approved_by_user_id", Integer),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.current_timestamp(),
        ),
        UniqueConstraint("community_id", "connection_id"),
        CheckConstraint("status IN ('pending', 'approved', 'revoked')"),
    )
    return metadata


class Seed:
    """Ids of the rows every test starts from."""

    def __init__(self) -> None:
        """Filled in by the ``pc_db`` fixture."""
        self.tenant_id = 0
        self.other_tenant_id = 0
        self.community_id = 0
        self.second_community_id = 0
        self.other_community_id = 0
        self.admin_user_id = 0


@pytest.fixture
async def pc_db(tmp_path: Any) -> Any:
    """File-backed pydal ``AsyncDAL`` (hub tables) + penguin-dal connection tables, one sqlite file.

    Two tenants (``acme-corp`` with two communities, ``other-corp`` with one) and a tenant-owner
    admin (``ADMIN_USER_ID``) for ``acme-corp`` only.
    """
    db_path = tmp_path / "pc_test.db"
    async_dal = AsyncDAL(f"sqlite://{db_path}", pool_size=1)
    dal = async_dal.dal
    dal.executesql("PRAGMA synchronous=OFF")
    dal.executesql("PRAGMA journal_mode=MEMORY")
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    seed = Seed()
    seed.tenant_id = int(dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme", is_active=True))
    seed.other_tenant_id = int(
        dal.tenants.insert(slug=OTHER_TENANT_SLUG, display_name="Other", is_active=True)
    )
    seed.community_id = int(dal.communities.insert(name="acme-main", tenant_id=seed.tenant_id))
    seed.second_community_id = int(
        dal.communities.insert(name="acme-second", tenant_id=seed.tenant_id)
    )
    seed.other_community_id = int(
        dal.communities.insert(name="other-main", tenant_id=seed.other_tenant_id)
    )
    admin_id = int(dal.hub_users.insert(username="tenant-owner"))
    dal.tenant_admins.insert(tenant_id=seed.tenant_id, user_id=admin_id, role="tenant-owner")
    seed.admin_user_id = admin_id
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()

    install_dal = AsyncDB(async_dal.uri, pool_size=1, echo=False)
    async with install_dal.engine.begin() as conn:
        await conn.run_sync(build_pc_metadata().create_all)
    await install_dal.reflect()
    async_dal.install_dal = install_dal
    async_dal.seed = seed
    async_dal.db_path = db_path
    yield async_dal
    await install_dal.close()
    dal.close()


def make_registry(fake: FakeTwitch, clock: Callable[[], datetime]) -> AdapterRegistry:
    """Registry with the real Twitch adapter pointed at the fake socket and the test app creds."""
    return AdapterRegistry(
        [
            TwitchAdapter(
                transport=fake.transport,
                app_credentials=lambda: (APP_CLIENT_ID, APP_CLIENT_SECRET),
                clock=clock,
            )
        ]
    )


def make_static_registry(fake: FakeTwitch, clock: Callable[[], datetime]) -> AdapterRegistry:
    """Twitch (on the fake socket) plus the three static-secret adapters (no network at all)."""
    registry = make_registry(fake, clock)
    registry.register(ExternalHttpAdapter())
    registry.register(AwsLambdaAdapter())
    registry.register(GcpFunctionAdapter())
    return registry


@pytest.fixture
def pc_static_service(
    pc_db: Any, fake_twitch: FakeTwitch, clock: FakeClock
) -> ProviderConnectionService:
    """The real service with the static-secret providers registered."""
    return ProviderConnectionService(
        pc_db.install_dal, make_static_registry(fake_twitch, clock), PlatformKeyVault(), clock=clock
    )


@pytest.fixture
def pc_service(pc_db: Any, fake_twitch: FakeTwitch, clock: FakeClock) -> ProviderConnectionService:
    """The real service over the sqlite DB, real vault, real Twitch adapter on the fake socket."""
    return ProviderConnectionService(
        pc_db.install_dal, make_registry(fake_twitch, clock), PlatformKeyVault(), clock=clock
    )


def hub_config() -> HubAPIConfig:
    """Minimal ``HubAPIConfig`` for the test app."""
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key=SECRET_KEY,
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="https://hub.example.com",
        frontend_origin="https://app.example.com",
        log_level="INFO",
    )


@pytest.fixture
def entitlements() -> Iterator[Entitlements]:
    """A real ``EntitlementClient`` (Professional tier, every flag on) over fake backends."""
    previous = entitlement._default_client
    handle = Entitlements()
    handle.set_tier("professional")
    yield handle
    entitlement._default_client = previous


@pytest.fixture
def service_issuer() -> ServiceJwtIssuer:
    """A real Ed25519 machine-JWT issuer allow-listing the data plane for ``connections:resolve``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    identity = ServiceIdentity(
        service_id=SYSTEM_SERVICE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-ingest",
        allowed_scopes=frozenset({"connections:resolve", "other:scope"}),
        tenant="system",
    )
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={SYSTEM_SERVICE_ID: identity},
        audience=SERVICE_AUDIENCE,
    )


@pytest.fixture
def pc_app(
    pc_db: Any,
    pc_service: ProviderConnectionService,
    service_issuer: ServiceJwtIssuer,
    entitlements: Entitlements,
) -> Quart:
    """The real blueprints + auth chain wired to the test database and service."""
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(provider_connections_bp)
    app.register_blueprint(provider_connections_internal_bp)
    app.config["dal"] = pc_db.dal
    app.config["async_dal"] = pc_db
    app.config["install_dal"] = pc_db.install_dal
    app.config["HUB_API_CONFIG"] = hub_config()
    app.config[SERVICE_CONFIG_KEY] = pc_service
    app.config["SERVICE_JWT_VERIFIER"] = service_issuer.as_verifier()
    return app


@pytest.fixture
def client(pc_app: Quart) -> Any:
    """Quart test client over ``pc_app``."""
    return pc_app.test_client()


@pytest.fixture
def static_client(pc_app: Quart, pc_static_service: ProviderConnectionService) -> Any:
    """Test client whose service also has the three static-secret providers registered."""
    pc_app.config[SERVICE_CONFIG_KEY] = pc_static_service
    return pc_app.test_client()


def admin_headers(
    pc_db: Any,
    *,
    scope: str = "connections:admin",
    tenant: str = TENANT_SLUG,
    user_id: int | None = None,
) -> dict[str, str]:
    """Bearer header for a tenant-owner of ``acme-corp`` carrying ``scope`` (JSON content type)."""
    uid = user_id if user_id is not None else pc_db.seed.admin_user_id
    return {
        "Authorization": f"Bearer {make_user_token(user_id=uid, scope=scope, tenant=tenant)}",
        "Content-Type": "application/json",
    }
