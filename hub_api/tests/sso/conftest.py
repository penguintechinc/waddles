"""Fixtures for the enterprise SSO suite.

Real-path principle (testing standards: "mock only the IdP socket"): everything
between the HTTP request and the database is real -- the Quart blueprints, the
`tenant_middleware`/`require_scope` auth chain, the real two-gate entitlement
client (only its PostHog/license *backends* are faked, so the tier catalog
genuinely enforces), the service layer, penguin-dal + pydal against a file-backed
sqlite database, AES-GCM secret storage, the SSRF guard, and the OIDC/SAML
protocol code. What is replaced: the IdP socket (`httpx.MockTransport`), Redis
(an in-process fake of the three commands used), DNS (public addresses for the
fake IdP hostnames, so the real guard logic still runs and still blocks private
ones), and the entitlement backends.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest
from flask_core import entitlement
from flask_core.database import AsyncDAL
from flask_core.entitlement import EntitlementClient
from penguin_dal import AsyncDB
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from app import bridge_session_cookie_to_bearer
from blueprints.v1.auth import auth_bp
from blueprints.v1.sso import SETTINGS_CONFIG_KEY, TRANSPORT_CONFIG_KEY, sso_admin_bp, sso_public_bp
from config import HubAPIConfig
from services import sso_oidc, sso_state
from services.schema import bind_auth_tables
from services.sso_settings import SsoSettings
from tests.conftest import TENANT_SLUG
from tests.sso.idp_fakes import FakeOidcIdp, FakeSamlIdp

#: Valid 256-bit test key -- NEVER a real platform key.
TEST_SSO_KEY = "ab" * 32

#: Hostnames the fake IdPs live on; resolved to a public documentation address.
PUBLIC_TEST_HOSTS = ("idp.example.com", "saml-idp.example.com", "accounts.google.com")
PUBLIC_TEST_ADDR = "93.184.216.34"


class FakeRedis:
    """In-process stand-in for the Redis commands SSO uses: `set` (ex/nx) and `getdel`."""

    def __init__(self) -> None:
        """Start empty."""
        self.store: dict[str, tuple[str, float | None]] = {}
        self.fail = False

    def _live(self, key: str) -> str | None:
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self.store[key]
            return None
        return value

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        if nx and self._live(key) is not None:
            return False
        self.store[key] = (value, time.monotonic() + ex if ex is not None else None)
        return True

    async def getdel(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis down")
        value = self._live(key)
        self.store.pop(key, None)
        return value

    def keys_with_prefix(self, prefix: str) -> list[str]:
        """Test helper: live keys under `prefix`."""
        return [k for k in list(self.store) if k.startswith(prefix) and self._live(k) is not None]


@pytest.fixture(autouse=True)
def _sso_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A valid SSO key, clean OIDC caches, and DNS that resolves the fake IdP hosts publicly."""
    monkeypatch.setenv("SSO_ENCRYPTION_KEY", TEST_SSO_KEY)
    sso_oidc.clear_caches()
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> Any:
        if host in PUBLIC_TEST_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_TEST_ADDR, 0))]
        if host == "private-idp.corp.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake_getaddrinfo)
    yield
    sso_oidc.clear_caches()


@pytest.fixture
def fake_redis() -> FakeRedis:
    """Fresh fake Redis."""
    return FakeRedis()


@pytest.fixture
def oidc_idp() -> FakeOidcIdp:
    """A working OIDC provider at https://idp.example.com."""
    return FakeOidcIdp()


@pytest.fixture
def saml_idp() -> FakeSamlIdp:
    """A working SAML IdP at https://saml-idp.example.com."""
    return FakeSamlIdp()


class _FlagGate:
    """PostHog backend fake: every flag ON unless listed in `off`."""

    def __init__(self) -> None:
        self.off: set[str] = set()

    def is_enabled(self, flag_key: str, distinct_id: str, *, groups: Any = None) -> bool | None:
        return flag_key not in self.off


class _LicenseGate:
    """License-server backend fake: a settable tier."""

    def __init__(self, tier: str = "enterprise") -> None:
        self.tier = tier

    def resolve_tier(self) -> str:
        return self.tier


class Entitlements:
    """Handle tests use to set the tenant tier / flags the REAL entitlement client sees."""

    def __init__(self) -> None:
        """Install an Enterprise-tier client with every flag on."""
        self.flags = _FlagGate()
        self.license = _LicenseGate()
        self._install()

    def _install(self) -> None:
        client = EntitlementClient(flag_gate=self.flags, license_gate=self.license)
        entitlement._default_client = client

    def set_tier(self, tier: str) -> None:
        """Change the tenant's licensed tier (free / professional / enterprise)."""
        self.license.tier = tier
        self._install()  # fresh client: drop cached decisions

    def flag_off(self, flag_key: str) -> None:
        """Turn one PostHog flag OFF."""
        self.flags.off.add(flag_key)
        self._install()


