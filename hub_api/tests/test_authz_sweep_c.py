"""Community write/admin authorization, sweep C: event proxy, guild pairing, access tokens.

These routes were authorized only by a TENANT-WIDE token scope (`event.calendar:admin`,
`community.guild_pairing:write`, `community.tokens:admin`, ...) plus a tenant fence, so a
maintainer of community A held every scope needed to pair a Discord guild to, mint a credential
for, or rewrite the events of community B in the same tenant. Each route now carries a community
guard (`community_admin_guard` for admin surfaces, `community_member_guard` for member-facing
ones). Every test below runs the REAL guards (no `bypass_route_guards`, no patched standing)
against seeded `community_members` rows, and fails when the guard is removed.

The ingest-sources routes (inline guard, `mark_guarded`) are proven in
`tests/test_read_route_inline_guards.py`, keyed in its `INLINE_GUARD_DENY_TESTS` registry.
"""

from __future__ import annotations

# ruff: noqa: E501
import dataclasses
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.event as event_module
from blueprints.v1.access_token import community_tokens_bp
from blueprints.v1.event import (
    ALL_ROUTES,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    event_calendar_admin_bp,
)
from blueprints.v1.guild_pairing import guild_pairing_bp
from services import access_token_service as token_svc
from services import discord_role_mirror as mirror_svc
from services import guild_pairing as pairing_svc
from services.community_common import ensure_community_tables
from services.event_calendar_proxy import ProxyResult
from services.global_identity import ensure_tables as ensure_global_tables
from services.schema import (
    bind_calendar_child_tables,
    bind_calendar_sync_tables,
    bind_support_token_tables,
)
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids, public_id
from tests.conftest import make_user_token, seed_membership
from tests.route_guard_walker import guard_kind, iter_routes, required_scopes

