"""One real-guard deny test per blueprint family that decorates routes with a route guard.

Many legacy blueprint test modules run under `bypass_route_guards` (admits every caller as an
admin), so their own suites can never fail on a missing guard. This module discovers every
blueprint module in the real app that carries a `community_admin_guard` / `community_member_guard`
route, then sends a token holding exactly the route's declared scopes from a caller who lacks the
community standing (plain member for admin routes, outsider for member routes) and asserts 403
with the REAL guards active (no bypass fixture, no patched standing).
"""

from __future__ import annotations

# ruff: noqa: E501
from types import SimpleNamespace
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from app import create_app
from services.route_guards import GUARD_ADMIN, GUARD_MEMBER
from tests.community_ids import cpath, install_community_ids
from tests.conftest import make_user_token, seed_membership
from tests.route_guard_walker import chain, guard_kind, is_inline_stamped
from tests.test_app_factory import _test_config

MEMBER_UID, OUTSIDER_UID = 52, 53


def _required_scopes(view: Any) -> list[str]:
    """Scopes passed to `require_scope(...)` in the view's wrapper chain."""
    scopes: list[str] = []
    for fn in chain(view):
        code = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None)
        if code is not None and cells and "required_scopes" in code.co_freevars:
            scopes.extend(cells[code.co_freevars.index("required_scopes")].cell_contents)
    return scopes


def _discover_families() -> list[Any]:
    """`pytest.param`s: one route per blueprint module that has a real (decorator) route guard."""
    application = create_app(_test_config())
    picked: dict[str, tuple[str, str, str, list[str], Any]] = {}
    for rule in application.url_map.iter_rules():
        if set(rule.arguments) != {"community_id"}:
            continue
        view = application.view_functions[rule.endpoint]
        kind = guard_kind(view)
        if kind not in (GUARD_ADMIN, GUARD_MEMBER) or is_inline_stamped(view):
            continue
        methods = sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})
        method = "GET" if "GET" in methods else methods[0]
        current = picked.get(view.__module__)
        if current is None or (method == "GET" and current[1] != "GET"):
            bp_name = rule.endpoint.rsplit(".", 1)[0]
            picked[view.__module__] = (
                bp_name,
                method,
                rule.rule,
                _required_scopes(view),
                (kind, application.blueprints[bp_name]),
            )
    return [
        pytest.param(module, *info, id=module.rsplit(".", 1)[-1])
        for module, info in sorted(picked.items())
    ]


FAMILIES = _discover_families()


def test_every_guarded_family_is_examined() -> None:
    """A discovery pointed at the wrong place would parametrize nothing and pass vacuously."""
    assert len(FAMILIES) >= 12, f"only {len(FAMILIES)} guarded blueprint families discovered"


@pytest.fixture
def guarded_app(community_db: Any) -> tuple[Quart, Any, int]:
    """Plain Quart app over the real community schema with a member seeded (NOT the outsider)."""
    dal, community_id = community_db
    seed_membership(
        SimpleNamespace(dal=dal), community_id=community_id, user_id=MEMBER_UID, role="member"
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    return quart_app, dal, community_id


@pytest.mark.parametrize(("module", "bp_name", "method", "rule", "scopes", "kind_bp"), FAMILIES)
async def test_real_guard_refuses_caller_without_standing(
    guarded_app: tuple[Quart, Any, int],
    module: str,
    bp_name: str,
    method: str,
    rule: str,
    scopes: list[str],
    kind_bp: tuple[str, Any],
) -> None:
    quart_app, dal, community_id = guarded_app
    kind, blueprint = kind_bp
    quart_app.register_blueprint(blueprint)
    # Admin routes: a plain member must be refused. Member routes: a non-member must be.
    uid = MEMBER_UID if kind == GUARD_ADMIN else OUTSIDER_UID
    token = make_user_token(user_id=uid, scope=" ".join(["*:read", *scopes]))
    url = cpath(dal, rule.replace("<cuuid:community_id>", "{community_id}"), community_id)
    response = await quart_app.test_client().open(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}"},
        json={} if method not in ("GET", "DELETE") else None,
    )
    assert response.status_code == 403, f"{module} {method} {rule} -> {response.status_code}"
