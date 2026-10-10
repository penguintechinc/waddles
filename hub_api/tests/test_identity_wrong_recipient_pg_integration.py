"""Wrong-recipient hardening of the handle -> UUID path against a REAL Postgres (#748 review).

A `!secret @alice` that resolves to the wrong person delivers the secret to an attacker, so
every refusal here must be loud and every match must be of a stable, verified identity:

- a Discord display name / nickname (mutable, non-unique, self-chosen) never matches --
  only an OAuth-verified ``platform_username`` does, and only Twitch's login-equals-handle
  chat names match through the pseudonym store
- every handle AND mention match requires CURRENT community membership (not left, not
  removed, community active, hub user active) and a mention never mints for non-members
- no username fallback in display names (cross-platform / cross-tenant PII leak)
- format / zero-width characters cannot blank or hide a name
- failures log + span-event type and SQLSTATE only -- never the driver message (handles)

Real triggers/functions on Postgres 17, the real `AsyncDAL`, and the production gRPC
interceptor chain. Skipped (never failed) where the `docker` CLI is unavailable.
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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))
_PB_ROOT = Path(__file__).resolve().parents[1] / "grpc_internal" / "pb"
if str(_PB_ROOT) not in sys.path:  # generated waddles.* stubs, same as grpc_internal/__init__
    sys.path.insert(0, str(_PB_ROOT))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)
from waddles.hub.internal.v1 import identity_pb2  # noqa: E402

from services import identity_resolution_service as svc  # noqa: E402
from services.identity_resolution_service import (  # noqa: E402
    AmbiguousHandleError,
    HandleNotFoundError,
    HandleUnverifiedError,
    IdentityRequest,
    IdentityResolutionError,
    IdentityValidationError,
    TargetNotMemberError,
    _clean_display_name,
    parse_target,
    resolve_display_names,
    resolve_identities,
    resolve_target,
)
from tests._identity_grpc import AuthedIdentityStub, serving  # noqa: E402

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

LEAK_HANDLE = "LeakyHandle_do_not_log"
LEAK_PUID = "leaky-platform-id-4242"


# ------------------------------------------------------------------ pure input hardening


@pytest.mark.parametrize(
    "raw",
    [
        "\u200b",  # zero-width space
        "\u2060",  # word joiner
        "\ufeff",  # BOM / zero-width no-break space
        "\U000e0041",  # tag character
        "\u200b\u2060\ufeff",
        "\u200c\u200d",  # ZWNJ / ZWJ
        "\u00ad",  # soft hyphen
        "\u3164",  # Hangul filler
        "\uffa0",  # halfwidth Hangul filler
        "\u2800",  # braille blank
        "\u034f",  # combining grapheme joiner
        "\ue000",  # private use
        "\u0301",  # a lone combining mark renders as a dotted circle, not a name
        " \t\u200b ",
    ],
)
def test_blank_looking_names_are_rejected(raw: str) -> None:
    """A name with no visible character is `""` (reported unresolved), never blank text."""
    assert _clean_display_name(raw) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Al\u200bice", "Alice"),
        ("\u2060Bob\ufeff", "Bob"),
        ("a\u00adb", "ab"),
        ("x\U000e0041y", "xy"),
        ("Zo\u00eb \U0001f427", "Zo\u00eb \U0001f427"),
        ("\u00e9", "\u00e9"),  # a combining mark ON a letter is kept
    ],
)
def test_invisible_characters_are_stripped_but_visible_text_is_kept(
    raw: str, expected: str
) -> None:
    assert _clean_display_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "bo\u200bb",
        "\u2060bob",
        "\ufeffbob",
        "bob\u200b",
        "bob\U000e0041",
        "\u3164bob",
        "\uff20everyone",  # fullwidth @everyone: judged by what it renders as
        "\uff20\uff48\uff45\uff52\uff45",
        "@\uff25veryone",
    ],
)
def test_parse_target_rejects_invisible_and_fullwidth_non_user_refs(raw: str) -> None:
    with pytest.raises(IdentityValidationError):
        parse_target("twitch", raw)


def test_parse_target_rejects_non_ascii_digit_mentions_and_normalises_the_rest() -> None:
    with pytest.raises(IdentityValidationError):
        parse_target("discord", "<@\u0661\u0662\u0663>")  # Arabic-Indic digits
    # NFKC: a fullwidth login is the same login as its plain form
    assert parse_target("twitch", "\uff20\uff41\uff4c\uff49\uff43\uff45") == parse_target(
        "twitch", "@alice"
    )
    assert parse_target("twitch", "x" * 255).value == "x" * 255
    with pytest.raises(IdentityValidationError):
        parse_target("twitch", "x" * 256)


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("ALICE", "alice", True),
        ("\uff41\uff4c\uff49\uff43\uff45", "alice", True),  # fullwidth
        ("\ufb01sh", "fish", True),  # fi ligature
        ("\u0430lice", "alice", False),  # Cyrillic \u0430 is a DIFFERENT login, not folded away
        ("alice", "alice2", False),
    ],
)
def test_fold_handle_is_nfkc_casefold(a: str, b: str, same: bool) -> None:
    assert (svc._fold_handle(a) == svc._fold_handle(b)) is same


def test_display_name_sql_has_no_platform_username_fallback() -> None:
    """The any-platform / any-tenant username fallback must stay gone."""
    assert "platform_username" not in svc._HUB_NAMES_SQL
    assert "hub_user_identities" not in svc._HUB_NAMES_SQL


# --------------------------------------------------------------------------- DB fixtures


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-identity-wrong-recipient") as db:
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


async def _link(
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
) -> tuple[int, str]:
    """Insert a member; returns `(member id, derived user_uuid)`.

    `display` models the platform-asserted name svc-process sends with a mint: it lands as
    the pseudonym's handle (the membership trigger never copies display names itself).
    """
    row = (
        await adal.executesql_async(
            "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
            "display_name) VALUES (%s, %s, %s, %s, %s) RETURNING id, user_uuid::text",
            [community_id, user_id, platform, puid, display],
        )
    )[0]
    assert row[1] is not None
    if display:
        await adal.executesql_async(
            "UPDATE ephemeral_pseudonyms SET handle = %s WHERE pseudonym = %s::uuid",
            [display, row[1]],
        )
    return int(row[0]), str(row[1])


async def _pseudonym_count(adal: AsyncDAL) -> int:
    return int(await _one(adal, "SELECT count(*) FROM ephemeral_pseudonyms"))


# ------------------------------------------------- 1. nickname impersonation (wrong recipient)


@pg_only
async def test_discord_nickname_cannot_steal_a_verified_login(adal: AsyncDAL) -> None:
    """`!secret @alice` goes to the verified alice, never to whoever is NICKNAMED alice."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, alice_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-alice", "alice")
    await _member(adal, c, platform="discord", puid="d-alice", display="Alice (real)")
    _, attacker = await _member(adal, c, platform="discord", puid="d-evil", display="alice")
    got = await resolve_target(adal, "acme", "discord", "@alice")
    assert got.uuid == uuid.UUID(alice_uuid)
    assert got.uuid != uuid.UUID(attacker)
    assert (await resolve_target(adal, "acme", "discord", "ALICE")).uuid == got.uuid


