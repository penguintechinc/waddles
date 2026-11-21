"""Per-community standing on connections, interaction member-writes and support routes.

Sweep B (the calls routes are proven in `test_calls_authz_sweep.py`).

The route scopes (`community.connections:write`, `community.interaction:write`,
`community.support:admin`) are TENANT-WIDE token scopes and the `maintainer` bundle carries
`*:write`, so on their own they let a maintainer of community A act in community B of the same
tenant. Each route now carries `@community_admin_guard` (admin) or `@community_member_guard`
(member). These tests run the REAL guards (no `bypass_route_guards`, no patched standing) against
seeded `community_members` rows, for EVERY route of the three blueprints:

* a maintainer of A (admin of A only, holding the route's scopes + `*:write`) is refused on B;
* a plain member of B is refused on every admin route and is NOT refused on member routes;
* a child id (forum channel/post, ticket, category) of community A addressed through B's own URL
  is a 404 and nothing is written.

`SWEEP_B_DENY_TESTS` is the keyed registry `(METHOD, rule) -> tests`; the coverage test fails for
a route with no entry or a route that lost its guard stamp.
"""

from __future__ import annotations

# ruff: noqa: E501, F811, F401
import json as json_module
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.community_connections import connections_bp
from blueprints.v1.community_interaction import interaction_member_bp
from blueprints.v1.support import support_admin_bp, support_member_bp
from services import support_service as svc
from services.community_common import ensure_community_tables
from services.global_identity import ensure_tables as ensure_global_tables
from tests.community_ids import install_community_ids, public_id
from tests.conftest import make_user_token, seed_membership
from tests.route_guard_walker import guard_kind, required_scopes

MAINTAINER_A, MEMBER_B, ADMIN_B = 71, 72, 73
BLUEPRINTS = (connections_bp, interaction_member_bp, support_admin_bp, support_member_bp)
_CHILD_SEGMENTS = ("<int:ticket_id>", "<int:category_id>", "<int:post_id>", "<int:channel_id>")

_BODIES: dict[str, dict[str, Any]] = {
    "POST support/categories": {"name": "c"},
    "PUT support/categories": {"name": "c"},
    "PUT support/tickets/status": {"status": "open"},
    "PUT support/tickets/assign": {},
    "PUT support/tickets/priority": {"priority": "high"},
    "POST support/tickets/comments": {"content": "x"},
    "POST support/submit": {"subject": "s"},
    "POST support/my-tickets/comments": {"content": "x"},
    "POST interact/channels": {"name": "ch"},
    "POST interact/forum/posts": {"title": "t", "body": "b"},
    "POST interact/forum/posts/replies": {"content": "r"},
}


def _body_key(method: str, rule: str) -> str:
    """`METHOD tail` with the id segments dropped, e.g. `PUT support/tickets/status`."""
    tail = rule.split("community_id>/", 1)[-1]
    parts = [p for p in tail.split("/") if not p.startswith("<")]
    return f"{method} {'/'.join(parts)}"


def _routes() -> list[tuple[str, str, bool, list[str]]]:
    """`(METHOD, rule, is_admin, scopes)` for the in-scope routes of the four blueprints.

    Member READ routes (`GET .../interact/...`) were guarded earlier and are proven by
    `test_read_route_authz.py`; they are not part of this sweep.
    """
    probe = Quart(__name__)
    for bp in BLUEPRINTS:
        probe.register_blueprint(bp)
    out = []
    for rule in probe.url_map.iter_rules():
        if "community_id" not in rule.arguments:
            continue
        view = probe.view_functions[rule.endpoint]
        scopes = required_scopes(view)
        is_support = "/support/" in rule.rule
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            if "/interact/" in rule.rule and method == "GET":
                continue
            is_admin = "connections" in rule.rule or (
                is_support and any(s.endswith(":admin") for s in scopes)
            )
            out.append((method, rule.rule, is_admin, scopes))
    return sorted(out)