@pytest.fixture
def entitlements() -> Iterator[Entitlements]:
    """Install a real `EntitlementClient` backed by fake PostHog/license gates (Enterprise)."""
    previous = entitlement._default_client
    handle = Entitlements()
    yield handle
    entitlement._default_client = previous


def build_sso_metadata() -> MetaData:
    """SQLAlchemy Core mirror of migration 0049 (sqlite-compatible), plus `audit_log`."""
    metadata = MetaData()
    Table(
        "sso_connections",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("public_id", String(36), nullable=False, unique=True),
        Column("tenant_id", Integer, nullable=False),
        Column("protocol", String(16), nullable=False),
        Column("display_name", String(100), nullable=False),
        Column("enabled", Boolean, nullable=False, server_default="0"),
        Column("config", JSON, nullable=False),
        Column("secret_ciphertext", Text),
        Column("created_by_user_id", Integer),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
        UniqueConstraint("tenant_id", "display_name"),
    )
    Table(
        "sso_identities",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("connection_id", BigInteger, ForeignKey("sso_connections.id"), nullable=False),
        Column("subject", String(512), nullable=False),
        Column("hub_user_id", Integer, nullable=False),
        Column("created_at", DateTime),
        Column("last_login_at", DateTime),
        UniqueConstraint("connection_id", "subject"),
    )
    Table(
        "audit_log",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", Integer),
        Column("action", String(100), nullable=False),
        Column("target_type", String(50)),
        Column("target_id", String(255)),
        Column("details", JSON),
        Column("ip_address", String(45)),
        Column("user_agent", Text),
        Column("created_at", DateTime),
    )
    return metadata


def _create_sso_tables(conn: Any) -> None:
    build_sso_metadata().create_all(conn)


@pytest.fixture
async def sso_db(tmp_path: Any) -> Any:
    """File-backed pydal `AsyncDAL` (hub tables) + penguin-dal SSO tables on the SAME sqlite file.

    Same construction as `tests/conftest.py::auth_db` (real `bind_auth_tables` schema, tables
    forced into existence on the main thread before any worker thread exists) with one
    speed-up: `synchronous=OFF` on the creating connection, which turns ~1 s of fsync'd
    `CREATE TABLE`s into ~30 ms. Durability is irrelevant for a throwaway test file.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'sso_test.db'}", pool_size=1)
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
    dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()

    install_dal = AsyncDB(async_dal.uri, pool_size=1, echo=False)
    async with install_dal.engine.begin() as conn:
        await conn.run_sync(_create_sso_tables)
    await install_dal.reflect()
    async_dal.install_dal = install_dal
    yield async_dal
    await install_dal.close()
    dal.close()


def hub_config(*, callback_base: str = "https://hub.example.com") -> HubAPIConfig:
    """Minimal `HubAPIConfig` for the SSO app (external URL = `callback_base`)."""
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
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url=callback_base,
        frontend_origin="https://app.example.com",
        log_level="INFO",
    )


@pytest.fixture
def sso_settings() -> SsoSettings:
    """Default operator settings for tests (no shared Google client, no private hosts)."""
    return SsoSettings()


@pytest.fixture
def sso_app(
    sso_db: Any, fake_redis: FakeRedis, sso_settings: SsoSettings, entitlements: Entitlements
) -> Quart:
    """The real SSO + auth blueprints wired to the test database, Redis fake and settings."""
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(sso_public_bp)
    app.register_blueprint(sso_admin_bp)
    app.register_blueprint(auth_bp)
    app.before_request(bridge_session_cookie_to_bearer)
    app.config["dal"] = sso_db.dal
    app.config["async_dal"] = sso_db
    app.config["install_dal"] = sso_db.install_dal
    app.config["HUB_API_CONFIG"] = hub_config()
    app.config[sso_state.SSO_REDIS_CONFIG_KEY] = fake_redis
    app.config[SETTINGS_CONFIG_KEY] = sso_settings
    return app


@pytest.fixture
def client(sso_app: Quart) -> Any:
    """Quart test client over `sso_app`."""
    return sso_app.test_client()


def attach_idp(app: Quart, idp: FakeOidcIdp) -> None:
    """Point hub-api's IdP socket at `idp`."""
    app.config[TRANSPORT_CONFIG_KEY] = idp.transport


@pytest.fixture
def kit(
    sso_app: Quart, client: Any, sso_db: Any, oidc_idp: FakeOidcIdp, saml_idp: FakeSamlIdp
) -> Any:
    """Scenario helper bundling the app, client, database and both fake IdPs."""
    from tests.sso.kit import Kit

    # A second, unrelated tenant for cross-tenant (IDOR) assertions.
    sso_db.dal.tenants.insert(slug="other-corp", display_name="Other Corp", is_active=True)
    sso_db.dal.commit()
    return Kit(app=sso_app, client=client, db=sso_db, oidc=oidc_idp, saml=saml_idp)
