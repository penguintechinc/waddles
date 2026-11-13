"""GetStreamDek broker against a REAL, fully migrated Postgres (0054 key store + 0101 audit).

Proves on production DDL: stream DEKs land in `keystore.tenant_encryption_keys` with
purpose='ingest-stream' and ONLY wrapped (no DEK / handed-out key bytes anywhere in the row),
versions are stable and rotate, and every decision writes a `keystore.stream_dek_issuances`
row under the RBAC grants 0101 renders. A SKIP means docker is absent -- treat as FAILURE.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.bundle_install_dal import build_install_dal  # noqa: E402
from services.envelope.kms_adapter import KmsProviderRegistry  # noqa: E402
from services.envelope.repository import (  # noqa: E402
    PURPOSE_INGEST_STREAM,
    PenguinDalEnvelopeRepository,
)
from services.envelope.service import TenantEnvelopeService  # noqa: E402
from services.stream_dek_broker import (  # noqa: E402
    OUTCOME_DENIED_SERVICE,
    OUTCOME_ISSUED,
    StreamDekBroker,
    StreamDekRequest,
    StreamDekServiceDeniedError,
    seal_context,
)
from tests.envelope.fakes import CountingPlatformKek, FakeGate  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

INGEST = "spiffe://penguintech.io/alpha/svc-ingest"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres container migrated to head (includes 0054 + 0101)."""
    with migrated_postgres("hub-api-stream-dek") as db:
        yield db


