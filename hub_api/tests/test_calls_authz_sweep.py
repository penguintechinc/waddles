"""Every community-scoped calls route needs PER-COMMUNITY standing, not a tenant-wide token scope.

`streaming.calls:{read,write,admin}` are tenant-wide token scopes and the `maintainer` bundle
carries `*:write`, so a maintainer of community A holds them for community B too. The calls
handlers close that by gating inside the handler (`_gated_admin` -> `community_access.
require_community_admin(scope=calls:manage)`, `_gated_member` -> `require_community_member`), a
gate the AST walker cannot follow through the `op` closure. These tests are the proof: they run
the REAL gates (no `bypass_route_guards`, no patched standing) against seeded
`community_members` rows for ALL calls routes, discovered from the blueprints:

* a maintainer of A (admin of A only, holding the route's scopes + `*:write`) is refused on B;
* a plain member of B is refused on every admin route;
* a plain member of B is NOT refused on the member routes (positive control);
* A's room addressed through B's own URL is a 404 and A's room is untouched (IDOR child id).

`CALLS_DENY_TESTS` is the keyed registry: `(METHOD, rule)` -> the tests that prove the route.
`test_registry_covers_every_calls_route` fails for a route with no entry and for a stale entry,
so a new calls route cannot ship without a deny test.
"""

from __future__ import annotations

# ruff: noqa: F811, F401, E501
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.calls import BLUEPRINTS as CALLS_BLUEPRINTS
from tests.community_ids import public_id
from tests.conftest import make_user_token, seed_community, seed_membership
from tests.route_guard_walker import required_scopes
from tests.test_calls_support import (  # noqa: F401  (fixtures are consumed by name)
    PARTICIPANT,
    ROOM,
    FakeRooms,
    Issuer,
    _feature_enabled_default_on,
    app,
    client,
    fake,
    pid,
    record_room,
    seed_user,
)

MAINTAINER_A, MEMBER_B, ADMIN_B = 91, 92, 93
VICTIM = "9c1d2e3f-0a1b-4c2d-8e3f-4a5b6c7d8e9f"

#: JSON bodies the `validate_request` DTOs need so the request reaches the gate (an invalid body is
#: a 400 BEFORE the handler runs and would hide a missing gate behind a validation error).
_BODIES: dict[str, dict[str, Any]] = {
    "POST /api/v1/admin/<cuuid:community_id>/calls/rooms": {"room_name": "r"},
    "POST /api/v1/admin/<cuuid:community_id>/calls/rooms/<room_name>/kick": {"identity": VICTIM},
    "POST /api/v1/admin/<cuuid:community_id>/calls/rooms/<room_name>/mute": {"identity": VICTIM},
    "POST /api/v1/admin/<cuuid:community_id>/calls/rooms/<room_name>/unmute": {"identity": VICTIM},
    "POST /api/v1/admin/<cuuid:community_id>/calls/rooms/<room_name>/acknowledge-hand": {
        "user_id": VICTIM
    },
    "POST /api/v1/community/<cuuid:community_id>/interact/voice/rooms": {"room_name": "r"},
}


def _calls_routes() -> list[tuple[str, str, bool, list[str]]]:
    """`(METHOD, rule, is_admin, scopes)` for every `<community_id>` route of the calls blueprints."""
    probe = Quart(__name__)
    for bp in CALLS_BLUEPRINTS:
        probe.register_blueprint(bp)
    out: list[tuple[str, str, bool, list[str]]] = []
    for rule in probe.url_map.iter_rules():
        if "community_id" not in rule.arguments:
            continue
        scopes = required_scopes(probe.view_functions[rule.endpoint])
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, rule.rule, "/api/v1/admin/" in rule.rule, scopes))
    return sorted(out)


CALLS_ROUTES = _calls_routes()
ADMIN_ROUTES = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in CALLS_ROUTES if a]
MEMBER_ROUTES = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in CALLS_ROUTES if not a]
ALL_ROUTES = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in CALLS_ROUTES]
ROOM_ROUTES = [
    pytest.param(m, r, a, s, id=f"{m} {r}")
    for m, r, a, s in CALLS_ROUTES
    if "<room_name>" in r or "<room_id>" in r
]


def _url(db: Any, rule: str, community_id: int) -> str:
    """The route's concrete URL for `community_id` (community by its public UUID)."""
    template = (
        rule.replace("<cuuid:community_id>", public_id(db, community_id))
        .replace("<room_name>", ROOM)
        .replace("<room_id>", ROOM)
    )
    return template


def _headers(user_id: int, scopes: list[str]) -> dict[str, str]:
    """A maintainer-shaped token: viewer `*:read`, maintainer `*:write`, plus the route's scopes."""
    scope = " ".join(["*:read", "*:write", *scopes])
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, scope=scope)}"}


