"""Every community-scoped bot route (shoutout, ai-chatter, RCON) needs community admin standing.

The route scopes (`bot.shoutout:write`, `bot.server_manager:admin`, ...) are TENANT-WIDE token
scopes, so on their own they let a maintainer of community A run RCON on, or rewrite the shoutout
config of, community B in the same tenant. Each route now carries `@community_admin_guard`
(or the member guard for the public RCON info routes). These tests run the REAL guards (no
`bypass_route_guards`, no patched standing) against seeded `community_members` rows:

* a maintainer of A (admin of A only) holding the route's scopes is refused on B;
* a plain member of B holding the route's scopes is refused on B;
* the same maintainer is NOT refused on A (positive control: the guard is not a blanket deny).
"""

from __future__ import annotations

# ruff: noqa: F811, E501
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart

from blueprints.v1.bot import bot_bp
from services.community_common import ensure_community_tables
from services.global_identity import ensure_tables as ensure_global_tables
from tests.community_ids import cpath, install_community_ids
from tests.conftest import make_user_token, seed_membership
from tests.route_guard_walker import guard_kind, required_scopes
from tests.test_bot_blueprint import app, client, community_id, dal  # noqa: F401 - fixtures

MAINTAINER_A, MEMBER_B = 71, 72


def _bot_routes() -> list[Any]:
    """`pytest.param(method, rule, scopes)` for every `<community_id>` route of the bot blueprint."""
    probe = Quart(__name__)
    probe.register_blueprint(bot_bp)
    params: list[Any] = []
    for rule in probe.url_map.iter_rules():
        if "community_id" not in rule.arguments:
            continue
        view = probe.view_functions[rule.endpoint]
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            params.append(
                pytest.param(
                    method,
                    rule.rule,
                    required_scopes(view),
                    guard_kind(view),
                    id=f"{method}-{rule.rule.split('community_id>/')[-1]}",
                )
            )
    return params


BOT_ROUTES = _bot_routes()


def test_every_bot_community_route_is_guarded() -> None:
    """Static proof alongside the behavioural one: no bot community route lacks a guard."""
    assert len(BOT_ROUTES) >= 25, f"only {len(BOT_ROUTES)} bot community routes discovered"
    unguarded = [p.id for p in BOT_ROUTES if p.values[3] is None]
    assert unguarded == [], f"bot community routes with no route guard: {unguarded}"


@pytest.fixture
def two_communities(dal: Any, community_id: int, app: Quart) -> tuple[int, int]:
    """`(A, B)`: two communities of one tenant; 71 administers A only, 72 is a plain member of B."""
    ensure_global_tables(dal, migrate=True)  # real `tenant_admins` for the standing lookup
    ensure_community_tables(dal, migrate=True)
    tenant_row = dal(dal.tenants.slug == "acme-corp").select().first()
    b = int(dal.communities.insert(tenant_id=tenant_row.id))
    dal.commit()
    db = SimpleNamespace(dal=dal)
    seed_membership(db, community_id=community_id, user_id=MAINTAINER_A, role="community-admin")
    seed_membership(db, community_id=b, user_id=MEMBER_B, role="member")
    install_community_ids(app, dal)
    return community_id, b


def _call(
    client: Any, method: str, rule: str, dal: Any, cid: int, uid: int, scopes: list[str]
) -> Any:
    url = cpath(
        dal,
        rule.replace("<cuuid:community_id>", "{community_id}")
        .replace("<int:server_id>", "1")
        .replace("<int:creator_id>", "1"),
        cid,
    )
    token = make_user_token(user_id=uid, scope=" ".join(["*:read", *scopes]))
    return client.open(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
        json={} if method not in ("GET", "DELETE") else None,
    )


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), BOT_ROUTES)
async def test_maintainer_of_a_is_refused_on_b(
    client: Any,
    dal: Any,
    two_communities: tuple[int, int],
    method: str,
    rule: str,
    scopes: list[str],
    kind: str | None,
) -> None:
    _, b = two_communities
    response = await _call(client, method, rule, dal, b, MAINTAINER_A, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), BOT_ROUTES)
async def test_plain_member_is_refused_on_admin_routes(
    client: Any,
    dal: Any,
    two_communities: tuple[int, int],
    method: str,
    rule: str,
    scopes: list[str],
    kind: str | None,
) -> None:
    if kind == "member":
        pytest.skip("member-guarded read route: a member is allowed by design")
    _, b = two_communities
    response = await _call(client, method, rule, dal, b, MEMBER_B, scopes)
    assert response.status_code == 403, f"{method} {rule} -> {response.status_code}"


@pytest.mark.parametrize(("method", "rule", "scopes", "kind"), BOT_ROUTES[:6])
async def test_maintainer_of_a_is_not_blanket_denied_on_a(
    client: Any,
    dal: Any,
    two_communities: tuple[int, int],
    method: str,
    rule: str,
    scopes: list[str],
    kind: str | None,
) -> None:
    a, _ = two_communities
    response = await _call(client, method, rule, dal, a, MAINTAINER_A, scopes)
    assert response.status_code != 403, f"{method} {rule} -> {response.status_code}"