@pg_only
async def test_discord_display_name_only_is_refused_not_guessed(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    await _member(adal, c, platform="discord", puid="d-evil", display="alice")
    # a unique nickname is still just a nickname...
    with pytest.raises(HandleUnverifiedError):
        await resolve_target(adal, "acme", "discord", "@alice")
    # ...two of them are not "ambiguous, pick one" either...
    await _member(adal, c, platform="discord", puid="d-evil2", display="Alice")
    before = await _pseudonym_count(adal)
    with pytest.raises(HandleUnverifiedError):
        await resolve_target(adal, "acme", "discord", "@alice")
    # ...and the refusal is uniform: it does not reveal whether anyone wears the nickname
    with pytest.raises(HandleUnverifiedError):
        await resolve_target(adal, "acme", "discord", "@nobody-wears-this")
    assert await _pseudonym_count(adal) == before  # lookups never mint
    # HandleUnverifiedError IS-A HandleNotFoundError: callers that handle NOT_FOUND still work
    assert issubclass(HandleUnverifiedError, HandleNotFoundError)


@pg_only
async def test_nickname_collision_with_a_verified_login_does_not_make_it_ambiguous(
    adal: AsyncDAL,
) -> None:
    """Attacker nicknames themselves like the target: neither a steal NOR a denial of service."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, bob_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-bob", "bob")
    await _member(adal, c, platform="discord", puid="d-bob")
    for i in range(3):
        await _member(adal, c, platform="discord", puid=f"d-evil{i}", display="bob")
    assert (await resolve_target(adal, "acme", "discord", "@bob")).uuid == uuid.UUID(bob_uuid)


@pg_only
async def test_two_verified_logins_that_collide_fail_loud(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    for puid in ("d-1", "d-2"):
        hub_id, _ = await _hub_user(adal)
        await _link(adal, hub_id, "discord", puid, "twin")
        await _member(adal, c, platform="discord", puid=puid)
    with pytest.raises(AmbiguousHandleError):
        await resolve_target(adal, "acme", "discord", "@twin")


@pg_only
async def test_twitch_login_handle_still_resolves_for_a_current_member(adal: AsyncDAL) -> None:
    """Positive control: on Twitch the chat handle IS the unique login, so it matches."""
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, tw = await _member(adal, c, platform="twitch", puid="tw-1", display="BobTheBuilder")
    got = await resolve_target(adal, "acme", "twitch", "@bobthebuilder")
    assert (got.uuid, got.kind) == (uuid.UUID(tw), "handle")
    with pytest.raises(HandleNotFoundError) as miss:
        await resolve_target(adal, "acme", "twitch", "@someone-else")
    assert not isinstance(miss.value, HandleUnverifiedError)  # a plain miss on twitch


@pg_only
async def test_lookalike_and_compatibility_spellings(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, alice_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-alice", "alice")
    await _member(adal, c, platform="discord", puid="d-alice")
    # fullwidth spelling of the same login: same person
    assert (
        await resolve_target(adal, "acme", "discord", "\uff20\uff41\uff4c\uff49\uff43\uff45")
    ).uuid == uuid.UUID(alice_uuid)
    # a Cyrillic-\u0430 lookalike is a different login: refused, not folded onto alice
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "discord", "@\u0430lice")


# --------------------------- 2. current membership required (left / removed / erased users)

#: name -> (scope id the SQL binds, sql that ENDS the membership, sql that restores it)
_ENDINGS: dict[str, tuple[str, str, str]] = {
    "left": (
        "community",
        "UPDATE community_members SET left_at = now() WHERE community_id = %s",
        "UPDATE community_members SET left_at = NULL WHERE community_id = %s",
    ),
    "removed": (
        "community",
        "UPDATE community_members SET removed_at = now() WHERE community_id = %s",
        "UPDATE community_members SET removed_at = NULL WHERE community_id = %s",
    ),
    "deactivated": (
        "community",
        "UPDATE community_members SET is_active = FALSE WHERE community_id = %s",
        "UPDATE community_members SET is_active = TRUE WHERE community_id = %s",
    ),
    "community_deleted": (
        "community",
        "UPDATE communities SET deleted_at = now() WHERE id = %s",
        "UPDATE communities SET deleted_at = NULL WHERE id = %s",
    ),
    "community_inactive": (
        "community",
        "UPDATE communities SET is_active = FALSE WHERE id = %s",
        "UPDATE communities SET is_active = TRUE WHERE id = %s",
    ),
    "hub_user_inactive": (
        "hub",
        "UPDATE hub_users SET is_active = FALSE WHERE id = %s",
        "UPDATE hub_users SET is_active = TRUE WHERE id = %s",
    ),
}
_MEMBERSHIP_ENDINGS = [k for k, v in _ENDINGS.items() if v[0] == "community"]


@pg_only
@pytest.mark.parametrize("ending", list(_ENDINGS))
async def test_verified_login_requires_current_membership_and_an_active_user(
    adal: AsyncDAL, ending: str
) -> None:
    scope, end_sql, restore_sql = _ENDINGS[ending]
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-1", "dana")
    await _member(adal, c, platform="discord", puid="d-1")
    scope_id = c if scope == "community" else hub_id
    assert (await resolve_target(adal, "acme", "discord", "@dana")).uuid == uuid.UUID(hub_uuid)
    await adal.executesql_async(end_sql, [scope_id])
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "discord", "@dana")
    await adal.executesql_async(restore_sql, [scope_id])  # rejoining restores the lookup
    assert (await resolve_target(adal, "acme", "discord", "@dana")).uuid == uuid.UUID(hub_uuid)


@pg_only
async def test_a_verified_login_that_is_not_a_member_is_not_found(adal: AsyncDAL) -> None:
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1, _ = await _community(adal, t1), await _community(adal, t2)
    hub_id, _ = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-1", "erin")
    with pytest.raises(HandleNotFoundError):  # linked but a member of nothing
        await resolve_target(adal, "t1", "discord", "@erin")
    await _member(adal, c1, platform="discord", puid="d-1")
    await resolve_target(adal, "t1", "discord", "@erin")
    with pytest.raises(HandleNotFoundError):  # a member of t1 only
        await resolve_target(adal, "t2", "discord", "@erin")


@pg_only
@pytest.mark.parametrize("ending", _MEMBERSHIP_ENDINGS)
async def test_pseudonym_login_requires_current_membership(adal: AsyncDAL, ending: str) -> None:
    scope, end_sql, restore_sql = _ENDINGS[ending]
    assert scope == "community"
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, tw = await _member(adal, c, platform="twitch", puid="tw-1", display="Frank")
    assert (await resolve_target(adal, "acme", "twitch", "@frank")).uuid == uuid.UUID(tw)
    await adal.executesql_async(end_sql, [c])
    with pytest.raises(HandleNotFoundError):  # left / removed users are never returned
        await resolve_target(adal, "acme", "twitch", "@frank")
    await adal.executesql_async(restore_sql, [c])
    assert (await resolve_target(adal, "acme", "twitch", "@frank")).uuid == uuid.UUID(tw)


@pg_only
async def test_pseudonym_login_of_a_non_member_is_not_found(adal: AsyncDAL) -> None:
    """A pseudonym minted by chat (ingest) is not membership: no member row, no handle match."""
    t = await _tenant(adal, "acme")
    await _tenant(adal, "globex")
    await resolve_identities(adal, [IdentityRequest("acme", "twitch", "tw-9", "Ghosty")])
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@ghosty")
    c = await _community(adal, t)
    await _member(adal, c, platform="twitch", puid="tw-9")  # now a member: matchable
    await resolve_target(adal, "acme", "twitch", "@ghosty")


@pg_only
async def test_an_erased_identity_is_never_returned(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, _ = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-1", "gina")
    await _member(adal, c, platform="discord", puid="d-1")
    await _member(adal, c, platform="twitch", puid="tw-1", display="Gina")
    await resolve_target(adal, "acme", "discord", "@gina")
    await resolve_target(adal, "acme", "twitch", "@gina")
    # account erasure: the identity link goes (hub_user_identities cascades) and the handle
    # on the pseudonym is wiped -- neither path may still produce a recipient
    await adal.executesql_async("DELETE FROM hub_user_identities WHERE hub_user_id = %s", [hub_id])
    await svc.erase_pseudonym_handles(adal, "twitch", "tw-1")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "discord", "@gina")
    with pytest.raises(HandleNotFoundError):
        await resolve_target(adal, "acme", "twitch", "@gina")


# ----------------------------------------- 3. mention membership (no mint, no correlation)


@pg_only
async def test_mention_of_a_non_member_is_refused_and_never_mints(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    await _community(adal, t)
    events_before = await _one(adal, "SELECT count(*) FROM identity_resolution_events")
    for n in range(60):  # the old behaviour minted one row per distinct id
        with pytest.raises(TargetNotMemberError):
            await resolve_target(adal, "acme", "discord", f"<@{100000 + n}>")
    assert await _pseudonym_count(adal) == 0
    assert await _one(adal, "SELECT count(*) FROM identity_resolution_events") == events_before


@pg_only
@pytest.mark.parametrize("ending", _MEMBERSHIP_ENDINGS)
async def test_mention_requires_current_membership(adal: AsyncDAL, ending: str) -> None:
    _, end_sql, restore_sql = _ENDINGS[ending]
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, member_uuid = await _member(adal, c, platform="discord", puid="555")
    got = await resolve_target(adal, "acme", "discord", "<@555>")
    assert (got.uuid, got.kind) == (uuid.UUID(member_uuid), "mention")
    await adal.executesql_async(end_sql, [c])
    before = await _pseudonym_count(adal)
    with pytest.raises(TargetNotMemberError):
        await resolve_target(adal, "acme", "discord", "<@555>")
    assert await _pseudonym_count(adal) == before
    await adal.executesql_async(restore_sql, [c])
    assert (await resolve_target(adal, "acme", "discord", "<@555>")).uuid == uuid.UUID(member_uuid)


@pg_only
async def test_mention_is_tenant_gated_and_does_not_correlate_across_tenants(
    adal: AsyncDAL,
) -> None:
    ta, tb = await _tenant(adal, "acme"), await _tenant(adal, "globex")
    ca, _ = await _community(adal, ta), await _community(adal, tb)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "777", None)
    await _member(adal, ca, platform="discord", puid="777")
    assert (await resolve_target(adal, "acme", "discord", "<@777>")).uuid == uuid.UUID(hub_uuid)
    before = await _pseudonym_count(adal)
    with pytest.raises(TargetNotMemberError):  # not a globex member: no uuid, no token
        await resolve_target(adal, "globex", "discord", "<@777>")
    assert await _pseudonym_count(adal) == before


@pg_only
async def test_mention_of_a_hub_member_with_no_platform_row_resolves_via_the_link(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, hub_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "888", None)
    await _member(adal, c, user_id=str(hub_id))  # member through the hub, no platform columns
    assert (await resolve_target(adal, "acme", "discord", "<@888>")).uuid == uuid.UUID(hub_uuid)


@pg_only
async def test_target_not_member_is_indistinguishable_from_not_found_over_grpc(
    adal: AsyncDAL,
) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    hub_id, alice_uuid = await _hub_user(adal)
    await _link(adal, hub_id, "discord", "d-alice", "alice")
    await _member(adal, c, platform="discord", puid="d-alice")
    await _member(adal, c, platform="discord", puid="d-evil", display="alice")
    before = await _pseudonym_count(adal)
    async with serving(adal) as addr, grpc.aio.insecure_channel(addr) as ch:
        stub = AuthedIdentityStub(ch)

        def req(target: str) -> Any:
            return identity_pb2.ResolveHandleRequest(
                tenant_id="acme", platform="discord", target=target
            )

        ok = await stub.ResolveHandle(req("@alice"), timeout=5)
        assert ok.uuid == alice_uuid  # the verified alice, not the nickname-alice
        with pytest.raises(grpc.aio.AioRpcError) as stranger:
            await stub.ResolveHandle(req("<@4242424242>"), timeout=5)
        assert stranger.value.code() == grpc.StatusCode.NOT_FOUND
        assert stranger.value.details() == "handle not found"
        assert "4242424242" not in (stranger.value.details() or "")
        with pytest.raises(grpc.aio.AioRpcError) as nickname:
            await stub.ResolveHandle(req("@nickname-only"), timeout=5)
        assert nickname.value.code() == grpc.StatusCode.NOT_FOUND
        assert "mention" in (nickname.value.details() or "")  # tells the caller what to do
        assert "nickname-only" not in (nickname.value.details() or "")
    assert await _pseudonym_count(adal) == before


# --------------------------------- 4. no cross-platform / cross-tenant username fallback


@pg_only
async def test_display_names_never_surface_a_linked_platform_username(adal: AsyncDAL) -> None:
    t1, t2 = await _tenant(adal, "t1"), await _tenant(adal, "t2")
    c1, c2 = await _community(adal, t1), await _community(adal, t2)
    # no hub profile name, no member display name -- only linked logins on other platforms
    h1, nameless = await _hub_user(adal, display_name=None)
    await _link(adal, h1, "twitch", "tw-1", "twitch_login_secret")
    await _link(adal, h1, "discord", "d-1", "discord_login_secret")
    await _member(adal, c1, user_id=str(h1))
    await _member(adal, c2, user_id=str(h1))
    h2, named = await _hub_user(adal, display_name="Profile Name")
    await _link(adal, h2, "twitch", "tw-2", "other_login_secret")
    await _member(adal, c1, user_id=str(h2))
    for tenant in ("t1", "t2"):
        got = await resolve_display_names(adal, tenant, [nameless, named])
        assert nameless in got.unresolved, tenant
        assert "login_secret" not in str(got), tenant
    # the named user resolves only where they are a member
    assert [n.display_name for n in (await resolve_display_names(adal, "t1", [named])).names] == [
        "Profile Name"
    ]
    assert (await resolve_display_names(adal, "t2", [named])).unresolved == (named,)


@pg_only
async def test_zero_width_only_names_report_unresolved_from_the_database(adal: AsyncDAL) -> None:
    t = await _tenant(adal, "acme")
    c = await _community(adal, t)
    _, blank = await _member(adal, c, platform="twitch", puid="tw-1", display="\u200b\u2060\ufeff")
    _, spoofed = await _member(adal, c, platform="twitch", puid="tw-2", display="Gr\u200beg")
    got = await resolve_display_names(adal, "acme", [blank, spoofed])
    assert got.unresolved == (blank,)
    assert [(n.uuid, n.display_name) for n in got.names] == [(spoofed, "Greg")]


# ---------------------------------- 5. failures: PII-free logs + span exception events


class _PgLikeError(Exception):
    """A driver-style failure whose MESSAGE embeds the values being looked up."""

    pgcode = "23505"


class _LeakyDal:
    """Resolves the tenant, then fails every query with a PII-bearing driver message."""

    async def executesql_async(self, sql: str, params: list[Any] | None = None) -> list[Any]:
        if "FROM tenants" in sql:
            return [(1,)]
        raise _PgLikeError(
            f'duplicate key: Key (platform_user_id)=({LEAK_PUID}) handle "{LEAK_HANDLE}" exists'
        )


@pytest.fixture
def span_sink(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """The service's tracer pointed at a real in-process SDK + in-memory exporter (local sink)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(svc, "_tracer", provider.get_tracer("hub_api.identity.test"))
    # control: the SDK's DEFAULT exception recording DOES carry the message -- proving this
    # sink can see a leak, so the "no leak" assertions below can actually fail
    with pytest.raises(_PgLikeError), provider.get_tracer("control").start_as_current_span("c"):
        raise _PgLikeError(f"control {LEAK_HANDLE}")
    control = exporter.get_finished_spans()
    assert len(control) == 1 and LEAK_HANDLE in str(control[0].events[0].attributes)
    exporter.clear()
    return exporter


def _assert_pii_free_failure(
    exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture, span_name: str
) -> None:
    spans = [s for s in exporter.get_finished_spans() if s.name == span_name]
    print(f"telemetry check: {span_name} spans received = {len(spans)}")  # counts always printed
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert [e.name for e in span.events] == ["exception"]
    event = span.events[0].attributes or {}
    assert set(event) == {"exception.type", "exception.sqlstate"}  # no message / stacktrace
    assert event["exception.sqlstate"] == "23505"
    sink_text = f"{span.attributes} {span.status} {[(e.name, e.attributes) for e in span.events]}"
    assert LEAK_HANDLE not in sink_text and LEAK_PUID not in sink_text
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a non-domain failure must be logged at ERROR, never swallowed"
    rendered = " ".join(f"{r.getMessage()} {r.__dict__} {r.exc_text}" for r in caplog.records)
    assert LEAK_HANDLE not in rendered and LEAK_PUID not in rendered
    message = errors[0].getMessage()
    assert "_PgLikeError" in message and "sqlstate=23505" in message  # type + code
    assert "identity_resolution_service.py:" in message  # traceback frames in the rendered text


@pytest.mark.parametrize("op", ["mint", "resolve_target", "display_names"])
async def test_failures_log_and_span_type_and_sqlstate_only(
    op: str, span_sink: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    dal = _LeakyDal()
    with pytest.raises(IdentityResolutionError) as exc_info:
        if op == "mint":
            await resolve_identities(
                dal, [IdentityRequest("acme", "twitch", LEAK_PUID, LEAK_HANDLE)]
            )
        elif op == "resolve_target":
            await resolve_target(dal, "acme", "discord", f"@{LEAK_HANDLE}")
        else:
            await resolve_display_names(dal, "acme", [str(uuid.uuid4())])
    # the driver error stays chained for whoever handles the exception (never swallowed)...
    assert isinstance(exc_info.value.__cause__, _PgLikeError)
    assert LEAK_HANDLE in str(exc_info.value.__cause__)
    # ...but no telemetry sink ever receives its message
    span_name = {
        "mint": "identity.resolve",
        "resolve_target": "identity.resolve_target",
        "display_names": "identity.resolve_display_names",
    }[op]
    _assert_pii_free_failure(span_sink, caplog, span_name)


@pg_only
async def test_real_driver_failure_renders_type_and_sqlstate(
    adal: AsyncDAL, span_sink: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    await _tenant(adal, "acme")
    caplog.set_level(logging.DEBUG)
    await adal.executesql_async("ALTER TABLE ephemeral_pseudonyms RENAME TO ep_tmp")
    try:
        with pytest.raises(IdentityResolutionError):
            await resolve_target(adal, "acme", "twitch", f"@{LEAK_HANDLE}")
    finally:
        await adal.executesql_async("ALTER TABLE ep_tmp RENAME TO ephemeral_pseudonyms")
    # (flask_core's AsyncDAL logs its own "ExecuteSQL error: <driver message>" line -- a
    # separate shared-library logger, out of this service's control; assert on ours)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name == svc.logger.name]
    assert len(errors) == 1
    assert "UndefinedTable" in errors[0].getMessage()
    assert "sqlstate=42P01" in errors[0].getMessage()
    spans = [s for s in span_sink.get_finished_spans() if s.name == "identity.resolve_target"]
    print(f"telemetry check: identity.resolve_target spans received = {len(spans)}")
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    event = (spans[0].events[0].attributes or {}) if spans[0].events else {}
    assert event.get("exception.sqlstate") == "42P01"
    assert LEAK_HANDLE not in f"{spans[0].events} {errors[0].getMessage()}"
