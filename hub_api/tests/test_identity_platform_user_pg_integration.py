"""`IdentityService.ResolvePlatformUser`: UUID -> RAW platform user id, per community (real PG).

The one detokenization point the PII boundary allows. Everything runs on a fully migrated
Postgres 17 container with the real `AsyncDAL` behind the production interceptor chain (deadline
-> auth -> rate limit -> OTel -> servicer), so the scope, tenant-claim, tenant-confinement,
membership and no-oracle properties are proven end to end rather than asserted on mocks. Skipped,
never failed, where the `docker` CLI is unavailable (same posture as every real-Postgres test).
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
from flask_core.service_jwt import SYSTEM_TENANT, BootstrapRejected

import grpc_internal  # noqa: F401  # puts grpc_internal/pb on sys.path (waddles.* stubs)

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)
from waddles.hub.internal.v1 import identity_pb2, identity_pb2_grpc  # noqa: E402

from grpc_internal.servicers import REQUIRED_SCOPES  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    MAX_PLATFORM_USER_BATCH,
    IdentityValidationError,
    PlatformUserNotFoundError,
    resolve_platform_users,
)
from tests._identity_grpc import AuthedIdentityStub, make_issuer, serving  # noqa: E402

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_METHOD = "/waddles.hub.internal.v1.IdentityService/ResolvePlatformUser"
_SCOPE = "identity:platform-user:resolve"
_INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
#: Raw platform ids the logs and error text must never carry.
SNOWFLAKE = "812345678901234567"
OTHER_SNOWFLAKE = "912345678901234567"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (own, uniquely named container)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hubapi-platformuser-a5d2") as db:
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


async def _member(
    adal: AsyncDAL,
    community_id: int,
    *,
    platform: str | None = "discord",
    puid: str | None = SNOWFLAKE,
    user_id: str | None = None,
) -> str:
    value = await _one(
        adal,
        "INSERT INTO community_members (community_id, user_id, platform, platform_user_id) "
        "VALUES (%s, %s, %s, %s) RETURNING user_uuid::text",
        [community_id, user_id, platform, puid],
    )
    assert value is not None
    return str(value)


async def _hub_user(adal: AsyncDAL, *, active: bool = True) -> tuple[int, str]:
    row = (
        await adal.executesql_async(
            "INSERT INTO hub_users (username, is_active) VALUES ('u', %s) RETURNING id, uuid::text",
            [active],
        )
    )[0]
    return int(row[0]), str(row[1])


async def _link(adal: AsyncDAL, hub_id: int, platform: str, puid: str) -> None:
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, %s, %s)",
        [hub_id, platform, puid],
    )


def _req(
    tenant: str, community: int, uuids: list[str], platform: str = "discord"
) -> identity_pb2.ResolvePlatformUserRequest:
    return identity_pb2.ResolvePlatformUserRequest(
        tenant_id=tenant, community_id=community, platform=platform, uuids=uuids
    )


# --------------------------------------------------------------------------- service layer


@pg_only
async def test_pseudonymous_member_resolves_to_raw_id(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    got = await resolve_platform_users(adal, "acme", c, "discord", [u])
    assert [(r.uuid, r.platform_user_id) for r in got] == [(u, SNOWFLAKE)]


@pg_only
async def test_tenant_addressable_by_slug_or_numeric_id(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    for ref in ("acme", str(t)):
        got = await resolve_platform_users(adal, ref, c, "discord", [u])
        assert got[0].platform_user_id == SNOWFLAKE


@pg_only
async def test_linked_hub_user_resolves_via_platform_membership(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", SNOWFLAKE)
    member_uuid = await _member(adal, c)
    assert member_uuid == hub_uuid  # the trigger derives the hub uuid for a linked account
    got = await resolve_platform_users(adal, "acme", c, "discord", [hub_uuid])
    assert got[0].platform_user_id == SNOWFLAKE


@pg_only
async def test_hub_member_without_platform_row_resolves_via_identity_link(adal: AsyncDAL) -> None:
    """Joined through the hub (no platform columns): the verified link supplies the id."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", SNOWFLAKE)
    await _member(adal, c, platform=None, puid=None, user_id=str(hub_id))
    got = await resolve_platform_users(adal, "acme", c, "discord", [hub_uuid])
    assert got[0].platform_user_id == SNOWFLAKE


@pg_only
async def test_wrong_community_is_generic_not_found(adal: AsyncDAL) -> None:
    # regression: platform-user-resolve cross-community
    t = await _tenant(adal, "acme")
    c1 = await _community(adal, t)
    c2 = await _community(adal, t)
    u = await _member(adal, c1)
    with pytest.raises(PlatformUserNotFoundError):
        await resolve_platform_users(adal, "acme", c2, "discord", [u])


@pg_only
async def test_wrong_tenant_community_is_generic_not_found(adal: AsyncDAL) -> None:
    t1 = await _tenant(adal, "acme")
    t2 = await _tenant(adal, "other")
    c1 = await _community(adal, t1)
    c2 = await _community(adal, t2)
    u = await _member(adal, c1)
    with pytest.raises(PlatformUserNotFoundError):
        await resolve_platform_users(adal, "other", c2, "discord", [u])
    with pytest.raises(PlatformUserNotFoundError):  # community of another tenant
        await resolve_platform_users(adal, "other", c1, "discord", [u])


