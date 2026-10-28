"""Guarded demote-on-remove (#877) end-to-end on REAL Postgres + a mocked Discord REST API.

The sqlite suite (`test_role_sync_service.py::TestGuardedDemote`) swaps the Discord client for an
in-memory fake. Here the production `HttpDiscordRoleTargetClient` (real pagination, real status
classification) talks to an `httpx.MockTransport` standing in for Discord, and every DB read/write
-- Discord id -> `hub_user_identities` -> `community_members.user_id` (a VARCHAR holding the hub
user id), the baseline `community_roles` lookup, the role UPDATE -- runs against a migrated
Postgres container. What this locks down for the demo:

- the 4 guards (member read non-empty, provably complete, baseline exists, owner/unmanaged skip),
- the circuit breaker boundaries (`> 3` AND `> 50%`),
- the baseline-missing abort is LOGGED at WARNING (never a silent no-op),
- 4xx/5xx/transport Discord failures demote nobody and log type-only (no response body, no PII),
- the demote logs carry ids/counts only -- never a Discord user id or hub username.

Skipped (never failed) where the `docker` CLI is unavailable, like every other PG test here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from flask_core.database import AsyncDAL

from services import role_sync_service as svc
from services.credential_resolver import PlatformCredentials
from services.guild_pairing import create_binding, create_pairing, update_pairing
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

GUILD_ID = "123456789012345678"  # gitleaks:allow - fake snowflake
MEMBER_DISCORD_ROLE = "444444444444444499"
ADMIN_DISCORD_ROLE = "333333333333333399"
BASELINE = "community-member"
LOGGER = "services.role_sync_service"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres 17 container for the module, columns topped up to the binders."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-role-sync-demote") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated, bound pydal `AsyncDAL` on the real container."""
    yield from pg.fresh_dal(pg_db)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Flag ON; demote knobs unset unless a test sets them."""
    monkeypatch.delenv(svc.DEMOTE_PAIRING_IDS_ENV, raising=False)
    monkeypatch.delenv(svc.DEMOTE_BASELINE_ROLE_ENV, raising=False)
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


class _Resolver:
    """Returns a fake bot token for any tenant (credential resolution is out of scope here)."""

    async def resolve(
        self, dal: Any, *, tenant_id: int, is_global_tenant: bool, platform: str
    ) -> PlatformCredentials:
        return PlatformCredentials(
            tenant_id=tenant_id,
            platform=platform,
            payload={"bot_token": "fake-bot-token"},  # noqa: S106
            source="tenant",
        )


class _Discord:
    """`httpx.MockTransport` guild-member endpoint with real `after`/`limit` pagination."""

    def __init__(self, members: list[tuple[str, list[str]]]) -> None:
        self.members = members
        self.requests: list[httpx.Request] = []
        self.override: Callable[[httpx.Request], httpx.Response] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.override is not None:
            return self.override(request)
        after = request.url.params.get("after")
        limit = int(request.url.params.get("limit", "1000"))
        rows = [m for m in self.members if after is None or int(m[0]) > int(after)]
        page = [{"user": {"id": uid}, "roles": roles} for uid, roles in rows[:limit]]
        return httpx.Response(200, json=page)

    def client(self, token: str) -> svc.HttpDiscordRoleTargetClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return svc.HttpDiscordRoleTargetClient(http, bot_token=token)


class _World:
    """Seeded tenant/community/roles/pairing plus helpers to add linked members."""

    def __init__(self, dal: Any) -> None:
        self.dal = dal
        self.tenant_id = int(
            pg.scalar(
                dal,
                "INSERT INTO tenants (slug, display_name, is_global, is_active) "
                "VALUES ('acme', 'Acme', false, true) RETURNING id",
            )
        )
        self.community_id = self._community("c1")
        self.pairing_id = 0

    def _community(self, name: str) -> int:
        return int(
            pg.scalar(
                self.dal,
                "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
                [name, self.tenant_id],
            )
        )

    def roles(self, community_id: int | None = None, *names: str) -> None:
        for name in names or (BASELINE, "member", "community-admin", "vip"):
            pg.q(
                self.dal,
                "INSERT INTO community_roles (community_id, name, priority) VALUES (%s, %s, 0)",
                [community_id or self.community_id, name],
            )

    def pairing(self, *, enable_demote: bool, mp: pytest.MonkeyPatch) -> Any:
        created = create_pairing(
            self.dal,
            self.community_id,
            discord_guild_id=GUILD_ID,
            direction="discord_to_twitch",
            role_name_prefix="[BC]",
            actor_user_id=None,
        )
        update_pairing(self.dal, self.community_id, created.id, sync_enabled=True)
        create_binding(
            self.dal,
            self.community_id,
            created.id,
            sync_scope="community_role",
            discord_role_id=MEMBER_DISCORD_ROLE,
            community_role="member",
        )
        create_binding(
            self.dal,
            self.community_id,
            created.id,
            sync_scope="community_role",
            discord_role_id=ADMIN_DISCORD_ROLE,
            community_role="community-admin",
        )
        self.pairing_id = created.id
        if enable_demote:
            mp.setenv(svc.DEMOTE_PAIRING_IDS_ENV, str(created.id))
        return self.dal(self.dal.guild_tenant_pairings.id == created.id).select().first()

    def member(
        self,
        discord_id: str,
        role: str,
        *,
        community_id: int | None = None,
        active: bool = True,
        username: str | None = None,
        link: bool = True,
    ) -> int:
        hub_id = int(
            pg.scalar(
                self.dal,
                "INSERT INTO hub_users (username) VALUES (%s) RETURNING id",
                [username or f"hubname-{discord_id}"],
            )
        )
        if link:
            pg.q(
                self.dal,
                "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
                "VALUES (%s, 'discord', %s)",
                [hub_id, discord_id],
            )
        pg.q(
            self.dal,
            "INSERT INTO community_members (community_id, user_id, role, is_active) "
            "VALUES (%s, %s, %s, %s)",
            [community_id or self.community_id, str(hub_id), role, active],
        )
        return hub_id

    def role_of(self, hub_id: int, community_id: int | None = None) -> str:
        return str(
            pg.scalar(
                self.dal,
                "SELECT role FROM community_members WHERE community_id = %s AND user_id = %s",
                [community_id or self.community_id, str(hub_id)],
            )
        )


@pytest.fixture
def world(adal: AsyncDAL) -> _World:
    return _World(adal.dal)


async def _run(world: _World, pairing: Any, discord: _Discord) -> svc.PlatformSyncResult:
    return await svc.reconcile_pairing_discord_to_platform(
        world.dal,
        pairing,
        credential_resolver=_Resolver(),  # type: ignore[arg-type]
        make_discord_client=discord.client,
    )


def _msgs(caplog: pytest.LogCaptureFixture, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if needle in r.getMessage()]


def _assert_pii_free(caplog: pytest.LogCaptureFixture, *forbidden: str) -> None:
    text = "\n".join(r.getMessage() + str(r.exc_text or "") for r in caplog.records)
    for token in forbidden:
        assert token not in text, f"{token!r} leaked into logs"


# --------------------------------------------------------------------------- happy path


async def test_demote_end_to_end_with_pagination_and_pii_free_logs(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(svc, "_DISCORD_MEMBER_PAGE_SIZE", 2)
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    keep = world.member("800001", "member")  # still holds the mapped role
    gone1 = world.member("800002", "member")  # lost it -> demote
    gone2 = world.member("800003", "community-admin", username="alice_secret")  # lost it -> demote
    world.member("800004", "member", link=False)  # unlinked discord id -> untouched
    discord = _Discord(
        [
            ("800001", [MEMBER_DISCORD_ROLE]),
            ("800002", []),
            ("800003", ["999"]),
            ("800004", []),
            ("800005", []),  # no hub link at all
        ]
    )
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, discord)

    assert result.error is None and result.community_roles_demoted == 2
    assert [world.role_of(u) for u in (keep, gone1, gone2)] == ["member", BASELINE, BASELINE]
    # 5 members at 2/page -> 3 requests, the later ones carry the `after` cursor
    assert len(discord.requests) == 3
    assert discord.requests[0].url.params.get("after") is None
    assert discord.requests[1].url.params["after"] == "800002"
    baseline_id = pg.scalar(
        world.dal,
        "SELECT id FROM community_roles WHERE community_id=%s AND name=%s",
        [world.community_id, BASELINE],
    )
    row = pg.q(
        world.dal,
        "SELECT community_role_id, claims_cache FROM community_members WHERE user_id = %s",
        [str(gone1)],
    )[0]
    assert row[0] == baseline_id and row[1] is None
    done = _msgs(caplog, "role_sync.demote_pass")
    assert len(done) == 1 and "demoted=2" in done[0].getMessage()
    _assert_pii_free(caplog, "800001", "800002", "800003", "alice_secret", "hubname-")


# --------------------------------------------------------------------------- baseline guard


async def test_baseline_missing_aborts_loudly_not_silently(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The demo landmine: community has no `community-member` role -> abort, WARNING emitted."""
    world.roles(None, "member", "community-admin")  # NOTE: no default baseline role
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800002", "member")
    discord = _Discord([("800002", [])])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, discord)

    assert result.error is None and result.community_roles_demoted == 0
    assert world.role_of(victim) == "member"
    hits = _msgs(caplog, "role_sync.demote_aborted_baseline_missing")
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    assert f"pairing_id={pairing.id}" in hits[0].getMessage()
    assert f"baseline={BASELINE}" in hits[0].getMessage()
    assert not _msgs(caplog, "role_sync.demote_pass ")  # never claims a pass happened
    _assert_pii_free(caplog, "800002", "hubname-")