@pytest.fixture(scope="module")
def sql(pg_db: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """Superuser autocommit connection for seeding + assertions."""
    conn = psycopg2.connect(pg_db.dsn)
    conn.autocommit = True
    yield conn
    conn.close()


def _one(conn: psycopg2.extensions.connection, query: str, params: tuple[object, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row[0] if row else None


@pytest.fixture
def tenant(sql: psycopg2.extensions.connection) -> int:
    """A fresh tenant per test."""
    return int(
        _one(
            sql,
            "INSERT INTO tenants (slug) VALUES (%s) RETURNING id",
            (f"t-{uuid.uuid4().hex[:10]}",),
        )
    )


@pytest.fixture
async def broker(pg_db: PgTestDatabase) -> AsyncIterator[StreamDekBroker]:
    """Real broker: penguin-dal on the migrated DB, real stream-lineage envelope service."""
    dal = await build_install_dal(pg_db.dsn, pool_size=4)
    repo = PenguinDalEnvelopeRepository(dal, purpose=PURPOSE_INGEST_STREAM)
    kek = CountingPlatformKek(os.urandom(32))
    keys = TenantEnvelopeService(
        keys=repo,
        configs=PenguinDalEnvelopeRepository(dal),
        registry=KmsProviderRegistry(),
        platform_kek=lambda: kek,
        gate=FakeGate(),
        lineage=PURPOSE_INGEST_STREAM,
    )
    yield StreamDekBroker(dal, keys, ttl_seconds=600)
    await dal.close()


def _unseal(sealed: bytes, client: X25519PrivateKey, context: bytes) -> bytes:
    shared = client.exchange(X25519PublicKey.from_public_bytes(sealed[:32]))
    okm = HKDF(algorithm=hashes.SHA256(), length=44, salt=None, info=context).derive(shared)
    return AESGCM(okm[:32]).decrypt(okm[32:], sealed[32:], context)


def _request(
    client: X25519PrivateKey, tenant: int, version: int = 0, sub: str = INGEST
) -> StreamDekRequest:
    return StreamDekRequest(
        tenant_id=str(tenant),
        purpose=PURPOSE_INGEST_STREAM,
        version=version,
        caller_public_key=client.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ),
        claim_tenant="system",
        caller_sub=sub,
    )


async def test_issue_rotate_and_ciphertext_only_at_rest(
    broker: StreamDekBroker, sql: psycopg2.extensions.connection, tenant: int
) -> None:
    c1 = X25519PrivateKey.generate()
    first = await broker.issue(_request(c1, tenant))
    k1 = _unseal(first.sealed_dek, c1, seal_context("svc-ingest", tenant, PURPOSE_INGEST_STREAM, 1))
    c2 = X25519PrivateKey.generate()
    again = await broker.issue(_request(c2, tenant))
    assert again.version == 1
    assert (
        _unseal(again.sealed_dek, c2, seal_context("svc-ingest", tenant, PURPOSE_INGEST_STREAM, 1))
        == k1
    )

    with sql.cursor() as cur:
        cur.execute(
            "SELECT purpose, dek_version, wrapped_dek, status, kek_kind "
            "FROM keystore.tenant_encryption_keys WHERE tenant_id = %s",
            (tenant,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    purpose, version, wrapped, status, kind = rows[0]
    assert (purpose, version, status, kind) == (PURPOSE_INGEST_STREAM, 1, "active", "platform")
    assert k1 not in bytes(wrapped)  # never plaintext (nor the handed-out key) at rest

    await broker._keys.rotate_dek(tenant, reason="test")  # noqa: SLF001
    c3 = X25519PrivateKey.generate()
    rotated = await broker.issue(_request(c3, tenant))
    assert rotated.version == 2
    k2 = _unseal(
        rotated.sealed_dek, c3, seal_context("svc-ingest", tenant, PURPOSE_INGEST_STREAM, 2)
    )
    assert k2 != k1
    c4 = X25519PrivateKey.generate()
    old = await broker.issue(_request(c4, tenant, version=1))
    assert (
        _unseal(old.sealed_dek, c4, seal_context("svc-ingest", tenant, PURPOSE_INGEST_STREAM, 1))
        == k1
    )
    assert (
        _one(
            sql,
            "SELECT count(*) FROM keystore.tenant_encryption_keys "
            "WHERE tenant_id = %s AND purpose = 'at-rest'",
            (tenant,),
        )
        == 0
    )  # the stream lineage never provisions at-rest keys

    with sql.cursor() as cur:
        cur.execute(
            "SELECT outcome, dek_version, caller_service, purpose "
            "FROM keystore.stream_dek_issuances WHERE tenant_id = %s ORDER BY id",
            (tenant,),
        )
        audit = cur.fetchall()
    assert audit == [
        (OUTCOME_ISSUED, 1, "svc-ingest", PURPOSE_INGEST_STREAM),
        (OUTCOME_ISSUED, 1, "svc-ingest", PURPOSE_INGEST_STREAM),
        (OUTCOME_ISSUED, 2, "svc-ingest", PURPOSE_INGEST_STREAM),
        (OUTCOME_ISSUED, 1, "svc-ingest", PURPOSE_INGEST_STREAM),
    ]


async def test_denial_is_audited(
    broker: StreamDekBroker, sql: psycopg2.extensions.connection
) -> None:
    before = int(
        _one(
            sql,
            "SELECT count(*) FROM keystore.stream_dek_issuances WHERE outcome = %s",
            (OUTCOME_DENIED_SERVICE,),
        )
    )
    with pytest.raises(StreamDekServiceDeniedError):
        await broker.issue(
            _request(X25519PrivateKey.generate(), 1, sub="spiffe://penguintech.io/alpha/svc-action")
        )
    after = int(
        _one(
            sql,
            "SELECT count(*) FROM keystore.stream_dek_issuances WHERE outcome = %s",
            (OUTCOME_DENIED_SERVICE,),
        )
    )
    assert after == before + 1


def test_issued_row_must_be_complete(sql: psycopg2.extensions.connection) -> None:
    with pytest.raises(psycopg2.errors.CheckViolation), sql.cursor() as cur:
        cur.execute("INSERT INTO keystore.stream_dek_issuances (outcome) VALUES (1)")


def test_rbac_hub_api_append_only_and_data_plane_locked_out(
    sql: psycopg2.extensions.connection,
) -> None:
    table = "keystore.stream_dek_issuances"

    def can(role: str, privilege: str) -> bool:
        return bool(_one(sql, "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)))

    assert can("hub_api", "SELECT") and can("hub_api", "INSERT")
    assert not can("hub_api", "UPDATE") and not can("hub_api", "DELETE")
    for role in (
        "waddles_publisher",
        "svc_ingest",
        "svc_process",
        "svc_action",
        "svc_streaming",
        "webui",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not can(role, privilege), f"{role} must not {privilege} {table}"