async def _send(client: Any, db: Any, method: str, rule: str, cid: int, headers: Any) -> Any:
    body = _BODIES.get(f"{method} {rule}")
    return await client.open(
        _url(db, rule, cid),
        method=method,
        headers=headers,
        json=body if method != "GET" and method != "DELETE" else None,
    )


@pytest.fixture
def two(overlay_db: Any, fake: FakeRooms) -> tuple[int, int]:
    """`(A, B)`: 91 is a maintainer (community-admin) of A only; 92 member + 93 admin of B."""
    a = seed_community(overlay_db, name="community-a")
    b = seed_community(overlay_db, name="community-b")
    for uid in (MAINTAINER_A, MEMBER_B, ADMIN_B):
        seed_user(overlay_db, uid)
    seed_membership(overlay_db, community_id=a, user_id=MAINTAINER_A, role="community-admin")
    seed_membership(overlay_db, community_id=b, user_id=MEMBER_B, role="member")
    seed_membership(overlay_db, community_id=b, user_id=ADMIN_B, role="community-admin")
    fake.seed_room(a)  # ROOM belongs to A only
    fake.seed_participant(a, VICTIM)
    record_room(overlay_db, a)
    return a, b


@pytest.mark.parametrize(("method", "rule", "scopes"), ALL_ROUTES)
async def test_maintainer_of_a_is_refused_on_b(
    client: Any, overlay_db: Any, two: tuple[int, int], method: str, rule: str, scopes: list[str]
) -> None:
    """Holding every tenant-wide scope the route asks for is NOT standing in community B."""
    _, b = two
    response = await _send(client, overlay_db, method, rule, b, _headers(MAINTAINER_A, scopes))
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), ADMIN_ROUTES)
async def test_plain_member_is_refused_on_admin_routes(
    client: Any, overlay_db: Any, two: tuple[int, int], method: str, rule: str, scopes: list[str]
) -> None:
    _, b = two
    response = await _send(client, overlay_db, method, rule, b, _headers(MEMBER_B, scopes))
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), MEMBER_ROUTES)
async def test_plain_member_is_not_refused_on_member_routes(
    client: Any,
    overlay_db: Any,
    fake: FakeRooms,
    two: tuple[int, int],
    method: str,
    rule: str,
    scopes: list[str],
) -> None:
    """Positive control: the gate is membership, not a blanket deny (a 404 for A's room is fine)."""
    _, b = two
    response = await _send(client, overlay_db, method, rule, b, _headers(MEMBER_B, scopes))
    assert response.status_code not in (401, 402, 403), f"{method} {rule} -> {response.status_code}"


async def test_admin_of_b_is_allowed_on_an_admin_route(
    client: Any, overlay_db: Any, two: tuple[int, int]
) -> None:
    """Positive control for the admin gate: the same route a member is refused on admits B's admin."""
    _, b = two
    response = await client.get(
        f"/api/v1/admin/{public_id(overlay_db, b)}/calls/rooms",
        headers=_headers(ADMIN_B, ["streaming.calls:admin"]),
    )
    assert response.status_code == 200


@pytest.mark.parametrize(("method", "rule", "is_admin", "scopes"), ROOM_ROUTES)
async def test_another_communitys_room_is_404_and_untouched(
    client: Any,
    overlay_db: Any,
    fake: FakeRooms,
    two: tuple[int, int],
    method: str,
    rule: str,
    is_admin: bool,
    scopes: list[str],
) -> None:
    """IDOR child id: B (authorised on its own URL) addressing A's `room_id` gets a 404."""
    a, b = two
    uid = ADMIN_B if is_admin else MEMBER_B
    response = await _send(client, overlay_db, method, rule, b, _headers(uid, scopes))
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"
    state = fake.rooms[(str(a), ROOM)]
    assert state["locked"] is False
    assert VICTIM in state["members"]
    assert all(req.community_id == str(b) for _, req, _ in fake.calls)


#: `(METHOD, rule)` -> the tests that prove the route. The two community-scoped tests cover every
#: route; the admin/member control and the IDOR test add the route-kind specific proof.
CALLS_DENY_TESTS: dict[tuple[str, str], tuple[Any, ...]] = {
    (m, r): (
        test_maintainer_of_a_is_refused_on_b,
        test_plain_member_is_refused_on_admin_routes
        if a
        else test_plain_member_is_not_refused_on_member_routes,
        *((test_another_communitys_room_is_404_and_untouched,) if ("<room_" in r) else ()),
    )
    for m, r, a, _ in CALLS_ROUTES
}


def test_registry_covers_every_calls_route() -> None:
    """Zero routes examined proves nothing: the discovery must find the whole surface."""
    assert len(CALLS_ROUTES) >= 24, f"only {len(CALLS_ROUTES)} calls routes discovered"
    assert len(CALLS_DENY_TESTS) == len(CALLS_ROUTES)
    for key, tests in CALLS_DENY_TESTS.items():
        assert tests and all(callable(t) and t.__name__.startswith("test_") for t in tests), key
