"""Auth-bypass sweep -- every Community-module (M6) route rejects an unauthenticated caller.

Fail-first proof (executed, not narrated): temporarily removed the
`@tenant_middleware` decorator from `community_chat.py::chat_history`.
This test (parametrized across every registered route, `/api/v1/community/
<id>/chat/history` included) went red for that one route -- `404` (no
matching rule with tenant enforcement bypassed... actually: the handler
ran unauthenticated and returned `200`/`404` depending on `community_id`
resolution, not the expected `401`) instead of `401`. Every other
parametrized case was unaffected. Reverted, all green again. See PR
report for the exact before/after run.

This complements the per-group test files (`test_community_*.py`), which
cover scope enforcement and success-path response shape per group --
this file's job is exhaustive, mechanical `401`-without-a-token coverage
across the full route surface, generated from the real `url_map` rather
than hand-enumerated (so a new route in one of these blueprints is
covered automatically, not only when someone remembers to add a test).
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceJwtIssuer, SigningKey
from quart import Quart
from quart_schema import QuartSchema

from blueprints import register_blueprints

# The login stub (M1 placeholder, migration plan phase M1) is the one
# route in the whole v1/v2 surface deliberately unauthenticated -- login
# always is. Every other route, including this module's internal
# (X-Service-Key-only) endpoints, rejects an unauthenticated caller with
# 401 (missing JWT for tenant routes, missing/invalid service key for
# internal routes -- both surface as 401, so one assertion covers both).
#
# `GET /public/communities/<id>/profile` (community_profile.py) and
# `GET /public/communities/<id>` (public.py's own `get_community`, M3
# Platform-admin/Public group) are the two Core-tenancy routes that are
# ALSO deliberately pre-auth -- byte-faithful ports of Node's own
# `routes/public.js`, no `@tenant_middleware` by design (see each
# handler's own docstring/comment). This app-fixture (no `async_dal` in
# `app.config`, only `dal`) can't actually serve either, but that's an
# artifact of this exhaustive sweep's minimal fixture, not a missing auth
# check -- the per-group `test_v1_community_profile_blueprint.py` /
# `test_v1_platform_blueprint.py` cover each route's real (200) success
# path. (`get_community`'s endpoint name is the only other `public.py`
# handler this sweep's `"community" in rule.endpoint` filter matches --
# `list_communities`/`get_spotlighted_communities` don't, since
# "community" (singular) isn't a substring of "communities".)
#
# `GET /connections/callback/<provider>` (community_connections.py, #320) is
# the OAuth consent landing page the *provider* redirects the admin's browser
# to -- it can never carry our JWT. It is deliberately pre-auth: the only
# input it trusts is a single-use, TTL-bound `state` minted by the
# authenticated `/authorize` route (CSRF binding), it stores nothing without
# a valid state, and its HTML response never contains token material. An
# unauthenticated hit therefore returns 400 (bad/missing state), not 401.
_EXEMPT_PATHS = {
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/public/communities/1/profile"),
    ("GET", "/api/v1/public/communities/1"),
    ("GET", "/api/v1/connections/callback/<provider>"),
}


@pytest.fixture
def app(tenant_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    register_blueprints(quart_app)
    quart_app.config["dal"] = tenant_db
    # A configured (real Ed25519) machine-JWT verifier: the connection token route no longer has an
    # X-Service-Key path, and with NO verifier it answers 503 (a deliberate fail-closed deployment
    # fault, covered in tests/test_machine_auth_bound.py). The sweep asserts the *unauthenticated*
    # outcome, which only exists once service auth is configured.
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    quart_app.config["SERVICE_JWT_VERIFIER"] = ServiceJwtIssuer(
        keys={"k1": key}, active_kid="k1", identities={}, audience="waddlebot-internal"
    ).as_verifier()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _community_routes(app: Quart) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for rule in app.url_map.iter_rules():
        if "community" not in rule.endpoint:
            continue
        path = rule.rule.replace("<int:community_id>", "1").replace("<int:channel_id>", "1")
        path = path.replace("<int:poll_id>", "1").replace("<int:form_id>", "1")
        path = path.replace("<int:announcement_id>", "1").replace("<int:item_id>", "1")
        path = path.replace("<int:giveaway_id>", "1").replace("<int:user_id>", "1")
        path = path.replace("<int:role_id>", "1").replace("<int:post_id>", "1")
        path = path.replace("<int:reply_id>", "1").replace("<event_type>", "raffle_start")
        path = path.replace("<int:queue_id>", "1")
        # v1_community_tokens (support/access-token port group) -- CAT routes.
        path = path.replace("<int:token_id>", "1")
        for method in rule.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, path) in _EXEMPT_PATHS:
                continue
            routes.append((method, path))
    return sorted(set(routes))


def _discovery_app() -> Quart:
    """Throwaway app used only to enumerate routes at collection time (no DAL, never served)."""
    discovery_app = Quart(__name__)
    QuartSchema(discovery_app)
    register_blueprints(discovery_app)
    return discovery_app


_ROUTES = _community_routes(_discovery_app())


def test_route_inventory_is_nonempty() -> None:
    """Denominator check (critical-rules.md Verification Integrity).

    The sweep below must actually examine routes, not silently iterate zero.
    """
    assert len(_ROUTES) >= 80, (
        f"expected the full ~88-route Community-module surface, got {len(_ROUTES)}"
    )


@pytest.mark.parametrize("method,path", _ROUTES)
async def test_rejects_unauthenticated_request(client: Any, method: str, path: str) -> None:
    """Every route requires either a bearer token (tenant JWT) or an X-Service-Key."""
    response = await client.open(path, method=method, json={})
    assert response.status_code == 401, f"{method} {path} -> {response.status_code}, want 401"