#: 71 administers A only (the "maintainer of A"), 72 is a plain member of B, 73 a plain member of
#: A, 74 administers B, 75 belongs to no community, 76 is a tenant admin.
ADMIN_A, MEMBER_B, MEMBER_A, ADMIN_B, OUTSIDER, TENANT_ADMIN = 71, 72, 73, 74, 75, 76
#: Event 11 belongs to community A, event 12 to community B.
EVENT_A, EVENT_B = 11, 12
DENIED = (403, 404)
TICKET_UUID_A = "11111111-1111-4111-8111-111111111111"
TICKET_UUID_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def world(guild_pairing_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Quart app over the real schema: communities A, B, a foreign-tenant one, seeded members."""
    dal, a, tenant_id, global_tenant_id = guild_pairing_db
    ensure_community_tables(dal, migrate=True)
    ensure_global_tables(dal, migrate=True)
    bind_support_token_tables(dal, migrate=True)
    bind_calendar_sync_tables(dal, migrate=True)
    b = int(dal.communities.insert(name="b", tenant_id=tenant_id))
    foreign = int(dal.communities.insert(name="foreign", tenant_id=global_tenant_id))
    dal.permission_scopes.insert(
        scope_key="chat:read", display_name="Read chat", description="d", category="chat"
    )
    dal.tenant_admins.insert(user_id=TENANT_ADMIN, tenant_id=tenant_id)
    for event_id, community in ((EVENT_A, a), (EVENT_B, b)):
        dal.calendar_events.insert(
            id=event_id,
            community_id=community,
            title="t",
            event_date=datetime(2030, 1, 1),
            status="scheduled",
        )
    bind_calendar_child_tables(dal, migrate=True)
    for table, a_id, b_id in (
        ("calendar_tickets", 7, 17),
        ("calendar_ticket_types", 5, 15),
        ("calendar_event_admins", 8, 18),
    ):
        dal[table].insert(id=a_id, event_id=EVENT_A)
        dal[table].insert(id=b_id, event_id=EVENT_B)
    dal.calendar_tickets[7] = {"ticket_uuid": TICKET_UUID_A, "ticket_code": "code-a"}
    dal.calendar_tickets[17] = {"ticket_uuid": TICKET_UUID_B, "ticket_code": "code-b"}
    dal.commit()
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=a, user_id=ADMIN_A, role="community-admin")
    seed_membership(db, community_id=b, user_id=MEMBER_B, role="member")
    seed_membership(db, community_id=a, user_id=MEMBER_A, role="member")
    seed_membership(db, community_id=b, user_id=ADMIN_B, role="community-admin")

    app = Quart(__name__)
    QuartSchema(app)
    app.config["dal"] = dal
    install_community_ids(app, dal)
    for bp in (guild_pairing_bp, community_tokens_bp, event_calendar_admin_bp):
        app.register_blueprint(bp)
    # Flag/upstream stubs only: the guards under test are the real ones.
    monkeypatch.setattr(event_module, "feature_enabled", AsyncMock(return_value=True))
    import blueprints.v1.guild_pairing as gp_module

    monkeypatch.setattr(gp_module, "feature_enabled", AsyncMock(return_value=True))
    proxy = AsyncMock(return_value=ProxyResult(ok=True, status_code=200, body={"ok": True}))
    monkeypatch.setattr(event_module._proxy_client, "request", proxy)
    return SimpleNamespace(app=app, dal=dal, a=a, b=b, foreign=foreign, proxy=proxy)


# --------------------------------------------------------------------------------------------
# Route discovery (every community route of the three blueprints, a denominator, a registry)
# --------------------------------------------------------------------------------------------

_CHILD_IDS = {
    "<int:pairing_id>": "1",
    "<int:binding_id>": "1",
    "<int:mapping_id>": "1",
    "<int:token_id>": "1",
    "<event_id>": str(EVENT_A),
    "<ticket_id>": "7",
    "<type_id>": "5",
    "<admin_id>": "8",
}


def _community_routes() -> list[Any]:
    """`pytest.param(method, rule, scopes, kind)` for every community-scoped route examined."""
    probe = Quart(__name__)
    for bp in (guild_pairing_bp, community_tokens_bp, event_calendar_admin_bp):
        probe.register_blueprint(bp)
    params: list[Any] = []
    for method, rule, view in iter_routes(probe):
        if "<cuuid:community_id>" not in rule:
            continue
        params.append(
            pytest.param(
                method, rule, required_scopes(view), guard_kind(view), id=f"{method} {rule}"
            )
        )
    return sorted(params, key=lambda p: p.id or "")


ROUTES = _community_routes()


def _url(world: Any, rule: str, community_id: int) -> str:
    """Concrete URL: the community's public UUID plus placeholder child ids."""
    filled = rule
    for token, value in _CHILD_IDS.items():
        filled = filled.replace(token, value)
    return cpath(world.dal, filled.replace("<cuuid:community_id>", "{community_id}"), community_id)


async def _call(
    world: Any, method: str, url: str, uid: int, scopes: list[str], body: Any = None
) -> Any:
    token = make_user_token(user_id=uid, scope=" ".join(["*:read", *scopes]))
    return await world.app.test_client().open(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
        json=body if body is not None else ({} if method not in ("GET", "DELETE") else None),
    )


def test_route_discovery_has_a_denominator() -> None:
    """A discovery pointed at the wrong place would parametrize nothing and pass vacuously."""
    guild = [p for p in ROUTES if "guild-pairings" in p.id or "/discord/" in p.id]
    tokens = [p for p in ROUTES if "/tokens/" in p.id]
    events = [p for p in ROUTES if "/calendar/" in p.id]
    assert (len(guild), len(tokens), len(events)) == (12, 4, 32), (
        len(guild),
        len(tokens),
        len(events),
    )
    unguarded = [p.id for p in ROUTES if p.values[3] not in ("admin", "member")]
    assert unguarded == [], f"community routes with no community guard: {unguarded}"


#: `(METHOD, rule)` -> the keyed test proving the real guard refuses the maintainer of A on B.
#: Every route of the three blueprints must be keyed; a new route without one fails the meta test.
SWEEP_C_DENY_TESTS: dict[tuple[str, str], Any] = {}


async def test_maintainer_of_a_is_refused_on_b(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    """Maintainer of A holding the route's exact scopes is refused on community B."""
    response = await _call(world, method, _url(world, rule, world.b), ADMIN_A, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


test_maintainer_of_a_is_refused_on_b = pytest.mark.parametrize(
    ("method", "rule", "scopes", "kind"), ROUTES
)(test_maintainer_of_a_is_refused_on_b)  # type: ignore[assignment]
SWEEP_C_DENY_TESTS.update(
    {(p.values[0], p.values[1]): test_maintainer_of_a_is_refused_on_b for p in ROUTES}
)


def test_every_route_has_a_keyed_deny_test() -> None:
    live = {(p.values[0], p.values[1]) for p in ROUTES}
    assert live == set(SWEEP_C_DENY_TESTS), live ^ set(SWEEP_C_DENY_TESTS)
    assert all(
        callable(fn) and fn.__name__.startswith("test_") for fn in SWEEP_C_DENY_TESTS.values()
    )


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_plain_member_is_refused_on_admin_routes(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    if kind == "member":
        pytest.skip("member-facing route: a member is allowed by design")
    response = await _call(world, method, _url(world, rule, world.b), MEMBER_B, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_non_member_is_refused_everywhere(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    """A signed-in user with no standing in the community gets nothing (member routes too)."""
    response = await _call(world, method, _url(world, rule, world.a), OUTSIDER, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_foreign_and_unknown_community_are_one_404(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    """Another tenant's community and a non-existent one are indistinguishable 404s."""
    foreign = await _call(world, method, _url(world, rule, world.foreign), ADMIN_A, scopes)
    unknown_url = _url(world, rule, world.a).replace(
        public_id(world.dal, world.a), UNKNOWN_PUBLIC_ID
    )
    unknown = await _call(world, method, unknown_url, ADMIN_A, scopes)
    assert (foreign.status_code, unknown.status_code) == (404, 404), rule
    assert (await foreign.get_json())["error"]["message"] == "Community not found"
    assert (await unknown.get_json())["error"]["message"] == "Community not found"


# --------------------------------------------------------------------------------------------
# Member-facing routes: allowed for a member, acting only as the caller
# --------------------------------------------------------------------------------------------

_MEMBER_ROUTES = [p for p in ROUTES if p.values[3] == "member"]


def test_member_facing_routes_are_the_expected_four() -> None:
    assert sorted(p.values[1].split("calendar/events/<event_id>")[-1] for p in _MEMBER_ROUTES) == [
        "/my-permissions",
        "/rsvp",
        "/rsvp",
        "/rsvp-counts",
    ]


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), _MEMBER_ROUTES)
async def test_member_is_allowed_on_member_routes(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    response = await _call(world, method, _url(world, rule, world.a), MEMBER_A, scopes)
    assert response.status_code == 200, f"{method} {rule} -> {response.status_code}"


async def test_rsvp_forwards_the_member_identity_and_never_a_caller_chosen_one(world: Any) -> None:
    """An RSVP acts as the verified caller: role is `member`, identity body keys are dropped."""
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/rsvp"
    body = {
        "status": "going",
        "user_id": "999",
        "platform_user_id": "999",
        "username": "mallory",
        "role": "admin",
    }
    response = await _call(world, "POST", _url(world, rule, world.a), MEMBER_A, [SCOPE_WRITE], body)
    assert response.status_code == 200
    kwargs = world.proxy.await_args.kwargs
    assert kwargs["user_context"].role == "member"
    assert kwargs["user_context"].user_id == str(MEMBER_A)
    assert kwargs["json_body"] == {"status": "going"}


# --------------------------------------------------------------------------------------------
# Admin routes: a community admin is allowed on their own community (guard is not a blanket deny)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "rule"),
    [
        ("GET", "/api/v1/communities/<cuuid:community_id>/guild-pairings"),
        ("GET", "/api/v1/admin/<cuuid:community_id>/tokens/cats"),
        ("GET", "/api/v1/admin/<cuuid:community_id>/tokens/scopes"),
        ("GET", "/api/v1/admin/<cuuid:community_id>/calendar/events"),
        ("GET", "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>"),
    ],
)
async def test_community_admin_is_allowed_in_own_community(
    world: Any, method: str, rule: str
) -> None:
    scopes = next(p.values[2] for p in ROUTES if p.values[:2] == (method, rule))
    response = await _call(world, method, _url(world, rule, world.a), ADMIN_A, scopes)
    assert response.status_code == 200, f"{method} {rule} -> {response.status_code}"


# --------------------------------------------------------------------------------------------
# Event proxy: verified community forwarded, event id fenced to it, upstream is not trusted
# --------------------------------------------------------------------------------------------

_EVENT_ID_ROUTES = [p for p in ROUTES if "<event_id>" in p.values[1]]


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), _EVENT_ID_ROUTES)
async def test_event_id_from_another_community_is_404(
    world: Any, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    """IDOR: B's event id under A's URL is refused for B's own admin AND A's admin; never proxied."""
    url = _url(world, rule, world.a).replace(f"/events/{EVENT_A}", f"/events/{EVENT_B}")
    uid = MEMBER_A if kind == "member" else ADMIN_A
    response = await _call(world, method, url, uid, scopes)
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"
    assert (await response.get_json())["error"]["message"] == "Event not found"
    world.proxy.assert_not_awaited()


#: placeholder -> (id of the child in A's event, id of the same kind of child in B's event)
_CHILD_PAIRS = {"<ticket_id>": ("7", "17"), "<type_id>": ("5", "15"), "<admin_id>": ("8", "18")}
_CHILD_ROUTES = [
    pytest.param(p, placeholder, id=f"{p.id}")
    for p in ROUTES
    for placeholder in _CHILD_PAIRS
    if placeholder in p.values[1]
]


def test_child_routes_cover_every_child_type() -> None:
    kinds = {placeholder for _p, placeholder in (q.values for q in _CHILD_ROUTES)}
    assert kinds == set(_CHILD_PAIRS)
    assert len(_CHILD_ROUTES) == 8  # tickets x4, ticket types x2, event admins x2


@pytest.mark.parametrize(("route", "placeholder"), _CHILD_ROUTES)
async def test_child_id_from_another_event_or_community_is_404(
    world: Any, route: Any, placeholder: str
) -> None:
    """IDOR: A's event + B's ticket / type / admin id -> 404, never proxied; own child proxies."""
    method, rule, scopes, _kind = route.values
    own, foreign = _CHILD_PAIRS[placeholder]
    base = _url(world, rule, world.a).replace(f"/{_CHILD_IDS[placeholder]}", "/@@", 1)
    for bad in (foreign, "999", "abc", "7;x"):
        response = await _call(world, method, base.replace("@@", bad), ADMIN_A, scopes)
        assert response.status_code == 404, f"{method} {rule} child={bad}"
    world.proxy.assert_not_awaited()
    ok = await _call(world, method, base.replace("@@", own), ADMIN_A, scopes)
    assert ok.status_code != 404, f"{method} {rule}: own child must pass"
    world.proxy.assert_awaited_once()


#: body/query field -> (own child in A's event, same-kind child of B's event)
_REF_FIELD_VALUES = {
    "ticket_id": ("7", "17"),
    "ticket_uuid": (TICKET_UUID_A, TICKET_UUID_B),
    "ticket_code": ("code-a", "code-b"),
    "ticket_type_id": ("5", "15"),
    "type_id": ("5", "15"),
    "admin_id": ("8", "18"),
}
_BODY_ROUTES = {
    (r.method, "/api/v1/admin" + r.rule.replace("<community_id>", "<cuuid:community_id>")): r
    for r in ALL_ROUTES
    if r.has_body and "<event_id>" in r.rule and r.scope is not None
}
_BODY_PARAMS = [
    pytest.param(p, field, id=f"{p.id}|{field}")
    for p in ROUTES
    if p.values[:2] in _BODY_ROUTES
    for field in _REF_FIELD_VALUES
]


def test_child_reference_fields_are_all_covered() -> None:
    """The deny tests cover every field the proxy treats as a child reference."""
    assert set(_REF_FIELD_VALUES) == set(event_module._CHILD_REF_FIELDS)
    assert len(_BODY_ROUTES) >= 14 and len(_BODY_PARAMS) == len(_BODY_ROUTES) * len(
        _REF_FIELD_VALUES
    )


@pytest.mark.parametrize(("route", "field"), _BODY_PARAMS)
async def test_child_id_in_body_from_another_community_is_404(
    world: Any, route: Any, field: str
) -> None:
    """IDOR via the body: B's ticket/type/admin reference on A's event is refused, not forwarded."""
    method, rule, scopes, _kind = route.values
    own, foreign = _REF_FIELD_VALUES[field]
    url = _url(world, rule, world.a)
    uid = MEMBER_A if _kind == "member" else ADMIN_A
    refused = await _call(world, method, url, uid, scopes, {field: foreign})
    assert refused.status_code == 404, f"{method} {rule} body.{field}"
    world.proxy.assert_not_awaited()
    allowed = await _call(world, method, url, uid, scopes, {field: own})
    assert allowed.status_code != 404, f"{method} {rule} body.{field}: own reference must pass"


@pytest.mark.parametrize("field", ["ticket_type_id"])
async def test_child_id_in_query_from_another_community_is_404(world: Any, field: str) -> None:
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/tickets"
    base = _url(world, rule, world.a)
    own, foreign = _REF_FIELD_VALUES[field]
    refused = await _call(world, "GET", f"{base}?{field}={foreign}", ADMIN_A, [SCOPE_ADMIN])
    assert refused.status_code == 404
    world.proxy.assert_not_awaited()
    allowed = await _call(world, "GET", f"{base}?{field}={own}", ADMIN_A, [SCOPE_ADMIN])
    assert allowed.status_code == 200


async def test_non_scalar_child_reference_is_refused(world: Any) -> None:
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/check-in"
    url = _url(world, rule, world.a)
    for bad in ([17], {"id": 17}, True):
        response = await _call(world, "POST", url, ADMIN_A, [SCOPE_ADMIN], {"ticket_id": bad})
        assert response.status_code == 404, bad
    world.proxy.assert_not_awaited()


async def test_ticket_uuid_is_checked_against_the_event_too(world: Any) -> None:
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>/tickets/<ticket_id>"
    base = _url(world, rule, world.a).replace("/tickets/7", "/tickets/@@")
    foreign = await _call(world, "GET", base.replace("@@", TICKET_UUID_B), ADMIN_A, [SCOPE_ADMIN])
    assert foreign.status_code == 404
    world.proxy.assert_not_awaited()
    own = await _call(world, "GET", base.replace("@@", TICKET_UUID_A), ADMIN_A, [SCOPE_ADMIN])
    assert own.status_code == 200


async def test_unknown_and_non_numeric_event_ids_are_404(world: Any) -> None:
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events/<event_id>"
    base = _url(world, rule, world.a)
    for bad in ("999", "abc", "1;drop"):
        response = await _call(
            world,
            "GET",
            base.replace(f"/events/{EVENT_A}", f"/events/{bad}"),
            ADMIN_A,
            [SCOPE_ADMIN],
        )
        assert response.status_code == 404, bad
    world.proxy.assert_not_awaited()


async def test_proxy_forwards_the_verified_url_community_not_a_body_one(world: Any) -> None:
    """A body `community_id` naming B cannot redirect an A-admin's call; upstream sees A."""
    rule = "/api/v1/admin/<cuuid:community_id>/calendar/events"
    response = await _call(
        world,
        "POST",
        _url(world, rule, world.a),
        ADMIN_A,
        [SCOPE_ADMIN],
        {"title": "x", "community_id": world.b},
    )
    assert response.status_code == 201
    args, kwargs = world.proxy.await_args.args, world.proxy.await_args.kwargs
    assert args[1] == f"/api/v1/calendar/{world.a}/events"
    assert kwargs["json_body"]["community_id"] == world.a
    assert kwargs["user_context"].role == "admin"


async def test_verify_ticket_has_no_community_to_bind_so_needs_a_tenant_admin(world: Any) -> None:
    """`verify-ticket` names its target in the body; only a tenant admin may use it."""
    url = "/api/v1/admin/calendar/verify-ticket"
    body = {"ticket_code": "abc", "perform_checkin": True}
    for uid in (ADMIN_A, MEMBER_A, OUTSIDER):
        response = await _call(world, "POST", url, uid, [SCOPE_WRITE], body)
        assert response.status_code == 403, uid
    world.proxy.assert_not_awaited()
    allowed = await _call(world, "POST", url, TENANT_ADMIN, [SCOPE_WRITE], body)
    assert allowed.status_code == 200
    world.proxy.assert_awaited_once()


def test_every_scoped_event_route_is_classified() -> None:
    """Import-time rule: a community URL route can never register without a community guard."""
    scoped = [r for r in ALL_ROUTES if r.scope is not None and "<community_id>" in r.rule]
    assert len(scoped) == 32
    for route in scoped:
        assert event_module._guard_kind(route) in ("admin", "member"), route.name
    assert {r.name for r in ALL_ROUTES if r.scope in (SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE)}
    stray = dataclasses.replace(scoped[0], scope=SCOPE_WRITE, member_guard=False)
    with pytest.raises(RuntimeError):
        event_module._guard_kind(stray)


# --------------------------------------------------------------------------------------------
# Guild pairing: child ids are scoped to the URL community
# --------------------------------------------------------------------------------------------


def _b_pairing(world: Any) -> int:
    pairing = pairing_svc.create_pairing(
        world.dal,
        world.b,
        discord_guild_id="222222222222222222",
        direction="bidirectional",
        role_name_prefix="[B]",
        ownership_verified=True,
        actor_user_id=ADMIN_B,
    )
    return int(pairing.id)


async def test_pairing_id_from_another_community_is_404_and_untouched(world: Any) -> None:
    pairing_id = _b_pairing(world)
    base = cpath(
        world.dal, f"/api/v1/communities/{{community_id}}/guild-pairings/{pairing_id}", world.a
    )
    scopes = ["community.guild_pairing:write"]
    patched = await _call(world, "PATCH", base, ADMIN_A, scopes, {"sync_enabled": True})
    deleted = await _call(world, "DELETE", base, ADMIN_A, scopes)
    bindings = await _call(
        world, "GET", base + "/role-bindings", ADMIN_A, ["community.guild_pairing:read"]
    )
    created = await _call(
        world,
        "POST",
        base + "/role-bindings",
        ADMIN_A,
        scopes,
        {"sync_scope": "moderator", "discord_role_id": "1"},
    )
    assert [r.status_code for r in (patched, deleted, bindings, created)] == [404] * 4
    survivor = pairing_svc.get_pairing(world.dal, world.b, pairing_id)
    assert survivor is not None and survivor.sync_enabled is False


async def test_binding_and_mirror_ids_from_another_community_are_404(world: Any) -> None:
    pairing_id = _b_pairing(world)
    scopes = ["community.guild_pairing:write"]
    binding_url = cpath(
        world.dal,
        f"/api/v1/communities/{{community_id}}/guild-pairings/{pairing_id}/role-bindings/1",
        world.a,
    )
    assert (await _call(world, "DELETE", binding_url, ADMIN_A, scopes)).status_code == 404
    mirror_svc.list_mappings(world.dal, world.b)  # binds the table
    mapping_id = int(
        world.dal.discord_role_mirror_mappings.insert(
            community_id=world.b,
            source_guild_id="1",
            source_role_id="2",
            target_guild_id="3",
            target_role_id="4",
        )
    )
    world.dal.commit()
    mirror_url = cpath(
        world.dal,
        f"/api/v1/communities/{{community_id}}/discord/role-mirrors/{mapping_id}",
        world.a,
    )
    assert (await _call(world, "DELETE", mirror_url, ADMIN_A, scopes)).status_code == 404
    assert mirror_svc.list_mappings(world.dal, world.b), "B's mapping must survive A's attempt"


# --------------------------------------------------------------------------------------------
# Access tokens: scoped child ids, credentials never re-disclosed
# --------------------------------------------------------------------------------------------

_CAT_SCOPES = ["community.tokens:admin"]


async def test_token_id_from_another_community_is_404_and_not_revoked(world: Any) -> None:
    token_svc.create_cat(
        world.dal,
        world.b,
        created_by_user_id=ADMIN_B,
        name="b-cat",
        scopes=["chat:read"],
        expires_at=None,
    )
    row = world.dal(world.dal.community_access_tokens.community_id == world.b).select().first()
    url = cpath(world.dal, f"/api/v1/admin/{{community_id}}/tokens/cats/{row.id}", world.a)
    response = await _call(world, "DELETE", url, ADMIN_A, _CAT_SCOPES)
    assert response.status_code == 404
    world.dal.commit()
    assert not world.dal.community_access_tokens[row.id].is_revoked


async def test_credential_is_returned_once_and_never_listed(world: Any) -> None:
    create_url = cpath(world.dal, "/api/v1/admin/{community_id}/tokens/cats", world.a)
    created = await _call(
        world, "POST", create_url, ADMIN_A, _CAT_SCOPES, {"name": "ci", "scopes": ["chat:read"]}
    )
    assert created.status_code == 201
    secret = (await created.get_json())["token"]
    assert secret.startswith("wdl_c_")
    listed = await _call(world, "GET", create_url, ADMIN_A, _CAT_SCOPES)
    text = json.dumps(await listed.get_json())
    assert secret not in text and "token_hash" not in text and '"token"' not in text
    stored = world.dal(world.dal.community_access_tokens.community_id == world.a).select().first()
    assert secret not in (stored.token_hash or ""), "only a hash may be stored"


async def test_maintainer_of_a_cannot_mint_a_credential_for_b(world: Any) -> None:
    create_url = cpath(world.dal, "/api/v1/admin/{community_id}/tokens/cats", world.b)
    response = await _call(
        world, "POST", create_url, ADMIN_A, _CAT_SCOPES, {"name": "evil", "scopes": ["chat:read"]}
    )
    assert response.status_code == 403
    assert world.dal(world.dal.community_access_tokens.community_id == world.b).count() == 0