@pg_only
async def test_every_miss_shape_is_the_same_error(adal: AsyncDAL) -> None:
    """Unknown uuid, other platform, left member, unknown community/tenant: one constant error."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    left = await _member(adal, c, puid=OTHER_SNOWFLAKE)
    await adal.executesql_async(
        "UPDATE community_members SET left_at = now() WHERE platform_user_id = %s",
        [OTHER_SNOWFLAKE],
    )
    cases = [
        ("acme", c, "discord", [str(uuid.uuid4())]),
        ("acme", c, "twitch", [u]),
        ("acme", c, "discord", [left]),
        ("acme", c + 999, "discord", [u]),
        ("nope", c, "discord", [u]),
    ]
    messages = set()
    for tenant, community, platform, uuids in cases:
        with pytest.raises(PlatformUserNotFoundError) as exc:
            await resolve_platform_users(adal, tenant, community, platform, uuids)
        messages.add(str(exc.value))
    assert messages == {"platform user not found"}


@pg_only
async def test_one_bad_uuid_fails_the_whole_batch_no_partial_answer(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    good = await _member(adal, c)
    with pytest.raises(PlatformUserNotFoundError):
        await resolve_platform_users(adal, "acme", c, "discord", [good, str(uuid.uuid4())])


@pg_only
async def test_deactivated_hub_user_never_resolves(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal, active=False)
    await _link(adal, hub_id, "discord", SNOWFLAKE)
    await _member(adal, c)
    with pytest.raises(PlatformUserNotFoundError):
        await resolve_platform_users(adal, "acme", c, "discord", [hub_uuid])


@pg_only
async def test_ambiguous_identity_is_refused_not_guessed(adal: AsyncDAL) -> None:
    """A hub user linked to two discord accounts, both members: never pick one."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", SNOWFLAKE)
    await _link(adal, hub_id, "discord", OTHER_SNOWFLAKE)
    await _member(adal, c, platform=None, puid=None, user_id=str(hub_id))
    with pytest.raises(PlatformUserNotFoundError):
        await resolve_platform_users(adal, "acme", c, "discord", [hub_uuid])


