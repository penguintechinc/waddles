"""Community content write/admin routes need PER-COMMUNITY admin standing (real guards).

announcements, loyalty, raffle-customization and the activity leaderboard-config
were authorized only by a tenant-wide token scope (`community.<x>:write`/`:admin`) plus the tenant
fence. The `maintainer` bundle carries `*:write`, so a maintainer of community A could write to
community B of the same tenant. Every such route now carries `@community_admin_guard`; all 16 are
admin-only (definitions, config, moderation, balance/wipe) -- none is member-facing by design
(member actions such as voting, submitting a form or earning loyalty points go through the
service-key `/internal` routes and core-engagement, not these admin routes).

These tests run the REAL guards (no `bypass_route_guards`, no patched standing) against seeded
`community_members` rows:

* a maintainer of A (admin of A only) holding the route's scopes is refused on B;
* a plain member of B holding the route's scopes is refused on B;
* a foreign-tenant community is 404 (same as `route_guards`), never 403;
* the maintainer is NOT refused on A (positive control: the guard is not a blanket deny);
* a child id (announcement) belonging to community B is not reachable through A's URL;
"""

from __future__ import annotations

# ruff: noqa: F811, E501
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_announcements as announcements_module
import blueprints.v1.community_raffle as raffle_module
from blueprints.v1.community_activity import activity_admin_bp
from blueprints.v1.community_announcements import announcements_bp
from blueprints.v1.community_loyalty import loyalty_bp
from blueprints.v1.community_raffle import raffle_bp
from services.community_announcements import create_announcement, get_announcement
from services.community_common import ensure_community_tables
from services.global_identity import ensure_tables as ensure_global_tables
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids, public_id
from tests.conftest import make_user_token, seed_membership
from tests.route_guard_walker import guard_kind, required_scopes

MAINTAINER_A, MEMBER_B = 81, 82
BLUEPRINTS = (
    announcements_bp,
    loyalty_bp,
    raffle_bp,
    activity_admin_bp,
)
_WRITE_SUFFIXES = (":write", ":admin")


def _content_routes() -> list[Any]:
    """`pytest.param(method, rule, scopes, kind)` for every write/admin-scoped community route."""
    probe = Quart(__name__)
    for bp in BLUEPRINTS:
        probe.register_blueprint(bp)
    params: list[Any] = []
    for rule in probe.url_map.iter_rules():
        if "community_id" not in rule.arguments:
            continue
        view = probe.view_functions[rule.endpoint]
        scopes = required_scopes(view)
        if not any(s.endswith(_WRITE_SUFFIXES) for s in scopes):
            continue
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            tail = rule.rule.split("community_id>/")[-1]
            params.append(
                pytest.param(method, rule.rule, scopes, guard_kind(view), id=f"{method}-{tail}")
            )
    return params


ROUTES = _content_routes()
ANNOUNCEMENT_ROUTES = [p for p in ROUTES if "announcement_id" in p.values[1]]


