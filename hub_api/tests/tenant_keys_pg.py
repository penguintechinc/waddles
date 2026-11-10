"""Shared real-Postgres fixtures for the tenant at-rest key tests (provisioning + DEK broker).

One Postgres 17 container, migrated to Alembic ``head`` (production DDL, including the
``keystore`` schema), plus the REAL :class:`TenantEnvelopeService` over it with the real platform
KEK adapter. Only the KEK failure case swaps in an adapter that raises -- the same fault a real
KMS outage produces. Skipped (not failed) where the ``docker`` CLI is unavailable.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path

import psycopg2
import pytest

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from penguin_dal import AsyncDB  # noqa: E402
from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.bundle_install_dal import build_install_dal  # noqa: E402
from services.envelope import TenantEnvelopeService  # noqa: E402
from services.envelope.errors import KmsUnavailableError  # noqa: E402
from services.envelope.kms_adapter import KmsProviderRegistry  # noqa: E402
from tests.envelope.fakes import CountingPlatformKek, FakeGate  # noqa: E402

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

KEK = b"\x07" * 32


class DownKek(CountingPlatformKek):
    """A platform KEK whose KMS is unreachable: every wrap/unwrap raises like a real outage."""

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Always fail."""
        raise KmsUnavailableError("kms unreachable")

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """Always fail."""
        raise KmsUnavailableError("kms unreachable")


def make_service(dal: AsyncDB, kek: CountingPlatformKek | None = None) -> TenantEnvelopeService:
    """Real envelope service over the real repository, platform-KEK only (no BYOK providers)."""
    from services.envelope.repository import PenguinDalEnvelopeRepository

    repo = PenguinDalEnvelopeRepository(dal)
    adapter = kek or CountingPlatformKek(KEK)
    return TenantEnvelopeService(
        keys=repo,
        configs=repo,
        registry=KmsProviderRegistry(),
        platform_kek=lambda: adapter,
        gate=FakeGate(),
    )


@pytest.fixture(scope="module")
def tk_pg() -> Iterator[PgTestDatabase]:
    """A fresh, fully migrated Postgres 17 container for this module."""
    with migrated_postgres(f"tenant-keys-{uuid.uuid4().hex[:8]}") as db:
        yield db


@pytest.fixture(scope="module")
def tk_sql(tk_pg: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """A superuser autocommit connection for seeding and raw assertions."""
    conn = psycopg2.connect(tk_pg.dsn)
    conn.autocommit = True
    # The shared alembic harness bootstraps a bare `tenants` table (see pg_docker._BOOTSTRAP_SQL);
    # production's comes from config/postgres/migrations/058_tenants_and_claims.sql. Mirror the
    # two things 058 adds that provisioning depends on: NOT NULL display_name + the seeded global
    # catch-all tenant (is_global is added by alembic 0034).
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS display_name VARCHAR(255) "
            "NOT NULL DEFAULT ''"
        )
        cur.execute("ALTER TABLE tenants ALTER COLUMN display_name DROP DEFAULT")
        cur.execute(
            "INSERT INTO tenants (slug, display_name, is_global) "
            "VALUES ('global', 'Waddles', TRUE) ON CONFLICT (slug) DO NOTHING"
        )
    yield conn
    conn.close()


@pytest.fixture
async def tk_dal(tk_pg: PgTestDatabase) -> AsyncIterator[AsyncDB]:
    """The reflected penguin-dal handle the envelope runtime uses in production."""
    handle = await build_install_dal(tk_pg.dsn, pool_size=4)
    yield handle
    await handle.close()


def scalar(conn: psycopg2.extensions.connection, query: str, params: tuple = ()) -> object:  # type: ignore[type-arg]
    """First column of the first row (or None)."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row[0] if row else None


def new_tenant(conn: psycopg2.extensions.connection) -> int:
    """Insert a uniquely named tenant row and return its id."""
    slug = f"t-{uuid.uuid4().hex[:12]}"
    return int(
        scalar(  # type: ignore[arg-type]
            conn,
            "INSERT INTO tenants (slug, display_name) VALUES (%s, %s) RETURNING id",
            (slug, slug),
        )
    )


def key_rows(conn: psycopg2.extensions.connection, tenant_id: int) -> int:
    """Number of key-store rows (all versions) for a tenant."""
    return int(
        scalar(  # type: ignore[arg-type]
            conn,
            "SELECT count(*) FROM keystore.tenant_encryption_keys WHERE tenant_id = %s",
            (tenant_id,),
        )
    )
