"""Identity security hardening (alembic 0047) against a REAL, fully-migrated Postgres.

Regression coverage for the post-merge adversarial review of #429:

- forged / stale ``community_members.user_uuid`` (trigger derives, never trusts)
- cross-tenant correlation (tenant-filtered ``hub_user_identities`` lookup; tenant taken from
  the VALIDATED token claim, enforced through the real interceptor chain)
- fail-loud dangling ``user_id``, last-writer handle policy, no display-name copy
- visible "unavailable" state for a user linked on two platforms in one community
- GDPR erasure of ``ephemeral_pseudonyms.handle``

No mocked boundary: real triggers/functions on Postgres 17, the real ``AsyncDAL``, and gRPC
servers built from the production interceptor chain. Skipped (never failed) where the
``docker`` CLI is unavailable, like every other real-Postgres test in this repo.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import grpc
import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))
_PB_ROOT = Path(__file__).resolve().parents[1] / "grpc_internal" / "pb"
if str(_PB_ROOT) not in sys.path:  # generated waddles.* stubs, same as grpc_internal/__init__
    sys.path.insert(0, str(_PB_ROOT))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    alembic_cli,
    migrated_postgres,
)
from waddles.hub.internal.v1 import identity_pb2  # noqa: E402

from services import identity_resolution_service as svc  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    HandleNotFoundError,
    IdentityRequest,
    IdentityValidationError,
    collect_uuid_unavailability,
    erase_pseudonym_handles,
    resolve_display_names,
    resolve_identities,
    resolve_identity,
    resolve_target,
    run_uuid_availability_monitor,
)
from tests._identity_grpc import AuthedIdentityStub, make_issuer, serving  # noqa: E402

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

SECRET_HANDLE = "SensitiveHandle_do_not_log"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0047)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-identity-hardening") as db:
        yield db


@pytest.fixture
async def adal(pg_db: PgTestDatabase) -> AsyncIterator[AsyncDAL]:
    """A real flask_core AsyncDAL on the migrated container with clean identity data."""
    dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    await dal.executesql_async(
        "TRUNCATE community_members, ephemeral_pseudonyms, identity_resolution_events, "
        "hub_user_identities, hub_users, communities, tenants RESTART IDENTITY CASCADE"
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


async def _hub_user(adal: AsyncDAL, *, display_name: str | None = None) -> tuple[int, str]:
    row = (
        await adal.executesql_async(
            "INSERT INTO hub_users (username, display_name) VALUES ('u', %s) "
            "RETURNING id, uuid::text",
            [display_name],
        )
    )[0]
    return int(row[0]), str(row[1])


async def _link(adal: AsyncDAL, hub_id: int, platform: str, puid: str) -> None:
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, %s, %s)",
        [hub_id, platform, puid],
    )


async def _member_row(
    adal: AsyncDAL,
    community_id: int,
    *,
    user_id: str | None = None,
    platform: str | None = None,
    puid: str | None = None,
    display: str | None = None,
    user_uuid: str | None = None,
) -> tuple[int, str | None]:
    """Insert a member; returns `(id, derived user_uuid)`. `user_uuid` models a forging caller."""
    row = (
        await adal.executesql_async(
            "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
            "display_name, user_uuid) VALUES (%s, %s, %s, %s, %s, %s::uuid) "
            "RETURNING id, user_uuid::text",
            [community_id, user_id, platform, puid, display, user_uuid],
        )
    )[0]
    return int(row[0]), (None if row[1] is None else str(row[1]))


async def _member_state(adal: AsyncDAL, member_id: int) -> tuple[str | None, str | None]:
    row = (
        await adal.executesql_async(
            "SELECT user_uuid::text, user_uuid_unavailable_reason FROM community_members "
            "WHERE id = %s",
            [member_id],
        )
    )[0]
    return (None if row[0] is None else str(row[0]), row[1])


async def _events(adal: AsyncDAL, event: str) -> list[tuple[Any, ...]]:
    rows = await adal.executesql_async(
        "SELECT tenant_id, community_id, member_id, affected FROM identity_resolution_events "
        "WHERE event = %s ORDER BY id",
        [event],
    )
    return [tuple(r) for r in rows or []]


async def _handle_of(adal: AsyncDAL, tenant_pk: int, platform: str, puid: str) -> str | None:
    return await _one(
        adal,
        "SELECT handle FROM ephemeral_pseudonyms WHERE tenant_id = %s AND platform = %s "
        "AND platform_user_id = %s",
        [tenant_pk, platform, puid],
    )


# ----------------------------------------------------------------- 1. forged / stale user_uuid


@pg_only
async def test_forged_user_uuid_on_insert_is_ignored(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, victim_uuid = await _hub_user(adal)  # someone else's real identity
    forged = str(uuid.uuid4())
    for planted in (forged, victim_uuid):
        puid = f"p-{planted[:8]}"
        member_id, derived = await _member_row(
            adal, c, platform="discord", puid=puid, user_uuid=planted
        )
        assert derived is not None and derived != planted  # never the caller's value
        # ...it is exactly what the service derives for this platform account
        assert uuid.UUID(derived) == await resolve_identity(adal, "acme", "discord", puid)
        assert await _member_state(adal, member_id) == (derived, None)
    assert len(await _events(adal, "caller_user_uuid_ignored")) == 2  # attempts are audited


@pg_only
async def test_forged_user_uuid_on_update_is_ignored(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, victim_uuid = await _hub_user(adal)
    member_id, original = await _member_row(adal, c, platform="discord", puid="d-1")
    await adal.executesql_async(
        "UPDATE community_members SET user_uuid = %s::uuid WHERE id = %s", [victim_uuid, member_id]
    )
    assert (await _member_state(adal, member_id))[0] == original  # re-derived, not the victim's
    await adal.executesql_async(
        "UPDATE community_members SET user_uuid = NULL WHERE id = %s", [member_id]
    )
    assert (await _member_state(adal, member_id))[0] == original  # NULL-ing re-derives too


@pg_only
async def test_relink_user_id_null_to_hub_user_rederives(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    member_id, pseudonym = await _member_row(adal, c, platform="discord", puid="d-1")
    assert pseudonym is not None and pseudonym != hub_uuid
    await adal.executesql_async(
        "UPDATE community_members SET user_id = %s WHERE id = %s", [str(hub_id), member_id]
    )
    assert await _member_state(adal, member_id) == (hub_uuid, None)  # no stale pseudonym kept


@pg_only
async def test_reassign_user_id_a_to_b_does_not_keep_a_uuid(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    a_id, a_uuid = await _hub_user(adal)
    b_id, b_uuid = await _hub_user(adal)
    member_id, first = await _member_row(adal, c, user_id=str(a_id))
    assert first == a_uuid
    await adal.executesql_async(
        "UPDATE community_members SET user_id = %s WHERE id = %s", [str(b_id), member_id]
    )
    assert await _member_state(adal, member_id) == (b_uuid, None)


@pg_only
async def test_unlink_and_platform_id_change_rederive(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    a_id, a_uuid = await _hub_user(adal)
    member_id, first = await _member_row(adal, c, user_id=str(a_id), platform="twitch", puid="x")
    assert first == a_uuid
    await adal.executesql_async(
        "UPDATE community_members SET user_id = NULL WHERE id = %s", [member_id]
    )
    unlinked, _ = await _member_state(adal, member_id)
    assert unlinked is not None and unlinked != a_uuid  # now the account's own pseudonym
    assert uuid.UUID(unlinked) == await resolve_identity(adal, "acme", "twitch", "x")
    await adal.executesql_async(
        "UPDATE community_members SET platform_user_id = 'y' WHERE id = %s", [member_id]
    )
    moved, _ = await _member_state(adal, member_id)
    assert moved is not None and moved != unlinked  # a different account is a different identity
    assert uuid.UUID(moved) == await resolve_identity(adal, "acme", "twitch", "y")


@pg_only
async def test_rederive_replaces_stale_and_forged_rows_from_the_0045_window(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, victim_uuid = await _hub_user(adal)
    hub_id, hub_uuid = await _hub_user(adal)
    await adal.executesql_async(
        "ALTER TABLE community_members DISABLE TRIGGER trg_community_members_user_uuid"
    )
    forged_id, _ = await _member_row(adal, c, platform="discord", puid="f-1", user_uuid=victim_uuid)
    stale_id, _ = await _member_row(
        adal,
        c,
        user_id=str(hub_id),
        user_uuid=str(uuid.uuid4()),  # relinked, old pseudonym kept
    )
    await adal.executesql_async(
        "ALTER TABLE community_members ENABLE TRIGGER trg_community_members_user_uuid"
    )
    spec = importlib.util.spec_from_file_location("m0047", _VERSIONS / "0047_identity_hardening.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    await adal.executesql_async(mod._REDERIVE_SQL)
    forged_after, _ = await _member_state(adal, forged_id)
    assert forged_after is not None and forged_after != victim_uuid
    assert (await _member_state(adal, stale_id))[0] == hub_uuid


# ------------------------------------------------------ 2. tenant filter on the hub-link lookup


@pg_only
async def test_hub_link_resolves_only_inside_a_tenant_the_user_belongs_to(
    adal: AsyncDAL,
) -> None:
    ta, tb = await _tenant(adal, "acme"), await _tenant(adal, "globex")
    ca = await _community(adal, ta)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "111")
    await _member_row(adal, ca, user_id=str(hub_id))  # member of acme only
    assert await resolve_identity(adal, "acme", "discord", "111") == uuid.UUID(hub_uuid)
    outsider = await resolve_identity(adal, "globex", "discord", "111")
    assert outsider != uuid.UUID(hub_uuid)  # cannot correlate the person across tenants
    assert await resolve_identity(adal, "globex", "discord", "111") == outsider  # stable
    # the mention path is tenant-gated too: a non-member of globex is refused outright (no
    # hub uuid, no pseudonym handed out, and nothing minted by the lookup itself)
    minted_before = await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms")
    with pytest.raises(svc.TargetNotMemberError):
        await resolve_target(adal, "globex", "discord", "<@111>")
    assert await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms") == minted_before
    # the SQL function itself (not just the service) refuses the explicit-id shortcut too
    with pytest.raises(Exception, match="not a member of the tenant"):
        await adal.executesql_async(
            "SELECT resolve_identity_uuid(%s, 'discord', 'zzz', %s, NULL)", [tb, str(hub_id)]
        )


@pg_only
async def test_membership_row_vouches_for_its_own_hub_link(adal: AsyncDAL) -> None:
    """A member row in tenant B for a linked account IS membership evidence there."""
    ta, tb = await _tenant(adal, "acme"), await _tenant(adal, "globex")
    ca, cb = await _community(adal, ta), await _community(adal, tb)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-1")
    await _member_row(adal, ca, platform="discord", puid="d-1")
    _, in_b = await _member_row(adal, cb, platform="discord", puid="d-1")
    assert in_b == hub_uuid
    assert await resolve_identity(adal, "globex", "discord", "d-1") == uuid.UUID(hub_uuid)


# ------------------------------------------ 3. dangling user_id, handle policy, display-name copy


@pg_only
async def test_explicit_user_id_that_misses_fails_loud_and_never_falls_to_a_pseudonym(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    with pytest.raises(Exception, match="does not resolve to a hub user"):
        await adal.executesql_async(
            "SELECT resolve_identity_uuid(%s, 'discord', 'd-1', '999999', NULL)", [t]
        )
    member_id, derived = await _member_row(
        adal, c, user_id="999999", platform="discord", puid="d-1"
    )
    assert derived is None  # unavailable, NOT the pseudonym the platform id would have minted
    assert await _member_state(adal, member_id) == (None, "dangling_user_id")
    assert await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms") == 0
    assert len(await _events(adal, "dangling_user_id")) == 1


@pg_only
async def test_handle_policy_last_writer_wins_and_membership_never_writes_it(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    # the membership trigger mints the pseudonym but copies NO display name into the PII table
    _, minted = await _member_row(adal, c, platform="twitch", puid="tw-1", display=SECRET_HANDLE)
    assert minted is not None
    assert await _handle_of(adal, t, "twitch", "tw-1") is None
    # platform-asserted handles: the latest non-empty one wins (first-writer-wins is gone)
    for handle, expected in (("Evil", "Evil"), ("Real", "Real"), ("", "Real"), ("  ", "Real")):
        await resolve_identities(adal, [IdentityRequest("acme", "twitch", "tw-1", handle)])
        assert await _handle_of(adal, t, "twitch", "tw-1") == expected
    same = await resolve_identity(adal, "acme", "twitch", "tw-1")
    assert str(same) == minted  # the UUID never changes with the handle


# ------------------------------------------- 4. unavailable is visible, not a silent NULL


@pg_only
async def test_second_platform_account_in_one_community_is_visibly_unavailable(
    adal: AsyncDAL, caplog: pytest.LogCaptureFixture
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal, display_name=SECRET_HANDLE)
    await _link(adal, hub_id, "discord", "d-1")
    await _link(adal, hub_id, "twitch", "t-1")
    first_id, first = await _member_row(adal, c, platform="discord", puid="d-1")
    second_id, second = await _member_row(adal, c, platform="twitch", puid="t-1")
    assert first == hub_uuid and second is None
    assert await _member_state(adal, second_id) == (None, "uuid_collision")
    # durable audit event: ids only
    events = await _events(adal, "uuid_collision")
    assert events == [(t, c, second_id, None)]
    # consumers can tell "unavailable" from "not a member" through the sanctioned view
    rows = await adal.executesql_async(
        "SELECT platform_user_id, user_uuid_status, user_uuid_unavailable_reason "
        "FROM community_member_identities ORDER BY platform_user_id"
    )
    assert [tuple(r) for r in rows] == [
        ("d-1", "resolved", None),
        ("t-1", "unavailable", "uuid_collision"),
    ]
    # ...and the operator metric/log sees it
    caplog.set_level(logging.DEBUG)
    assert await collect_uuid_unavailability(adal) == {"uuid_collision": 1}
    observed = {
        o.attributes["reason"]: o.value  # type: ignore[index]
        for o in svc._observe_unavailable(None)  # type: ignore[arg-type]
    }
    assert observed == {
        "uuid_collision": 1,
        "dangling_user_id": 0,
        "unresolvable": 0,
        "no_tenant": 0,
    }
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "identity_uuid_unavailable" in text and SECRET_HANDLE not in text
    # freeing the first row lets the second re-derive; the reason is cleared (CHECK-consistent)
    await adal.executesql_async("DELETE FROM community_members WHERE id = %s", [first_id])
    await adal.executesql_async(
        "UPDATE community_members SET platform_user_id = platform_user_id WHERE id = %s",
        [second_id],
    )
    assert await _member_state(adal, second_id) == (hub_uuid, None)
    assert await collect_uuid_unavailability(adal) == {}
    assert {o.value for o in svc._observe_unavailable(None)} == {0}  # type: ignore[arg-type]


@pg_only
async def test_unresolvable_row_records_its_reason(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    member_id, derived = await _member_row(adal, c)
    assert derived is None
    assert await _member_state(adal, member_id) == (None, "unresolvable")


async def test_availability_monitor_survives_a_failing_pass_and_cancels_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FlakyDal:
        """First pass raises, later passes return a row -- the loop must keep going."""

        def __init__(self) -> None:
            self.calls = 0

        async def executesql_async(self, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("db hiccup")
            return [("uuid_collision", 3)]

    dal = FlakyDal()
    caplog.set_level(logging.DEBUG)
    task = asyncio.create_task(run_uuid_availability_monitor(dal, interval_seconds=0.01))
    for _ in range(200):
        if dal.calls >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dal.calls >= 3  # kept collecting after the failure
    assert any(
        r.levelno == logging.ERROR and r.exc_info and "db hiccup" in str(r.exc_info[1])
        for r in caplog.records
    )  # failure logged with its traceback, not swallowed


async def test_availability_monitor_waits_for_schema_readiness() -> None:
    class CountingDal:
        """Records every query so a not-ready pass is provably a no-op."""

        def __init__(self) -> None:
            self.calls = 0

        async def executesql_async(self, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
            self.calls += 1
            return []

    dal = CountingDal()
    ready = {"value": False}
    task = asyncio.create_task(
        run_uuid_availability_monitor(dal, interval_seconds=0.01, is_ready=lambda: ready["value"])
    )
    await asyncio.sleep(0.1)
    assert dal.calls == 0  # schema not at head yet: no query, no spurious failure
    ready["value"] = True
    for _ in range(200):
        if dal.calls:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert dal.calls >= 1


# ------------------------------------------------------------------- GDPR erasure of handles


@pg_only
async def test_erasure_removes_the_handle_but_keeps_the_pseudonym(adal: AsyncDAL) -> None:
    ta, tb = await _tenant(adal, "acme"), await _tenant(adal, "globex")
    ra = await resolve_identities(adal, [IdentityRequest("acme", "twitch", "tw-1", SECRET_HANDLE)])
    rb = await resolve_identities(
        adal, [IdentityRequest("globex", "twitch", "tw-1", SECRET_HANDLE)]
    )
    pseudonym = str(ra[0].uuid)
    # a handle lookup needs current membership: tw-1 is a member of acme (not of globex)
    await _member_row(adal, await _community(adal, ta), platform="twitch", puid="tw-1")
    assert (await resolve_target(adal, "acme", "twitch", f"@{SECRET_HANDLE}")).uuid == ra[0].uuid
    # scoped to one tenant: the other tenant's row is untouched
    assert await erase_pseudonym_handles(adal, "twitch", "tw-1", tenant_id="acme") == 1
    assert await _handle_of(adal, ta, "twitch", "tw-1") is None
    assert await _handle_of(adal, tb, "twitch", "tw-1") == SECRET_HANDLE
    # the pseudonym UUID is stable, but nothing can be read back through the handle paths
    assert await resolve_identity(adal, "acme", "twitch", "tw-1") == ra[0].uuid
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", f"@{SECRET_HANDLE}")
    names = await resolve_display_names(adal, "acme", [pseudonym])
    assert names.names == () and names.unresolved == (pseudonym,)
    # across all tenants (DSAR for the person): the remaining handle goes too
    assert await erase_pseudonym_handles(adal, "twitch", "tw-1") == 1
    assert await _handle_of(adal, tb, "twitch", "tw-1") is None
    assert rb[0].uuid != ra[0].uuid
    assert await erase_pseudonym_handles(adal, "twitch", "tw-1") == 0  # idempotent
    audit = await _events(adal, "pseudonym_handle_erased")
    assert [e[3] for e in audit] == [1, 1, 0] and audit[0][0] == ta
    dump = str(await adal.executesql_async("SELECT * FROM identity_resolution_events"))
    assert SECRET_HANDLE not in dump


@pg_only
async def test_erasure_delete_mapping_severs_the_account_from_its_uuid(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    first = (
        await resolve_identities(adal, [IdentityRequest("acme", "twitch", "tw-1", SECRET_HANDLE)])
    )[0].uuid
    assert await erase_pseudonym_handles(adal, "twitch", "tw-1", delete_mapping=True) == 1
    assert await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms") == 0
    assert len(await _events(adal, "pseudonym_erased")) == 1
    assert await resolve_identity(adal, "acme", "twitch", "tw-1") != first  # fresh pseudonym


@pg_only
@pytest.mark.parametrize(
    ("platform", "puid"),
    [("Bad Platform", "1"), ("twitch", ""), ("twitch", "a\x00b"), ("twitch", "x" * 256)],
)
async def test_erasure_validates_input_fail_loud(adal: AsyncDAL, platform: str, puid: str) -> None:
    with pytest.raises(IdentityValidationError):
        await erase_pseudonym_handles(adal, platform, puid)


@pg_only
async def test_erasure_unknown_tenant_fails_loud(adal: AsyncDAL) -> None:
    with pytest.raises(svc.TenantNotFoundError):
        await erase_pseudonym_handles(adal, "twitch", "1", tenant_id="ghost")


# ---------------------------------- tenant from the VALIDATED token claim (real interceptors)


def _mint_req(*items: tuple[str, str]) -> identity_pb2.MintEphemeralPseudonymsRequest:
    return identity_pb2.MintEphemeralPseudonymsRequest(
        items=[
            identity_pb2.MintEphemeralPseudonymRequest(
                tenant_id=tenant, platform="discord", platform_user_id=puid, handle="h"
            )
            for tenant, puid in items
        ]
    )


@pg_only
async def test_token_without_tenant_claim_is_rejected_on_every_identity_rpc(
    adal: AsyncDAL,
) -> None:
    await _tenant(adal, "acme")
    tenantless = make_issuer(tenant="")
    async with serving(adal, tenantless) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, tenantless)
        calls = (
            stub.MintEphemeralPseudonyms(_mint_req(("acme", "1"))),
            stub.ResolveDisplayNames(
                identity_pb2.ResolveDisplayNamesRequest(tenant_id="acme", uuids=[str(uuid.uuid4())])
            ),
            stub.ResolveHandle(
                identity_pb2.ResolveHandleRequest(
                    tenant_id="acme", platform="discord", target="<@1>"
                )
            ),
        )
        for call in calls:
            with pytest.raises(grpc.aio.AioRpcError) as denied:
                await call
            assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
            assert denied.value.details() == "tenant claim required"
    assert await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms") == 0


@pg_only
@pytest.mark.parametrize("claim_by", ["slug", "id"])
async def test_tenant_bound_token_is_confined_to_its_tenant(
    adal: AsyncDAL, claim_by: str, caplog: pytest.LogCaptureFixture
) -> None:
    ta = await _tenant(adal, "acme")
    await _tenant(adal, "globex")
    # the mention lookup below needs a CURRENT member (it never mints for strangers)
    await _member_row(adal, await _community(adal, ta), platform="discord", puid="1")
    issuer = make_issuer(tenant="acme" if claim_by == "slug" else str(ta))
    caplog.set_level(logging.DEBUG)
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, issuer)
        own = await stub.MintEphemeralPseudonyms(_mint_req(("acme", "1")))
        assert len(own.pseudonyms) == 1
        by_id = await stub.MintEphemeralPseudonyms(_mint_req((str(ta), "1")))
        assert by_id.pseudonyms[0].pseudonym == own.pseudonyms[0].pseudonym  # id == slug
        defaulted = await stub.MintEphemeralPseudonyms(_mint_req(("", "1")))
        assert defaulted.pseudonyms[0].pseudonym == own.pseudonyms[0].pseudonym  # from the claim
        before = await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms")
        # another tenant -- real, unknown, or hidden in a mixed batch -- is denied identically
        details = set()
        for req in (
            _mint_req(("globex", "1")),
            _mint_req(("ghost-tenant", "1")),
            _mint_req(("acme", "2"), ("globex", "2")),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as denied:
                await stub.MintEphemeralPseudonyms(req)
            assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
            details.add(denied.value.details())
        assert details == {"tenant access denied"}  # no tenant-existence oracle
        assert await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms") == before  # no partial
        with pytest.raises(grpc.aio.AioRpcError) as names:
            await stub.ResolveDisplayNames(
                identity_pb2.ResolveDisplayNamesRequest(
                    tenant_id="globex", uuids=[own.pseudonyms[0].pseudonym]
                )
            )
        assert names.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.aio.AioRpcError) as handle:
            await stub.ResolveHandle(
                identity_pb2.ResolveHandleRequest(
                    tenant_id="globex", platform="discord", target="<@1>"
                )
            )
        assert handle.value.code() == grpc.StatusCode.PERMISSION_DENIED
        # ...while its own tenant works on every RPC
        ok = await stub.ResolveHandle(
            identity_pb2.ResolveHandleRequest(tenant_id="acme", platform="discord", target="<@1>")
        )
        assert ok.uuid == own.pseudonyms[0].pseudonym
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "globex" not in text and "ghost-tenant" not in text  # caller input never logged
    assert "identity_tenant_authz" in text


@pg_only
async def test_system_token_may_act_for_any_tenant_but_must_name_one(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    await _tenant(adal, "globex")
    system = make_issuer(tenant="system")
    async with serving(adal, system) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch, system)
        a = await stub.MintEphemeralPseudonyms(_mint_req(("acme", "1")))
        g = await stub.MintEphemeralPseudonyms(_mint_req(("globex", "1")))
        assert a.pseudonyms[0].pseudonym != g.pseudonyms[0].pseudonym  # per-tenant identities
        with pytest.raises(grpc.aio.AioRpcError) as unnamed:
            await stub.MintEphemeralPseudonyms(_mint_req(("", "1")))
        assert unnamed.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as ghost:
            await stub.MintEphemeralPseudonyms(_mint_req(("ghost-tenant", "1")))
        assert ghost.value.code() == grpc.StatusCode.NOT_FOUND


@pg_only
async def test_cross_tenant_mint_cannot_correlate_a_hub_user_through_the_rpc(
    adal: AsyncDAL,
) -> None:
    ta, _ = await _tenant(adal, "acme"), await _tenant(adal, "globex")
    ca = await _community(adal, ta)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-1")
    await _member_row(adal, ca, user_id=str(hub_id))
    acme, globex = make_issuer(tenant="acme"), make_issuer(tenant="globex")
    async with serving(adal, acme) as addr_a, serving(adal, globex) as addr_g:
        async with grpc.aio.insecure_channel(addr_a) as cha:
            mine = await AuthedIdentityStub(cha, acme).MintEphemeralPseudonyms(
                _mint_req(("acme", "d-1"))
            )
        assert mine.pseudonyms[0].pseudonym == hub_uuid  # a member tenant sees the hub uuid
        async with grpc.aio.insecure_channel(addr_g) as chg:
            theirs = await AuthedIdentityStub(chg, globex).MintEphemeralPseudonyms(
                _mint_req(("globex", "d-1"))
            )
        assert theirs.pseudonyms[0].pseudonym != hub_uuid  # ...an outsider tenant never does


# ----------------------------------------------------------- migration up/down round trip


@pg_only
def test_0047_downgrade_restores_0045_shape_and_upgrade_reapplies() -> None:
    with migrated_postgres("hub-api-identity-hardening-rt") as db:
        alembic_cli("downgrade", "0046_bundle_reputation_store", dsn=db.dsn)

        def scalar(sql: str) -> Any:
            with closing(psycopg2.connect(db.dsn)) as conn, conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return None if row is None else row[0]

        arity = (
            "SELECT array_agg(pronargs ORDER BY pronargs) FROM pg_proc "
            "WHERE proname = 'resolve_identity_uuid'"
        )
        assert scalar(arity) == [5]  # the 0045 resolver is back, the 6-arg one is gone
        assert scalar("SELECT to_regclass('identity_resolution_events')") is None
        erase_fn = "erase_ephemeral_pseudonym_handles(text,text,integer,boolean)"
        assert scalar(f"SELECT to_regprocedure('{erase_fn}')") is None
        assert (
            scalar(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'community_members' "
                "AND column_name = 'user_uuid_unavailable_reason'"
            )
            == 0
        )
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert scalar(arity) == [6]
        assert scalar("SELECT to_regclass('identity_resolution_events')") is not None