async def test_baseline_env_override_resolves_and_blank_falls_back(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.roles(None, "member", "community-admin", "guest")
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800002", "member")
    discord = _Discord([("800002", [])])

    monkeypatch.setenv(svc.DEMOTE_BASELINE_ROLE_ENV, "  ")  # blank -> default (absent here)
    assert svc._demote_baseline_role() == BASELINE
    assert (await _run(world, pairing, discord)).community_roles_demoted == 0

    monkeypatch.setenv(svc.DEMOTE_BASELINE_ROLE_ENV, "guest")
    assert (await _run(world, pairing, discord)).community_roles_demoted == 1
    assert world.role_of(victim) == "guest"


# --------------------------------------------------------------------------- circuit breaker


@pytest.mark.parametrize(
    ("would_demote", "other_holders", "expected"),
    [
        (3, 0, 3),  # at the absolute floor: allowed even at 100%
        (4, 4, 4),  # 4/8 == 50% is NOT "> 50%": allowed
        (4, 3, 0),  # 4/7 > 50% and > floor: TRIPS
        (5, 0, 0),  # mass demote of everyone: TRIPS
        (5, 10, 5),  # 5/15 well under half: allowed
    ],
)
async def test_circuit_breaker_boundaries(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    would_demote: int,
    other_holders: int,
    expected: int,
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    members: list[tuple[str, list[str]]] = []
    victims = []
    for i in range(would_demote):
        did = f"7000{i:02d}"
        victims.append(world.member(did, "member"))
        members.append((did, []))
    for i in range(other_holders):
        did = f"7100{i:02d}"
        world.member(did, "member")
        members.append((did, [MEMBER_DISCORD_ROLE]))
    members.sort(key=lambda m: int(m[0]))
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, _Discord(members))

    assert result.error is None and result.community_roles_demoted == expected
    tripped = _msgs(caplog, "role_sync.demote_aborted_circuit_breaker")
    if expected == 0:
        assert len(tripped) == 1 and tripped[0].levelno == logging.ERROR
        assert f"would_demote={would_demote}" in tripped[0].getMessage()
        assert {world.role_of(v) for v in victims} == {"member"}
    else:
        assert not tripped
        assert {world.role_of(v) for v in victims} == {BASELINE}


# --------------------------------------------------------------------------- member-read guards


async def test_incomplete_page_cap_demotes_nobody(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(svc, "_DISCORD_MEMBER_PAGE_SIZE", 1)
    monkeypatch.setattr(svc, "_MAX_PAGES", 2)  # list truncated: a 3rd page exists
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member")
    discord = _Discord([("800001", []), ("800002", [MEMBER_DISCORD_ROLE]), ("800003", [])])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, discord)
    assert result.community_roles_demoted == 0 and world.role_of(victim) == "member"
    hit = _msgs(caplog, "role_sync.demote_aborted_unknown_member_read")
    assert len(hit) == 1 and "complete=False" in hit[0].getMessage()


async def test_malformed_member_row_marks_read_incomplete(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member")
    discord = _Discord([])
    discord.override = lambda _r: httpx.Response(
        200, json=[{"user": {"id": "800001"}, "roles": []}, {"user": {}, "roles": []}]
    )
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, discord)
    assert result.community_roles_demoted == 0 and world.role_of(victim) == "member"
    assert _msgs(caplog, "role_sync.demote_aborted_unknown_member_read")


async def test_empty_guild_list_demotes_nobody(
    world: _World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, _Discord([]))
    assert result.community_roles_demoted == 0 and world.role_of(victim) == "member"


# --------------------------------------------------------------------------- Discord failures


def _boom_timeout(_r: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("connect timed out")


@pytest.mark.parametrize(
    "responder",
    [
        lambda _r: httpx.Response(401, text="secret-body-401"),
        lambda _r: httpx.Response(403, text="secret-body-403"),
        lambda _r: httpx.Response(429, text="secret-body-429"),
        lambda _r: httpx.Response(500, text="secret-body-500"),
        lambda _r: httpx.Response(503, text="secret-body-503"),
        _boom_timeout,
    ],
    ids=["401", "403", "429", "500", "503", "timeout"],
)
async def test_discord_failure_demotes_nobody_and_logs_type_only(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    responder: Callable[[httpx.Request], httpx.Response],
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member", username="bob_secret")
    discord = _Discord([])
    discord.override = responder
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = await _run(world, pairing, discord)

    assert result.error == "DiscordSyncError" and result.community_roles_demoted == 0
    assert world.role_of(victim) == "member"
    failed = _msgs(caplog, "role_sync.platform_pairing_failed ")
    assert len(failed) == 1 and failed[0].levelno == logging.ERROR
    assert "error_type=DiscordSyncError" in failed[0].getMessage()
    _assert_pii_free(caplog, "secret-body", "bob_secret", "800001", "fake-bot-token")


# --------------------------------------------------------------------------- resolution guards


async def test_discord_to_hub_user_to_member_resolution_guards(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.roles()
    other = world._community("c2")
    world.roles(other)
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member")
    owner = world.member("800002", "community-owner")
    vip = world.member("800003", "vip")  # manual/unmanaged role
    left = world.member("800004", "member", active=False)  # inactive membership
    # same hub user also a member of ANOTHER community: must stay untouched
    elsewhere = world.member("800005", "member")
    pg.q(
        world.dal,
        "INSERT INTO community_members (community_id, user_id, role, is_active) "
        "VALUES (%s, %s, 'member', true)",
        [other, str(elsewhere)],
    )
    world.member("800006", "member", community_id=other)  # linked, but only in c2
    discord = _Discord([(f"80000{i}", []) for i in range(1, 7)])
    # present+linked+mapped-role-less: 800001 (victim), 800005 (c1 row too) -> 2 demotions
    result = await _run(world, pairing, discord)

    assert result.community_roles_demoted == 2
    assert world.role_of(victim) == BASELINE
    assert world.role_of(elsewhere) == BASELINE
    assert world.role_of(elsewhere, other) == "member"  # other community untouched
    assert world.role_of(owner) == "community-owner"
    assert world.role_of(vip) == "vip"
    assert world.role_of(left) == "member"


async def test_grant_still_applies_and_hub_row_is_never_created(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=True, mp=monkeypatch)
    promoted = world.member("800001", "vip")
    # linked hub user, but never joined this community -> skipped, no row auto-created
    ghost = int(pg.scalar(world.dal, "INSERT INTO hub_users (username) VALUES ('g') RETURNING id"))
    pg.q(
        world.dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'discord', '800002')",
        [ghost],
    )
    result = await _run(
        world,
        pairing,
        _Discord([("800001", [MEMBER_DISCORD_ROLE]), ("800002", [MEMBER_DISCORD_ROLE])]),
    )
    assert result.community_roles_applied == 1 and result.users_skipped_not_member == 1
    assert world.role_of(promoted) == "member"
    assert (
        pg.scalar(
            world.dal, "SELECT count(*) FROM community_members WHERE user_id = %s", [str(ghost)]
        )
        == 0
    )


# --------------------------------------------------------------------------- opt-in + batch


@pytest.mark.parametrize("listed", ["unset", "other-pairing", "garbage"])
async def test_default_is_grant_only_unless_pairing_listed(
    world: _World, monkeypatch: pytest.MonkeyPatch, listed: str
) -> None:
    world.roles()
    pairing = world.pairing(enable_demote=False, mp=monkeypatch)
    if listed == "other-pairing":
        monkeypatch.setenv(svc.DEMOTE_PAIRING_IDS_ENV, str(pairing.id + 1000))
    elif listed == "garbage":
        monkeypatch.setenv(svc.DEMOTE_PAIRING_IDS_ENV, ", ,x,")
    victim = world.member("800001", "member")
    result = await _run(world, pairing, _Discord([("800001", [])]))
    assert result.community_roles_demoted == 0 and world.role_of(victim) == "member"


async def test_batch_entrypoint_demotes_and_counts(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.roles()
    world.pairing(enable_demote=True, mp=monkeypatch)
    victim = world.member("800001", "member")
    discord = _Discord([("800001", [])])
    summary = await svc.run_role_sync_reconcile_batch(
        world.dal,
        get_broadcaster_user_token=AsyncMock(return_value=None),
        credential_resolver=_Resolver(),  # type: ignore[arg-type]
        make_discord_client=discord.client,
    )
    assert (summary.pairings_examined, summary.pairings_synced, summary.pairings_failed) == (
        1,
        1,
        0,
    )
    assert world.role_of(victim) == BASELINE
