"""Handle/mention -> UUID and UUID -> display-name lookups against REAL Postgres (#429 / #427).

Round 2 of the identity PII boundary: `resolve_target()` (free-text `@handle` and Discord
`<@id>` mentions -> UUID, the key `!secret` / `lastseen` need) and `resolve_display_names()`
(UUID -> name, only for egress detokenization), plus their gRPC adapters. Everything runs on
a fully migrated Postgres 17 container with the real `AsyncDAL` -- no mocked boundary --
including the DB-level proof that `waddles_bundle_reader` (a real login, not just `SET ROLE`)
cannot read any raw handle or run the matching queries. Skipped, never failed, where the
`docker` CLI is unavailable (same posture as every real-Postgres test in this repo).
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import grpc
import psycopg2
import pytest
from flask_core.database import AsyncDAL

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
from services import identity_resolution_service as _svc  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    MAX_DISPLAY_NAME_LEN,
    AmbiguousHandleError,
    HandleNotFoundError,
    IdentityResolutionError,
    IdentityValidationError,
    ParsedTarget,
    TargetNotMemberError,
    TenantNotFoundError,
    _clean_display_name,
    parse_target,
    resolve_display_names,
    resolve_identity,
    resolve_target,
)
from tests._identity_grpc import AuthedIdentityStub, make_issuer, serving  # noqa: E402

_READER_ROLE = "waddles_bundle_reader"
_READER_DEFAULT_PW = "pg-docker-harness-default-reader-pw"

#: Raw PII strings the logs/responses must never carry.
HANDLE_SECRET = "SensitiveHandle_do_not_log"
NAME_SECRET = "Sensitive Display Name"


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("platform", "raw", "expected"),
    [
        ("twitch", "@bob", ParsedTarget("handle", "bob")),
        ("twitch", "bob", ParsedTarget("handle", "bob")),
        ("twitch", "  @Bob  ", ParsedTarget("handle", "Bob")),
        ("twitch", "@ bob", ParsedTarget("handle", "bob")),
        ("discord", "<@123456789012345678>", ParsedTarget("mention", "123456789012345678")),
        ("discord", "<@!42>", ParsedTarget("mention", "42")),
    ],
)
def test_parse_target_accepts(platform: str, raw: str, expected: ParsedTarget) -> None:
    assert parse_target(platform, raw) == expected


@pytest.mark.parametrize(
    ("platform", "raw"),
    [
        ("discord", ""),
        ("discord", "   "),
        ("discord", "@"),
        ("discord", "@@bob"),
        ("discord", "<@&123>"),  # role mention
        ("discord", "<#123>"),  # channel mention
        ("discord", "<:emoji:123>"),
        ("discord", "<@abc>"),
        ("twitch", "<@123>"),  # a Discord mention is not a twitch user reference
        ("discord", "@everyone"),
        ("discord", "@HERE"),
        ("discord", "a\x00b"),
        ("discord", "a\nb"),
        ("discord", "x" * 256),
        ("discord", "bo<b>"),
        ("Bad Platform", "bob"),
        ("discord", None),
    ],
)
def test_parse_target_rejects(platform: str, raw: Any) -> None:
    with pytest.raises(IdentityValidationError):
        parse_target(platform, raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Bob   the\tBuilder ", "Bob the Builder"),
        ("line1\nline2\r\nline3", "line1 line2 line3"),
        ("evil‮ecnalubma", "evilecnalubma"),  # bidi override dropped
        ("a b c", "a b c"),
        ("x" * (MAX_DISPLAY_NAME_LEN + 30), "x" * MAX_DISPLAY_NAME_LEN),
        ("‮‏", ""),
        ("   ", ""),
        (None, ""),
        (123, ""),
    ],
)
def test_clean_display_name(raw: Any, expected: str) -> None:
    assert _clean_display_name(raw) == expected


def test_clean_display_name_keeps_emoji_and_unicode() -> None:
    assert _clean_display_name("Zoë 🐧") == "Zoë 🐧"


# --------------------------------------------------------------------------- DB fixtures

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0045)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-identity-lookup") as db:
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


async def _hub_user(
    adal: AsyncDAL, *, display_name: str | None = None, username: str = "u"
) -> tuple[int, str]:
    row = (
        await adal.executesql_async(
            "INSERT INTO hub_users (username, display_name) VALUES (%s, %s) "
            "RETURNING id, uuid::text",
            [username, display_name],
        )
    )[0]
    return int(row[0]), str(row[1])


async def _identity(
    adal: AsyncDAL, hub_id: int, platform: str, puid: str, username: str | None
) -> None:
    await adal.executesql_async(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id, "
        "platform_username) VALUES (%s, %s, %s, %s)",
        [hub_id, platform, puid, username],
    )


async def _member(
    adal: AsyncDAL,
    community_id: int,
    *,
    platform: str | None = None,
    puid: str | None = None,
    display: str | None = None,
    user_id: str | None = None,
) -> str | None:
    value = await _one(
        adal,
        "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
        "display_name) VALUES (%s, %s, %s, %s, %s) RETURNING user_uuid::text",
        [community_id, user_id, platform, puid, display],
    )
    if value is not None and display:
        # The membership trigger never copies display names into the PII table (alembic
        # 0047); a pseudonym's handle arrives from the platform-asserted mint that
        # svc-process performs with the sender's name. Emulate that mint here.
        await adal.executesql_async(
            "UPDATE ephemeral_pseudonyms SET handle = %s WHERE pseudonym = %s::uuid",
            [display, value],
        )
    return None if value is None else str(value)


async def _pseudonym_count(adal: AsyncDAL) -> int:
    return int(await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms"))


# --------------------------------------------------------------------- handle -> UUID (DB)


@pg_only
async def test_handle_resolves_unlinked_pseudonym_case_insensitively(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    minted = await _member(adal, c, platform="twitch", puid="tw-1", display="BobTheBuilder")
    assert minted is not None
    for raw in ("@bobthebuilder", "BOBTHEBUILDER", " @BobTheBuilder "):
        got = await resolve_target(adal, "acme", "twitch", raw)
        assert got.uuid == uuid.UUID(minted)
        assert got.kind == "handle"
    # slug and numeric id address the same tenant
    assert (await resolve_target(adal, str(t), "twitch", "bobthebuilder")).uuid == uuid.UUID(minted)


@pg_only
async def test_handle_resolves_linked_hub_user_via_platform_username(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "discord", "d-1", "Alice")
    await _member(adal, c, platform="discord", puid="d-1")
    got = await resolve_target(adal, "acme", "discord", "@alice")
    assert (got.uuid, got.kind) == (uuid.UUID(hub_uuid), "handle")


@pg_only
@pytest.mark.parametrize("platform_username", ["Carol", None])
async def test_linked_identity_supersedes_its_own_stale_pseudonym(
    adal: AsyncDAL, platform_username: str | None
) -> None:
    """A pseudonym minted before the account was linked must not collide with the link.

    Twitch: its chat handle is the platform-unique login, so the minted handle is matchable
    (a Discord display name never is -- see test_identity_wrong_recipient_pg_integration).
    """
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    pseudonym = await _member(adal, c, platform="twitch", puid="tw-9", display="Carol")
    assert pseudonym is not None
    hub_id, hub_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "twitch", "tw-9", platform_username)
    got = await resolve_target(adal, "acme", "twitch", "carol")
    assert got.uuid == uuid.UUID(hub_uuid)  # one candidate, the linked uuid -- not ambiguous
    assert got.uuid != uuid.UUID(pseudonym)
    # ...and it is the same uuid resolve_identity() hands out for that platform id
    assert await resolve_identity(adal, "acme", "twitch", "tw-9") == got.uuid


@pg_only
async def test_handle_collision_two_pseudonyms_fails_loud(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    await _member(adal, c, platform="twitch", puid="tw-1", display="Dupe")
    await _member(adal, c, platform="twitch", puid="tw-2", display="dupe")
    with pytest.raises(AmbiguousHandleError):
        await resolve_target(adal, "acme", "twitch", "@DUPE")


@pg_only
async def test_handle_collision_pseudonym_vs_linked_user_fails_loud(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    await _member(adal, c, platform="twitch", puid="tw-1", display="Sam")
    hub_id, _ = await _hub_user(adal)
    await _identity(adal, hub_id, "twitch", "tw-2", "sam")
    await _member(adal, c, platform="twitch", puid="tw-2")
    with pytest.raises(AmbiguousHandleError):
        await resolve_target(adal, "acme", "twitch", "sam")


@pg_only
async def test_same_handle_in_two_tenants_resolves_independently(adal: AsyncDAL) -> None:
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1, c2 = await _community(adal, t1), await _community(adal, t2)
    p1 = await _member(adal, c1, platform="twitch", puid="tw-1", display="Pat")
    p2 = await _member(adal, c2, platform="twitch", puid="tw-2", display="Pat")
    assert p1 and p2 and p1 != p2
    assert (await resolve_target(adal, "t1", "twitch", "pat")).uuid == uuid.UUID(p1)
    assert (await resolve_target(adal, "t2", "twitch", "pat")).uuid == uuid.UUID(p2)


@pg_only
async def test_linked_user_is_invisible_outside_their_tenant(adal: AsyncDAL) -> None:
    """No cross-tenant handle oracle: a username only known in t1 is not found in t2."""
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1 = await _community(adal, t1)
    await _community(adal, t2)
    hub_id, hub_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "discord", "d-1", "Dana")
    await _member(adal, c1, platform="discord", puid="d-1")
    assert (await resolve_target(adal, "t1", "discord", "dana")).uuid == uuid.UUID(hub_uuid)
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "t2", "discord", "dana")


@pg_only
async def test_handle_is_platform_scoped(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    await _member(adal, c, platform="twitch", puid="tw-1", display="Robin")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "discord", "robin")


@pg_only
async def test_not_found_fails_loud_and_never_mints(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    before = await _pseudonym_count(adal)
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@nobody")
    assert await _pseudonym_count(adal) == before


@pg_only
@pytest.mark.parametrize("raw", ["%", "_", "b_b", "bob%", "b%", "bob' OR '1'='1", "bob'; --"])
async def test_wildcards_and_injection_never_match(adal: AsyncDAL, raw: str) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    await _member(adal, c, platform="twitch", puid="tw-1", display="Bob")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", raw)
    assert await _one(adal, "SELECT count(*) FROM hub_users") == 0  # table intact


@pg_only
async def test_mention_resolves_known_linked_and_unknown_ids(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    known = await _member(adal, c, platform="discord", puid="555", display="Quinn")
    assert known is not None
    for raw in ("<@555>", "<@!555>"):
        got = await resolve_target(adal, "acme", "discord", raw)
        assert (got.uuid, got.kind) == (uuid.UUID(known), "mention")
    # an id that is not a current member is refused and NEVER minted (a mention is a lookup,
    # not a way to create pseudonym rows or correlation tokens for strangers)
    before = await _pseudonym_count(adal)
    for _ in range(2):
        with pytest.raises(TargetNotMemberError):
            await resolve_target(adal, "acme", "discord", "<@999>")
    assert await _pseudonym_count(adal) == before
    # linked ids resolve to the hub uuid -- but only for a member of the tenant
    hub_id, hub_uuid = await _hub_user(adal)
    await _identity(adal, hub_id, "discord", "777", None)
    with pytest.raises(TargetNotMemberError):  # linked elsewhere, not a member here
        await resolve_target(adal, "acme", "discord", "<@777>")
    await _member(adal, c, platform="discord", puid="777")
    assert (await resolve_target(adal, "acme", "discord", "<@777>")).uuid == uuid.UUID(hub_uuid)


@pg_only
async def test_target_validation_and_unknown_tenant_fail_loud(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    with pytest.raises(IdentityValidationError):
        await resolve_target(adal, "acme", "discord", "<@&123>")
    with pytest.raises(IdentityValidationError):
        await resolve_target(adal, "", "discord", "bob")
    with pytest.raises(IdentityValidationError):
        await resolve_target(adal, "acme\x00", "discord", "bob")
    with pytest.raises(TenantNotFoundError):
        await resolve_target(adal, "ghost", "discord", "bob")
    with pytest.raises(TenantNotFoundError):
        await resolve_target(adal, "ghost", "discord", "<@1>")


@pg_only
@pytest.mark.parametrize(
    ("platform", "raw", "table"),
    [
        ("twitch", "@bob", "ephemeral_pseudonyms"),  # pseudonym-login path
        ("discord", "@bob", "hub_user_identities"),  # verified-login path
        ("discord", "<@1>", "community_members"),  # mention membership gate
    ],
)
async def test_target_db_failure_is_wrapped_not_defaulted(
    adal: AsyncDAL, platform: str, raw: str, table: str
) -> None:
    await _tenant(adal, "acme")
    await adal.executesql_async(f"ALTER TABLE {table} RENAME TO tbl_tmp")  # noqa: S608
    try:
        with pytest.raises(IdentityResolutionError) as exc_info:
            await resolve_target(adal, "acme", platform, raw)
        assert not isinstance(exc_info.value, HandleNotFoundError | AmbiguousHandleError)
        assert exc_info.value.__cause__ is not None  # driver error chained, not swallowed
    finally:
        await adal.executesql_async(f"ALTER TABLE tbl_tmp RENAME TO {table}")  # noqa: S608


# ------------------------------------------------------------------ UUID -> display names


@pg_only
async def test_display_names_hub_user_pseudonym_and_fallbacks(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    # hub profile name wins
    h1, u1 = await _hub_user(adal, display_name="  Profile Name ", username="h1@example.com")
    await _member(adal, c, user_id=str(h1), display="Member Name 1")
    # blank profile name -> tenant member display name
    h2, u2 = await _hub_user(adal, display_name="   ", username="h2@example.com")
    await _member(adal, c, user_id=str(h2), display="Member Name 2")
    # no names anywhere but a linked platform username -> NOT that: the username fallback was
    # any-platform and tenant-agnostic (a Twitch login could surface in a Discord message), so
    # the user reports unresolved instead
    h3, u3 = await _hub_user(adal, display_name=None, username="h3@example.com")
    await _identity(adal, h3, "twitch", "tw-3", "linked_login")
    await _member(adal, c, user_id=str(h3))
    # unlinked chatter -> pseudonym handle
    p4 = await _member(adal, c, platform="twitch", puid="tw-4", display="Chatter Four")
    assert p4 is not None

    got = await resolve_display_names(adal, "acme", [u1, u2, u3, p4])
    assert got.unresolved == (u3,)
    by_uuid = {n.uuid: n for n in got.names}
    assert (by_uuid[u1].display_name, by_uuid[u1].is_hub_user) == ("Profile Name", True)
    assert (by_uuid[u2].display_name, by_uuid[u2].is_hub_user) == ("Member Name 2", True)
    assert (by_uuid[p4].display_name, by_uuid[p4].is_hub_user) == ("Chatter Four", False)
    assert [n.uuid for n in got.names] == [u1, u2, p4]  # request order kept


@pg_only
async def test_display_names_never_use_username_or_email(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    h, hub_uuid = await _hub_user(adal, display_name=None, username="secret.person@example.com")
    await _member(adal, c, user_id=str(h))  # member of the tenant, but no usable name anywhere
    got = await resolve_display_names(adal, "acme", [hub_uuid])
    assert got.names == ()
    assert got.unresolved == (hub_uuid,)


@pg_only
async def test_display_names_unresolved_is_fail_safe_empty(adal: AsyncDAL) -> None:
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1, c2 = await _community(adal, t1), await _community(adal, t2)
    other_pseudonym = await _member(adal, c2, platform="twitch", puid="tw-x", display="Elsewhere")
    h_other, hub_other = await _hub_user(adal, display_name="Other Tenant User")
    await _member(adal, c2, user_id=str(h_other))
    no_handle = await _member(adal, c1, platform="twitch", puid="tw-nohandle")  # NULL handle
    blank = await _member(adal, c1, platform="twitch", puid="tw-blank", display="‮‏ ")
    assert other_pseudonym and no_handle and blank
    unknown = str(uuid.uuid4())
    tokens = [unknown, "not-a-uuid", other_pseudonym, hub_other, no_handle, blank]
    before = await _pseudonym_count(adal)
    got = await resolve_display_names(adal, "t1", tokens)
    assert got.names == ()
    assert got.unresolved == tuple(tokens)  # every miss reported, none fabricated
    assert await _pseudonym_count(adal) == before  # lookups never mint


@pg_only
async def test_display_names_batch_semantics_echo_dedupe_and_cap(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    p = await _member(adal, c, platform="twitch", puid="tw-1", display="Ana")
    assert p is not None
    shouting = p.upper()  # caller's own token form must be echoed back for keying
    got = await resolve_display_names(adal, "acme", [shouting, p, "x" * 200])
    assert [n.uuid for n in got.names] == [shouting]  # deduped on canonical uuid, first form kept
    assert got.unresolved == ("x" * 64,)  # malformed echo is bounded
    long_name = await _member(adal, c, platform="twitch", puid="tw-2", display="N" * 200)
    assert long_name is not None
    capped = await resolve_display_names(adal, "acme", [long_name])
    assert capped.names[0].display_name == "N" * MAX_DISPLAY_NAME_LEN


@pg_only
async def test_display_names_sanitize_stored_names(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    p = await _member(adal, c, platform="twitch", puid="tw-1", display="evil‮\nname\t@here")
    assert p is not None
    got = await resolve_display_names(adal, "acme", [p])
    assert got.names[0].display_name == "evil name @here"


@pg_only
async def test_display_names_validation_and_unknown_tenant(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    with pytest.raises(IdentityValidationError):
        await resolve_display_names(adal, "acme", [])
    with pytest.raises(IdentityValidationError):
        await resolve_display_names(adal, "acme", [str(uuid.uuid4()) for _ in range(101)])
    with pytest.raises(IdentityValidationError):
        await resolve_display_names(adal, "", [str(uuid.uuid4())])
    with pytest.raises(TenantNotFoundError):
        await resolve_display_names(adal, "ghost", [str(uuid.uuid4())])
    # a batch of only-malformed tokens needs no DB and cannot fail on an unknown tenant lookup
    only_bad = await resolve_display_names(adal, "ghost", ["nope"])
    assert (only_bad.names, only_bad.unresolved) == ((), ("nope",))


@pg_only
async def test_display_names_db_failure_is_wrapped_not_defaulted(adal: AsyncDAL) -> None:
    await _tenant(adal, "acme")
    await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
    try:
        with pytest.raises(IdentityResolutionError) as exc_info:
            await resolve_display_names(adal, "acme", [str(uuid.uuid4())])
        assert exc_info.value.__cause__ is not None
    finally:
        await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")


# ------------------------------------------------------------------- PII boundary + logs


def _reader_connection(db: PgTestDatabase) -> Any:
    """A genuine login as waddles_bundle_reader (not SET ROLE from a superuser)."""
    return psycopg2.connect(
        host=db.host,
        port=db.port,
        dbname=db.dbname,
        user=_READER_ROLE,
        password=os.environ.get("DB_READER_PASSWORD", _READER_DEFAULT_PW),
    )


def _reader_denied(db: PgTestDatabase, sql: str, params: list[Any] | None = None) -> str:
    """Run ``sql`` as the bundle reader; return the error text, failing if it succeeded."""
    conn = _reader_connection(db)
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                cur.execute(sql, params)
            return str(exc_info.value)
    finally:
        conn.rollback()
        conn.close()


@pg_only
async def test_reader_role_cannot_read_raw_handles_or_run_the_lookups(
    adal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal, display_name=NAME_SECRET, username="login@example.com")
    await _identity(adal, hub_id, "discord", "d-1", HANDLE_SECRET)
    await _member(adal, c, platform="discord", puid="d-1", display=HANDLE_SECRET)
    pseudonym = await _member(adal, c, platform="twitch", puid="tw-1", display=HANDLE_SECRET)
    assert pseudonym is not None
    # the real service path works (hub-api's own role)...
    assert (await resolve_target(adal, "acme", "discord", HANDLE_SECRET.lower())).uuid == (
        uuid.UUID(hub_uuid)
    )
    # ...while the data-plane reader is denied every raw-handle/name column, directly
    for sql in (
        "SELECT handle FROM ephemeral_pseudonyms",
        "SELECT * FROM ephemeral_pseudonyms",
        "SELECT platform_username FROM hub_user_identities",
        "SELECT display_name FROM community_members",
        "SELECT display_name FROM hub_users",
        "SELECT username FROM hub_users",
    ):
        assert "permission denied" in _reader_denied(pg_db, sql)
    # ...cannot run the handle->uuid join or either display-name query as an oracle
    for sql, params in (
        (_svc._LINKED_LOGIN_CANDIDATES_SQL, ["discord", "x", t]),
        (_svc._PSEUDONYM_LOGIN_CANDIDATES_SQL, [t, "twitch", "x"]),
        (_svc._MENTION_MEMBER_SQL, [t, "discord", "1", "discord", "1", "discord", "1"]),
    ):
        assert "permission denied" in _reader_denied(pg_db, sql, params)
    assert "permission denied" in _reader_denied(
        pg_db, _svc._HUB_NAMES_SQL, [t, [str(uuid.uuid4())], t]
    )
    assert "permission denied" in _reader_denied(
        pg_db, _svc._PSEUDONYM_NAMES_SQL, [t, [str(uuid.uuid4())]]
    )
    # ...and the resolver function is not executable by it (no uuid-minting oracle either)
    assert "permission denied" in _reader_denied(
        pg_db, "SELECT resolve_identity_uuid(%s, 'discord', 'zzz', NULL, NULL)", [t]
    )
    # the sanctioned PII-free view stays readable and projects no handle column
    conn = _reader_connection(pg_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM community_member_identities")
            columns = {d.name for d in cur.description}
            rows = cur.fetchall()
    finally:
        conn.rollback()
        conn.close()
    assert columns == {
        "community_id",
        "platform",
        "platform_user_id",
        "hub_user_uuid",
        "user_uuid",
        "user_uuid_status",
        "user_uuid_unavailable_reason",
    }
    assert HANDLE_SECRET not in str(rows) and NAME_SECRET not in str(rows)


@pg_only
async def test_logs_carry_no_handles_or_names(
    adal: AsyncDAL, caplog: pytest.LogCaptureFixture
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    minted = await _member(adal, c, platform="twitch", puid="pii-id-1", display=HANDLE_SECRET)
    await _member(adal, c, platform="twitch", puid="pii-id-2", display="DupeSecret")
    await _member(adal, c, platform="twitch", puid="pii-id-3", display="dupesecret")
    hub_id, hub_uuid = await _hub_user(adal, display_name=NAME_SECRET)
    await _member(adal, c, user_id=str(hub_id))
    await _member(adal, c, platform="discord", puid="424242")
    assert minted is not None
    caplog.set_level(logging.DEBUG)

    await resolve_target(adal, "acme", "twitch", f"@{HANDLE_SECRET}")  # ok
    await resolve_target(adal, "acme", "discord", "<@424242>")  # ok (mention)
    with pytest.raises(TargetNotMemberError):
        await resolve_target(adal, "acme", "discord", "<@535353>")  # non-member mention
    with pytest.raises(HandleNotFoundError):  # discord nickname: refused, never matched
        await resolve_target(adal, "acme", "discord", "@NicknameSecret")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@GhostSecretHandle")
    with pytest.raises(AmbiguousHandleError):
        await resolve_target(adal, "acme", "twitch", "@DupeSecret")
    with pytest.raises(IdentityValidationError):
        await resolve_target(adal, "acme", "twitch", "<@&SecretRole>")
    with pytest.raises(TenantNotFoundError):
        await resolve_target(adal, "ghost-tenant", "twitch", "@LeakyTenantHandle")
    await resolve_display_names(adal, "acme", [minted, hub_uuid, str(uuid.uuid4())])
    await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
    try:
        with pytest.raises(IdentityResolutionError):
            await resolve_target(adal, "acme", "twitch", "@DbFailureSecret")
    finally:
        await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")

    assert caplog.records
    text = " ".join(f"{r.getMessage()} {r.__dict__} {r.exc_text}" for r in caplog.records)
    for secret in (
        HANDLE_SECRET,
        NAME_SECRET,
        "DupeSecret",
        "dupesecret",
        "GhostSecretHandle",
        "SecretRole",
        "LeakyTenantHandle",
        "DbFailureSecret",
        "NicknameSecret",
        "424242",
        "535353",
        "pii-id-1",
    ):
        assert secret not in text, f"{secret!r} leaked into logs"
    assert "identity_resolve_target" in text and "identity_resolve_display_names" in text


# ------------------------------------------------------------------------------ gRPC


@pytest.fixture
async def grpc_addr(adal: AsyncDAL) -> AsyncIterator[str]:
    """In-process server: real IdentityServicer + real DAL behind the REAL interceptor chain."""
    async with serving(adal) as addr:
        yield addr


def _handle_req(target: str, tenant: str = "acme", platform: str = "twitch") -> Any:
    return identity_pb2.ResolveHandleRequest(tenant_id=tenant, platform=platform, target=target)


@pg_only
async def test_grpc_resolve_handle_real_path(adal: AsyncDAL, grpc_addr: str) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    minted = await _member(adal, c, platform="twitch", puid="tw-1", display=HANDLE_SECRET)
    await _member(adal, c, platform="twitch", puid="tw-2", display="Twin")
    await _member(adal, c, platform="twitch", puid="tw-3", display="twin")
    dm = await _member(adal, c, platform="discord", puid="808", display="Disc")
    async with grpc.aio.insecure_channel(grpc_addr) as ch:
        stub = AuthedIdentityStub(ch)
        ok = await stub.ResolveHandle(_handle_req(f"@{HANDLE_SECRET}"), timeout=5)
        assert ok.uuid == minted and ok.match_kind == identity_pb2.MATCH_KIND_HANDLE
        assert HANDLE_SECRET.encode() not in ok.SerializeToString()  # handle never echoed
        mention = await stub.ResolveHandle(_handle_req("<@808>", platform="discord"), timeout=5)
        assert mention.uuid == dm and mention.match_kind == identity_pb2.MATCH_KIND_MENTION
        expectations = {
            "@nobody": grpc.StatusCode.NOT_FOUND,
            "@twin": grpc.StatusCode.FAILED_PRECONDITION,
            "@everyone": grpc.StatusCode.INVALID_ARGUMENT,
            "": grpc.StatusCode.INVALID_ARGUMENT,
        }
        for target, code in expectations.items():
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.ResolveHandle(_handle_req(target), timeout=5)
            assert exc.value.code() == code, target
            assert "nobody" not in (exc.value.details() or "")  # no handle in status detail
        with pytest.raises(grpc.aio.AioRpcError) as ghost:
            await stub.ResolveHandle(_handle_req("@x", tenant="ghost"), timeout=5)
        assert ghost.value.code() == grpc.StatusCode.NOT_FOUND
        await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as boom:
                await stub.ResolveHandle(_handle_req("@x"), timeout=5)
            assert boom.value.code() == grpc.StatusCode.INTERNAL
            assert boom.value.details() == "identity resolution failed"
        finally:
            await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")


@pg_only
async def test_grpc_resolve_display_names_real_path(adal: AsyncDAL, grpc_addr: str) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, hub_uuid = await _hub_user(adal, display_name="Hubby")
    hub_id = int(await _one(adal, "SELECT id FROM hub_users WHERE uuid = %s::uuid", [hub_uuid]))
    await _member(adal, c, user_id=str(hub_id))
    pseudonym = await _member(adal, c, platform="twitch", puid="tw-1", display="Chatty")
    assert pseudonym is not None
    missing = str(uuid.uuid4())
    async with grpc.aio.insecure_channel(grpc_addr) as ch:
        stub = AuthedIdentityStub(ch)
        resp = await stub.ResolveDisplayNames(
            identity_pb2.ResolveDisplayNamesRequest(
                tenant_id="acme", uuids=[hub_uuid, pseudonym, missing]
            ),
            timeout=5,
        )
        assert [(n.uuid, n.display_name, n.is_hub_user) for n in resp.names] == [
            (hub_uuid, "Hubby", True),
            (pseudonym, "Chatty", False),
        ]
        assert list(resp.unresolved_uuids) == [missing]
        with pytest.raises(grpc.aio.AioRpcError) as too_many:
            await stub.ResolveDisplayNames(
                identity_pb2.ResolveDisplayNamesRequest(
                    tenant_id="acme", uuids=[str(uuid.uuid4()) for _ in range(101)]
                ),
                timeout=5,
            )
        assert too_many.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as ghost:
            await stub.ResolveDisplayNames(
                identity_pb2.ResolveDisplayNamesRequest(tenant_id="ghost", uuids=[missing]),
                timeout=5,
            )
        assert ghost.value.code() == grpc.StatusCode.NOT_FOUND
        await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as boom:
                await stub.ResolveDisplayNames(
                    identity_pb2.ResolveDisplayNamesRequest(tenant_id="acme", uuids=[missing]),
                    timeout=5,
                )
            assert boom.value.code() == grpc.StatusCode.INTERNAL
        finally:
            await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")


async def test_grpc_new_rpcs_without_dal_are_unavailable_not_default() -> None:
    async with serving(None) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch)
        with pytest.raises(grpc.aio.AioRpcError) as handle:
            await stub.ResolveHandle(_handle_req("@bob"), timeout=5)
        assert handle.value.code() == grpc.StatusCode.UNAVAILABLE
        with pytest.raises(grpc.aio.AioRpcError) as names:
            await stub.ResolveDisplayNames(
                identity_pb2.ResolveDisplayNamesRequest(
                    tenant_id="acme", uuids=[str(uuid.uuid4())]
                ),
                timeout=5,
            )
        assert names.value.code() == grpc.StatusCode.UNAVAILABLE


_SPIFFE = "spiffe://penguintech.io/alpha/svc-process"
_HANDLE_SCOPE = "identity:handle:resolve"


def test_resolve_handle_has_its_own_registered_scope() -> None:
    method = "/waddles.hub.internal.v1.IdentityService/ResolveHandle"
    assert REQUIRED_SCOPES[method] == _HANDLE_SCOPE
    assert _HANDLE_SCOPE not in {
        scope for m, scope in REQUIRED_SCOPES.items() if not m.endswith("/ResolveHandle")
    }


@pg_only
async def test_resolve_handle_requires_its_own_scope_end_to_end(adal: AsyncDAL) -> None:
    """The full interceptor chain: a mint-scoped token cannot resolve handles."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    minted = await _member(adal, c, platform="twitch", puid="tw-1", display="Scoped")
    issuer = make_issuer()  # system tenant, may hold every identity scope
    async with serving(adal, issuer) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = identity_pb2_grpc.IdentityServiceStub(ch)
        wrong = issuer.issue(_SPIFFE, "identity:ephemeral:mint")
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.ResolveHandle(
                _handle_req("@scoped"),
                metadata=(("authorization", f"Bearer {wrong}"),),
                timeout=2,
            )
        assert denied.value.code() == grpc.StatusCode.UNAUTHENTICATED
        right = issuer.issue(_SPIFFE, _HANDLE_SCOPE)
        ok = await stub.ResolveHandle(
            _handle_req("@scoped"),
            metadata=(("authorization", f"Bearer {right}"),),
            timeout=2,
        )
        assert ok.uuid == minted
