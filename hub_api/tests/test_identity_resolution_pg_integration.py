"""Identity resolution (#429) against a REAL, fully-migrated Postgres -- no mocked boundary.

Covers alembic 0045's `resolve_identity_uuid()` / membership trigger / backfill / view,
the `identity_resolution_service` batch path, and the real `IdentityServicer` gRPC
adapter end to end (in-process `grpc.aio` server, real `AsyncDAL`). Reuses
`alembic/tests/pg_docker.py`'s `migrated_postgres()`; skipped (never failed) where the
`docker` CLI is unavailable, like every other real-Postgres test in this repo.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import grpc
import pytest
from flask_core.database import AsyncDAL
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from grpc_internal.servicers import IdentityServicer  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    IdentityRequest,
    IdentityResolutionError,
    IdentityValidationError,
    TenantNotFoundError,
    resolve_identities,
    resolve_identity,
)

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

SECRET_HANDLE = "SensitiveHandle_do_not_log"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0045)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-identity-resolution") as db:
        yield db


@pytest.fixture
async def adal(pg_db: PgTestDatabase) -> AsyncIterator[AsyncDAL]:
    """A real flask_core AsyncDAL on the migrated container with clean identity data."""
    dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    await dal.executesql_async(
        "TRUNCATE community_members, ephemeral_pseudonyms, hub_user_identities, "
        "hub_users, communities, tenants RESTART IDENTITY CASCADE"
    )
    yield dal


async def _one(adal: AsyncDAL, sql: str, params: list[Any] | None = None) -> Any:
    rows = await adal.executesql_async(sql, params)
    return rows[0][0] if rows else None


async def _tenant(adal: AsyncDAL, slug: str) -> int:
    return int(await _one(adal, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", [slug]))


async def _community(adal: AsyncDAL, tenant_id: int) -> int:
    return int(
        await _one(
            adal,
            "INSERT INTO communities (name, tenant_id) VALUES ('c', %s) RETURNING id",
            [tenant_id],
        )
    )


async def _hub_user(adal: AsyncDAL) -> tuple[int, str]:
    row = (
        await adal.executesql_async(
            "INSERT INTO hub_users (username) VALUES ('u') RETURNING id, uuid::text"
        )
    )[0]
    return int(row[0]), str(row[1])


async def _member(
    adal: AsyncDAL,
    community_id: int,
    *,
    user_id: str | None = None,
    platform: str | None = None,
    puid: str | None = None,
    display: str | None = None,
) -> Any:
    return await _one(
        adal,
        "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
        "display_name) VALUES (%s, %s, %s, %s, %s) RETURNING user_uuid::text",
        [community_id, user_id, platform, puid, display],
    )


async def test_trigger_populates_uuid_from_hub_user_link(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    uid, hub_uuid = await _hub_user(adal)
    assert await _member(adal, c, user_id=str(uid)) == hub_uuid


async def test_trigger_uses_hub_user_identities_link(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    uid, hub_uuid = await _hub_user(adal)
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'discord', 'd-1')",
        [uid],
    )
    assert await _member(adal, c, platform="discord", puid="d-1") == hub_uuid


async def test_unlinked_identity_gets_stable_per_tenant_pseudonym(adal: AsyncDAL) -> None:
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1a, c1b, c2 = (
        await _community(adal, t1),
        await _community(adal, t1),
        await _community(adal, t2),
    )
    a = await _member(adal, c1a, platform="twitch", puid="tw-9", display=SECRET_HANDLE)
    assert a is not None
    assert await _member(adal, c1b, platform="twitch", puid="tw-9") == a  # same tenant -> same
    other = await _member(adal, c2, platform="twitch", puid="tw-9")
    assert other is not None and other != a  # different tenant -> different
    stored = await _one(
        adal, "SELECT handle FROM ephemeral_pseudonyms WHERE pseudonym = %s::uuid", [a]
    )
    assert stored == SECRET_HANDLE  # handle lives only in the PII-boundary table


async def test_unresolvable_row_stays_null_fail_closed(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    assert await _member(adal, c) is None


async def test_second_account_of_same_hub_user_in_community_stays_null(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    uid, hub_uuid = await _hub_user(adal)
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'discord', 'd'), (%s, 'twitch', 't')",
        [uid, uid],
    )
    assert await _member(adal, c, platform="discord", puid="d") == hub_uuid
    assert await _member(adal, c, platform="twitch", puid="t") is None


async def test_backfill_populates_pre_existing_rows(adal: AsyncDAL) -> None:
    import importlib.util

    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    await adal.executesql_async(
        "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
    )
    await _member(adal, c, platform="discord", puid="legacy-1")
    await adal.executesql_async(
        "ALTER TABLE community_members ENABLE TRIGGER trg_community_members_user_uuid"
    )
    assert await _one(adal, "SELECT user_uuid FROM community_members") is None
    spec = importlib.util.spec_from_file_location(
        "m0045", Path(_ALEMBIC_TESTS_DIR).parent / "versions" / "0045_identity_resolution.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    await adal.executesql_async(mod._BACKFILL_SQL.replace("%", "%%"))
    assert await _one(adal, "SELECT user_uuid FROM community_members") is not None


async def test_view_exposes_user_uuid_and_pseudonym_table_is_unreachable_to_reader(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "t1")
    c = await _community(adal, t)
    got = await _member(adal, c, platform="discord", puid="v-1")
    assert (
        await _one(
            adal,
            "SELECT user_uuid::text FROM community_member_identities WHERE platform_user_id='v-1'",
        )
        == got
    )
    reader_exists = await _one(
        adal, "SELECT 1 FROM pg_roles WHERE rolname = 'waddles_bundle_reader'"
    )
    assert reader_exists == 1
    dal = adal.dal
    dal.executesql("SET ROLE waddles_bundle_reader")
    try:
        dal.executesql("SELECT count(*) FROM community_member_identities")
        with pytest.raises(Exception, match="permission denied"):
            dal.executesql("SELECT * FROM ephemeral_pseudonyms")
    finally:
        dal.rollback()
        dal.executesql("RESET ROLE")
        dal.commit()


async def test_resolve_identities_batch_idempotent_dedup_and_slug_or_id(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    reqs = [
        IdentityRequest("acme", "discord", "1", SECRET_HANDLE),
        IdentityRequest(str(t), "discord", "1"),
        IdentityRequest("acme", "discord", "2"),
    ]
    first = await resolve_identities(adal, reqs)
    assert [r.platform_user_id for r in first] == ["1", "2"]  # deduped on (tenant,plat,id)
    assert first[0].uuid != first[1].uuid
    again = await resolve_identities(adal, reqs)
    assert [r.uuid for r in again] == [r.uuid for r in first]


async def test_resolve_identity_returns_linked_hub_uuid(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    uid, hub_uuid = await _hub_user(adal)
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'discord', '77')",
        [uid],
    )
    assert await resolve_identity(adal, "acme", "discord", "77") == uuid.UUID(hub_uuid)


async def test_membership_uuid_matches_service_resolution(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    member_uuid = await _member(adal, c, platform="twitch", puid="55")
    assert await resolve_identity(adal, "acme", "twitch", "55") == uuid.UUID(member_uuid)


@pytest.mark.parametrize(
    "item",
    [
        IdentityRequest("", "discord", "1"),
        IdentityRequest("acme", "Bad Platform", "1"),
        IdentityRequest("acme", "discord", ""),
        IdentityRequest("acme", "discord", "x" * 256),
        IdentityRequest("acme", "discord", "a\x00b"),
        IdentityRequest("acme", "discord", "1", "h" * 256),
    ],
)
async def test_validation_fails_loud(adal: AsyncDAL, item: IdentityRequest) -> None:
    with pytest.raises(IdentityValidationError):
        await resolve_identities(adal, [item])


async def test_batch_bounds_and_unknown_tenant_fail_loud(adal: AsyncDAL) -> None:
    with pytest.raises(IdentityValidationError):
        await resolve_identities(adal, [])
    with pytest.raises(IdentityValidationError):
        await resolve_identities(
            adal, [IdentityRequest("acme", "discord", str(i)) for i in range(101)]
        )
    with pytest.raises(TenantNotFoundError):
        await resolve_identities(adal, [IdentityRequest("nope", "discord", "1")])


async def test_db_failure_is_wrapped_not_defaulted(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
    try:
        with pytest.raises(IdentityResolutionError):
            await resolve_identities(adal, [IdentityRequest("acme", "discord", "1")])
    finally:
        await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")


async def test_logs_carry_no_pii(adal: AsyncDAL, caplog: pytest.LogCaptureFixture) -> None:
    await _tenant(adal, "acme")
    caplog.set_level(logging.DEBUG)
    await resolve_identities(
        adal, [IdentityRequest("acme", "discord", "pii-id-123", SECRET_HANDLE)]
    )
    with pytest.raises(TenantNotFoundError):
        await resolve_identities(adal, [IdentityRequest("pii-tenant", "discord", "pii-id-123")])
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert caplog.records
    assert "pii-id-123" not in text and SECRET_HANDLE not in text


@pytest.fixture
async def grpc_addr(adal: AsyncDAL) -> AsyncIterator[str]:
    """In-process grpc.aio server wired with the real IdentityServicer + real DAL."""
    server = grpc.aio.server()
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(IdentityServicer(adal), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


def _req(tenant: str, puid: str) -> identity_pb2.MintEphemeralPseudonymsRequest:
    return identity_pb2.MintEphemeralPseudonymsRequest(
        items=[
            identity_pb2.MintEphemeralPseudonymRequest(
                tenant_id=tenant, platform="discord", platform_user_id=puid, handle="h"
            )
        ]
    )


async def test_grpc_mint_real_path(adal: AsyncDAL, grpc_addr: str) -> None:
    await _tenant(adal, "acme")
    async with grpc.aio.insecure_channel(grpc_addr) as ch:
        stub = identity_pb2_grpc.IdentityServiceStub(ch)
        r1 = await stub.MintEphemeralPseudonyms(_req("acme", "9"), timeout=5)
        r2 = await stub.MintEphemeralPseudonyms(_req("acme", "9"), timeout=5)
        assert r1.pseudonyms[0].platform_user_id == "9"
        assert uuid.UUID(r1.pseudonyms[0].pseudonym) == uuid.UUID(r2.pseudonyms[0].pseudonym)
        with pytest.raises(grpc.aio.AioRpcError) as nf:
            await stub.MintEphemeralPseudonyms(_req("ghost", "9"), timeout=5)
        assert nf.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.aio.AioRpcError) as bad:
            await stub.MintEphemeralPseudonyms(_req("acme", ""), timeout=5)
        assert bad.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as boom:
                await stub.MintEphemeralPseudonyms(_req("acme", "10"), timeout=5)
            assert boom.value.code() == grpc.StatusCode.INTERNAL
        finally:
            await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")


async def test_grpc_mint_without_dal_is_unavailable_not_default() -> None:
    server = grpc.aio.server()
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(IdentityServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            stub = identity_pb2_grpc.IdentityServiceStub(ch)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.MintEphemeralPseudonyms(_req("acme", "1"), timeout=5)
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        await server.stop(grace=None)