@pg_only
async def test_duplicates_collapse_and_order_is_preserved(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    a = await _member(adal, c)
    b = await _member(adal, c, puid=OTHER_SNOWFLAKE)
    got = await resolve_platform_users(adal, "acme", c, "discord", [b, a, b.upper()])
    assert [r.uuid for r in got] == [b, a]


@pytest.mark.parametrize(
    ("community", "platform", "uuids"),
    [
        (0, "discord", [str(uuid.uuid4())]),
        (-1, "discord", [str(uuid.uuid4())]),
        (True, "discord", [str(uuid.uuid4())]),
        (1, "", [str(uuid.uuid4())]),
        (1, "Bad Platform", [str(uuid.uuid4())]),
        (1, "discord", []),
        (1, "discord", ["not-a-uuid"]),
        (1, "discord", ["{" + str(uuid.uuid4()) + "}"]),
        (1, "discord", ["urn:uuid:" + str(uuid.uuid4())]),
        (1, "discord", [SNOWFLAKE]),  # a raw platform id is never a valid reference
        (1, "discord", [str(uuid.uuid4()) for _ in range(MAX_PLATFORM_USER_BATCH + 1)]),
    ],
)
async def test_invalid_input_rejected_before_the_db(
    community: Any, platform: str, uuids: list[str]
) -> None:
    with pytest.raises(IdentityValidationError):
        await resolve_platform_users(object(), "acme", community, platform, uuids)


async def test_blank_tenant_rejected_before_the_db() -> None:
    with pytest.raises(IdentityValidationError):
        await resolve_platform_users(object(), "", 1, "discord", [str(uuid.uuid4())])


# --------------------------------------------------------------------------- gRPC end to end


def test_method_has_its_own_registered_scope() -> None:
    assert REQUIRED_SCOPES[_METHOD] == _SCOPE
    assert _SCOPE not in {s for m, s in REQUIRED_SCOPES.items() if m != _METHOD}


def _issuer(tenant: str = SYSTEM_TENANT, scopes: frozenset[str] = frozenset({_SCOPE})) -> Any:
    return make_issuer(tenant, service_id=_INGEST, scopes=scopes)


@pg_only
async def test_grpc_resolves_with_exact_response_dto_field_set(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    issuer = _issuer()
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        resp = await stub.ResolvePlatformUser(_req("acme", c, [u]))
    assert [f.name for f in resp.DESCRIPTOR.fields] == ["users"]
    assert [f.name for f in resp.users[0].DESCRIPTOR.fields] == ["uuid", "platform_user_id"]
    assert (resp.users[0].uuid, resp.users[0].platform_user_id) == (u, SNOWFLAKE)


@pg_only
async def test_grpc_missing_scope_is_unauthenticated(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    # The identity may hold every identity scope; the TOKEN carries only a different one.
    issuer = _issuer(scopes=frozenset({_SCOPE, "identity:handle:resolve"}))
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = identity_pb2_grpc.IdentityServiceStub(ch)
        wrong = issuer.issue(_INGEST, "identity:handle:resolve")
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.ResolvePlatformUser(
                _req("acme", c, [u]), metadata=(("authorization", f"Bearer {wrong}"),), timeout=2
            )
        assert denied.value.code() == grpc.StatusCode.UNAUTHENTICATED
        with pytest.raises(grpc.aio.AioRpcError) as anon:
            await stub.ResolvePlatformUser(_req("acme", c, [u]), timeout=2)
        assert anon.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_grpc_identity_without_the_scope_cannot_even_mint_a_token(adal: AsyncDAL) -> None:
    """The scope is an allow-list entry: an identity not granted it cannot obtain the token."""
    issuer = _issuer(scopes=frozenset({"identity:handle:resolve"}))
    with pytest.raises(BootstrapRejected):
        issuer.issue(_INGEST, _SCOPE)


@pg_only
async def test_grpc_token_without_tenant_claim_is_refused(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    issuer = _issuer(tenant="")
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await stub.ResolvePlatformUser(_req("acme", c, [u]))
    assert err.value.code() == grpc.StatusCode.PERMISSION_DENIED


@pg_only
async def test_grpc_tenant_bound_token_is_confined_to_its_tenant(adal: AsyncDAL) -> None:
    t1 = await _tenant(adal, "acme")
    t2 = await _tenant(adal, "other")
    c1 = await _community(adal, t1)
    u = await _member(adal, c1)
    await _community(adal, t2)
    issuer = _issuer(tenant="other")
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        # Naming the victim tenant in the body does not help a token bound to `other`.
        with pytest.raises(grpc.aio.AioRpcError) as named:
            await stub.ResolvePlatformUser(_req("acme", c1, [u]))
        assert named.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # Omitting the tenant defaults to the token's own, where c1 does not exist.
        with pytest.raises(grpc.aio.AioRpcError) as own:
            await stub.ResolvePlatformUser(_req("", c1, [u]))
        assert own.value.code() == grpc.StatusCode.NOT_FOUND


@pg_only
async def test_grpc_wrong_community_is_not_found_with_constant_detail(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c1 = await _community(adal, t)
    c2 = await _community(adal, t)
    u = await _member(adal, c1)
    issuer = _issuer()
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        details = set()
        for community, uuids in ((c2, [u]), (c1, [str(uuid.uuid4())])):
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await stub.ResolvePlatformUser(_req("acme", community, uuids))
            assert err.value.code() == grpc.StatusCode.NOT_FOUND
            details.add(err.value.details())
    assert details == {"platform user not found"}


@pg_only
async def test_grpc_invalid_argument_and_batch_bound(adal: AsyncDAL) -> None:
    issuer = _issuer()
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        too_many = [str(uuid.uuid4()) for _ in range(MAX_PLATFORM_USER_BATCH + 1)]
        for bad in (_req("acme", 1, []), _req("acme", 1, too_many), _req("acme", 1, ["x"])):
            with pytest.raises(grpc.aio.AioRpcError) as err:
                await stub.ResolvePlatformUser(bad)
            assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT


@pg_only
async def test_audit_and_logs_carry_no_raw_platform_id(
    adal: AsyncDAL, caplog: pytest.LogCaptureFixture
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    issuer = _issuer()
    caplog.set_level(logging.DEBUG)
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        await stub.ResolvePlatformUser(_req("acme", c, [u]))
        with pytest.raises(grpc.aio.AioRpcError):
            await stub.ResolvePlatformUser(_req("acme", c, [str(uuid.uuid4())]))
    rendered = "\n".join(r.getMessage() + repr(sorted(r.__dict__.items())) for r in caplog.records)
    assert SNOWFLAKE not in rendered
    audits = [
        r for r in caplog.records if getattr(r, "action", "") == "identity_resolve_platform_user"
    ]
    assert [(a.outcome, a.count, a.community_id) for a in audits] == [  # type: ignore[attr-defined]
        ("ok", 1, c),
        ("not_found", 1, c),
    ]
    assert all(a.caller == _INGEST for a in audits)  # type: ignore[attr-defined]


@pg_only
async def test_per_method_rate_limit_is_tighter_than_the_default(
    adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRPC_PLATFORM_USER_RPS", "0.001")
    monkeypatch.setenv("GRPC_PLATFORM_USER_BURST", "2")
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    u = await _member(adal, c)
    issuer = _issuer()
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer, service_id=_INGEST)
        await stub.ResolvePlatformUser(_req("acme", c, [u]))
        await stub.ResolvePlatformUser(_req("acme", c, [u]))
        with pytest.raises(grpc.aio.AioRpcError) as err:
            await stub.ResolvePlatformUser(_req("acme", c, [u]))
        assert err.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
