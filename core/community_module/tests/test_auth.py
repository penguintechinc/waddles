"""community_module scoped-auth wiring tests.

Covers the `install_community_scoped_auth` fix wired onto `api_bp` in
`app.py`. Previously `/api/v1/status` was this blueprint's only route and
happened to need no protection, so `api_bp` shipped with ZERO
tenant/community-membership enforcement -- the next CRUD route added would
have shipped unauthenticated by omission. This proves the currently-public
routes stay public, and that a stand-in protected route -- registered only
in this file's fixture, never in `app.py` itself -- is rejected without a
bearer token, i.e. the `before_request` hook is actually wired onto
`api_bp` and covers any future route on it, not just `/status`.

Fail-first proof: with `install_community_scoped_auth(api_bp, ...)`
(app.py's module-level call) commented out,
`test_protected_route_rejects_missing_token` goes green->red as expected
(200 instead of 401); the public-route tests stay green either way, which
is exactly the gap this fix closes.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

SECRET_KEY = "change-me-in-production"


@pytest_asyncio.fixture
async def protected_route_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """Same app as `app_and_client`, plus one throwaway protected `api_bp` route.

    Registered directly on `app` (not via `api_bp.route(...)`, which is a
    no-op once a blueprint is already registered) using an `api.`-prefixed
    endpoint name -- Quart/Flask resolve `request.blueprint` from the
    matched endpoint's dotted prefix at dispatch time, not from which
    object originally added the rule, so `api_bp`'s `before_request` hook
    (installed by `install_community_scoped_auth`) still runs for it. This
    is intentionally NOT added to `app.py` itself -- it exists only to
    prove the hook covers routes it has never seen, not to become a real
    endpoint.
    """
    db_path = tmp_path / "community_module_test_auth.db"
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite://{db_path}")

    for mod_name in ("app", "config"):
        sys.modules.pop(mod_name, None)

    import app as app_module

    async def _protected() -> tuple[dict[str, bool], int]:
        return {"ok": True}, 200

    app_module.app.add_url_rule(
        "/api/v1/_test-protected",
        endpoint="api._test_protected",
        view_func=_protected,
        methods=["GET"],
    )

    async with app_module.app.test_app() as running:
        yield running.test_client()


class TestPublicRoutesStayPublic:
    """`install_community_scoped_auth`'s `exempt_paths` -- no bearer token required."""

    async def test_status_is_public(self, client: Any) -> None:
        response = await client.get("/api/v1/status")
        assert response.status_code == 200

    async def test_health_is_public(self, client: Any) -> None:
        response = await client.get("/health")
        assert response.status_code == 200

    async def test_healthz_is_public(self, client: Any) -> None:
        # 200 (healthy) or 503 (degraded) both legitimate -- see
        # test_app.py::TestHealthBlueprint's identical note; the point
        # here is "no 401", not the CPU-check outcome.
        response = await client.get("/healthz")
        assert response.status_code in (200, 503)

    async def test_metrics_is_public(self, client: Any) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200


class TestNonPublicRouteRequiresToken:
    """A hypothetical future `api_bp` route -- proves the hook covers it, not just `/status`."""

    async def test_protected_route_rejects_missing_token(
        self, protected_route_client: Any
    ) -> None:
        response = await protected_route_client.get("/api/v1/_test-protected")
        assert response.status_code == 401