ROUTES = _routes()
ADMIN = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in ROUTES if a]
MEMBER = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in ROUTES if not a]
ALL = [pytest.param(m, r, s, id=f"{m} {r}") for m, r, a, s in ROUTES]
CHILD = [p for p in ALL if any(seg in p.values[1] for seg in _CHILD_SEGMENTS)]


@pytest.fixture
def world(support_token_db: Any) -> SimpleNamespace:
    """Two communities of one tenant, real roles, plus community A's channel/post/ticket/category."""
    dal, a = support_token_db
    ensure_global_tables(dal, migrate=True)
    ensure_community_tables(dal, migrate=True)
    tenant = dal(dal.tenants.slug == "acme-corp").select().first()
    b = int(dal.communities.insert(name="community-b", tenant_id=tenant.id))
    dal.commit()
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=a, user_id=MAINTAINER_A, role="community-admin")
    seed_membership(db, community_id=b, user_id=MEMBER_B, role="member")
    seed_membership(db, community_id=b, user_id=ADMIN_B, role="community-admin")
    channel = int(dal.hub_channels.insert(community_id=a, name="a-forum", channel_type="forum"))
    post = int(
        dal.hub_forum_posts.insert(hub_channel_id=channel, community_id=a, title="a", body="a")
    )
    dal.commit()
    category = svc.create_category(
        dal, a, name="a-cat", description=None, sort_order=None, form_fields=[]
    )
    ticket = svc.create_ticket(
        dal,
        a,
        category_id=category.id,
        subject="a-ticket",
        description=None,
        priority=None,
        reporter_user_id=MAINTAINER_A,
        reporter_name=None,
        reporter_email=None,
        custom_fields=None,
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return SimpleNamespace(
        app=quart_app,
        dal=dal,
        a=a,
        b=b,
        channel=channel,
        post=post,
        category=int(category.id),
        ticket=int(ticket.id),
    )


def _url(w: SimpleNamespace, rule: str, community_id: int) -> str:
    """Concrete URL; child ids are community A's rows (the IDOR payload)."""
    values = {
        "channel_id": w.channel,
        "post_id": w.post,
        "reply_id": 1,
        "role_id": 1,
        "category_id": w.category,
        "ticket_id": w.ticket,
    }
    url = rule.replace("<cuuid:community_id>", public_id(w.dal, community_id)).replace(
        "<provider>", "twitch"
    )
    for name, value in values.items():
        url = url.replace(f"<int:{name}>", str(value))
    return url


def _hdr(uid: int, scopes: list[str]) -> dict[str, str]:
    """A maintainer-shaped token: viewer `*:read`, maintainer `*:write`, plus the route's scopes."""
    scope = " ".join(["*:read", "*:write", *scopes])
    return {
        "Authorization": f"Bearer {make_user_token(user_id=uid, scope=scope)}",
        "Content-Type": "application/json",
    }


async def _send(
    w: SimpleNamespace, method: str, rule: str, cid: int, uid: int, scopes: list[str]
) -> Any:
    body = _BODIES.get(_body_key(method, rule))
    send_body = body is not None and method not in ("GET", "DELETE")
    return await w.app.test_client().open(
        _url(w, rule, cid),
        method=method,
        headers=_hdr(uid, scopes),
        data=json_module.dumps(body) if send_body else None,
    )


async def _passed_guard(response: Any) -> bool:
    """True unless the response is the route guard's refusal (a handler 403/404 still passed it)."""
    text = (await response.get_data(as_text=True)).lower()
    refused = response.status_code == 401 or (
        response.status_code == 403 and "access required" in text
    )
    return not refused


@pytest.mark.parametrize(("method", "rule", "scopes"), ALL)
async def test_maintainer_of_a_is_refused_on_b(
    world: SimpleNamespace, method: str, rule: str, scopes: list[str]
) -> None:
    response = await _send(world, method, rule, world.b, MAINTAINER_A, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), ADMIN)