def test_every_content_write_route_is_admin_guarded() -> None:
    """Static proof next to the behavioural one: all 16 routes carry the admin guard."""
    assert len(ROUTES) == 16, f"expected 16 write/admin community routes, found {len(ROUTES)}"
    wrong = [p.id for p in ROUTES if p.values[3] != "admin"]
    assert wrong == [], f"content write routes without the community admin guard: {wrong}"


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feature flags ON so an allowed caller reaches the handler (the guard runs before it)."""
    for module in (announcements_module, raffle_module):
        monkeypatch.setattr(module, "feature_enabled", AsyncMock(return_value=True))


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _ = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def worlds(community_db: Any, app: Quart) -> SimpleNamespace:
    """Communities `a`, `b` (same tenant) and `foreign` (other tenant); 81 admins A, 82 is a member of B."""
    dal, a = community_db
    ensure_global_tables(dal, migrate=True)  # real `tenant_admins` for the standing lookup
    ensure_community_tables(dal, migrate=True)
    tenant = dal(dal.tenants.slug == "acme-corp").select().first()
    b = int(dal.communities.insert(tenant_id=tenant.id))
    other_tenant = dal.tenants.insert(slug="other-corp-authz", is_active=True)
    foreign = int(dal.communities.insert(tenant_id=other_tenant))
    dal.commit()
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=a, user_id=MAINTAINER_A, role="community-admin")
    seed_membership(db, community_id=b, user_id=MEMBER_B, role="member")
    install_community_ids(app, dal)
    for cid in (a, b, foreign):
        public_id(dal, cid)  # mint + commit each community's public UUID
    return SimpleNamespace(dal=dal, a=a, b=b, foreign=foreign)


def _fill(rule: str) -> str:
    return (
        rule.replace("<cuuid:community_id>", "{community_id}")
        .replace("<int:announcement_id>", "{aid}")
        .replace("<int:poll_id>", "1")
        .replace("<int:form_id>", "1")
        .replace("<event_type>", "raffle_start")
        .replace("<uuid:user_uuid>", "33333333-3333-4333-8333-333333333333")
    )


async def _call(
    client: Any,
    w: SimpleNamespace,
    method: str,
    rule: str,
    cid: int,
    uid: int,
    scopes: list[str],
    *,
    aid: int = 1,
) -> Any:
    url = cpath(w.dal, _fill(rule).replace("{aid}", str(aid)), cid)
    token = make_user_token(user_id=uid, scope=" ".join(["*:read", *scopes]))
    body = {
        "platform": "twitch",
        "delta": 1,
        "title": "t",
        "content": "c",
        "platforms": ["discord"],
    }
    kwargs: dict[str, Any] = {}
    if method not in ("GET", "DELETE"):
        kwargs["data"] = json.dumps(body)
    return await client.open(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        **kwargs,
    )


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_maintainer_of_a_is_refused_on_b(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    response = await _call(client, worlds, method, rule, worlds.b, MAINTAINER_A, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_plain_member_is_refused_on_admin_routes(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    response = await _call(client, worlds, method, rule, worlds.b, MEMBER_B, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_foreign_tenant_community_is_404_not_403(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    response = await _call(client, worlds, method, rule, worlds.foreign, MAINTAINER_A, scopes)
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES[:8])
async def test_unknown_community_is_404(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    url = _fill(rule).replace("{community_id}", UNKNOWN_PUBLIC_ID).replace("{aid}", "1")
    token = make_user_token(user_id=MAINTAINER_A, scope=" ".join(["*:read", *scopes]))
    response = await client.open(
        url, method=method, headers={"Authorization": f"Bearer {token}"}, data="{}"
    )
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ROUTES)
async def test_maintainer_of_a_is_not_blanket_denied_on_a(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    response = await _call(client, worlds, method, rule, worlds.a, MAINTAINER_A, scopes)
    assert response.status_code not in (401, 403), f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), ANNOUNCEMENT_ROUTES)
async def test_announcement_of_b_is_not_reachable_through_a(
    client: Any, worlds: SimpleNamespace, method: str, rule: str, scopes: list[str], kind: str
) -> None:
    """IDOR: admin of A + a REAL announcement id that belongs to B -> 404, B's row untouched."""
    dto, err = create_announcement(
        worlds.dal,
        worlds.b,
        {"title": "b only", "content": "secret", "status": "published"},
        1,
        "x",
    )
    assert err is None and dto is not None
    before = get_announcement(worlds.dal, worlds.b, dto.id)
    response = await _call(
        client, worlds, method, rule, worlds.a, MAINTAINER_A, scopes, aid=int(dto.id)
    )
    assert response.status_code == 404, f"{method} {rule} -> {response.status_code}"
    assert get_announcement(worlds.dal, worlds.b, dto.id) == before


async def test_raffle_delete_only_touches_the_url_community(
    client: Any, worlds: SimpleNamespace
) -> None:
    """IDOR: A's admin resetting `raffle_start` must not delete B's customization."""
    dal = worlds.dal
    dal.community_raffle_sounds.insert(
        community_id=worlds.b, event_type="raffle_start", message_template="b-owned"
    )
    dal.commit()
    response = await _call(
        client,
        worlds,
        "DELETE",
        "/api/v1/admin/<cuuid:community_id>/raffle-customization/<event_type>",
        worlds.a,
        MAINTAINER_A,
        ["community.raffle:write"],
    )
    assert response.status_code == 200
    assert dal(dal.community_raffle_sounds.community_id == worlds.b).count() == 1