async def test_plain_member_is_refused_on_admin_routes(
    world: SimpleNamespace, method: str, rule: str, scopes: list[str]
) -> None:
    response = await _send(world, method, rule, world.b, MEMBER_B, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), MEMBER)
async def test_plain_member_passes_the_guard_on_member_routes(
    world: SimpleNamespace, method: str, rule: str, scopes: list[str]
) -> None:
    """Positive control: the guard is membership, not a blanket deny."""
    response = await _send(world, method, rule, world.b, MEMBER_B, scopes)
    assert await _passed_guard(response), f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), ADMIN)
async def test_admin_of_b_passes_the_guard_on_admin_routes(
    world: SimpleNamespace, method: str, rule: str, scopes: list[str]
) -> None:
    """Positive control: B's own admin is not refused by the guard."""
    response = await _send(world, method, rule, world.b, ADMIN_B, scopes)
    assert await _passed_guard(response), f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes"), CHILD)
async def test_child_row_of_another_community_is_404(
    world: SimpleNamespace, method: str, rule: str, scopes: list[str]
) -> None:
    """IDOR: B (authorised on its own URL) naming A's ticket/category/channel/post gets a 404."""
    is_admin = any(m == method and r == rule and a for m, r, a, _ in ROUTES)
    response = await _send(world, method, rule, world.b, ADMIN_B if is_admin else MEMBER_B, scopes)
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"
    dal = world.dal
    assert dal(dal.hub_forum_posts.community_id == world.b).count() == 0
    assert dal(dal.hub_forum_replies).count() == 0


async def test_submit_with_another_communitys_category_is_404(world: SimpleNamespace) -> None:
    """The category id is a body field: it must belong to the URL community."""
    response = await world.app.test_client().post(
        _url(world, "/api/v1/admin/<cuuid:community_id>/support/submit", world.b),
        headers=_hdr(MEMBER_B, []),
        data=json_module.dumps({"subject": "s", "category_id": world.category}),
    )
    assert response.status_code == 404
    assert world.dal(world.dal.support_tickets.community_id == world.b).count() == 0


async def test_tenant_wide_support_admin_scope_does_not_open_categories(
    world: SimpleNamespace,
) -> None:
    """`community.support:admin` without standing in B must not read B's category list."""
    response = await world.app.test_client().get(
        _url(world, "/api/v1/admin/<cuuid:community_id>/support/categories", world.b),
        headers=_hdr(MAINTAINER_A, ["community.support:admin"]),
    )
    assert response.status_code == 403


#: `(METHOD, rule)` -> the tests proving the route (see module docstring).
SWEEP_B_DENY_TESTS: dict[tuple[str, str], tuple[Any, ...]] = {
    (m, r): (
        test_maintainer_of_a_is_refused_on_b,
        test_plain_member_is_refused_on_admin_routes
        if a
        else test_plain_member_passes_the_guard_on_member_routes,
        *(
            (test_child_row_of_another_community_is_404,)
            if any(s in r for s in _CHILD_SEGMENTS)
            else ()
        ),
    )
    for m, r, a, _ in ROUTES
}


def test_registry_covers_every_route_and_each_is_guarded() -> None:
    """Zero routes examined proves nothing; every route must carry the matching guard stamp."""
    assert len(ROUTES) >= 18, f"only {len(ROUTES)} routes discovered"
    probe = Quart(__name__)
    for bp in BLUEPRINTS:
        probe.register_blueprint(bp)
    stamped = {
        (m, e.rule): guard_kind(probe.view_functions[e.endpoint])
        for e in probe.url_map.iter_rules()
        for m in (e.methods or set())
    }
    for method, rule, is_admin, _ in ROUTES:
        assert stamped[(method, rule)] == ("admin" if is_admin else "member"), (method, rule)
        assert (method, rule) in SWEEP_B_DENY_TESTS
