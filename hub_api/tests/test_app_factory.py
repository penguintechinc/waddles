"""`app.create_app()` boots end-to-end -- DAL init, health, MCP, v1/v2 routers, OpenAPI.

Uses `async with app.test_app():` so Quart actually runs the
`before_serving`/`after_serving` hooks (`app.py::startup`/`shutdown`),
not just registers routes -- the same hooks hypercorn triggers in
production. `sqlite:memory` (matching `tests/conftest.py::tenant_db` and
every `libs/flask_core` DB fixture) stands in for the real Postgres
`DATABASE_URL` so this test has no external dependency.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from quart import Quart

from app import bridge_session_cookie_to_bearer, create_app
from config import HubAPIConfig
from services.bundle_version_service import BUNDLE_MAX_REQUEST_BYTES


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app() -> Quart:
    return create_app(_test_config())


class TestAppFactoryBoots:
    async def test_startup_and_shutdown_hooks_run_cleanly(self, app: Quart) -> None:
        async with app.test_app():
            client = app.test_client()
            response = await client.get("/health")
            assert response.status_code == 200

    async def test_dal_is_bound_after_startup(self, app: Quart) -> None:
        async with app.test_app():
            assert app.config.get("dal") is not None
            assert app.config.get("async_dal") is not None

    def test_max_content_length_is_capped(self, app: Quart) -> None:
        """The cap is explicit and tied to this app's real ceiling, not Quart's 16 MiB default.

        Quart's own built-in default sits exactly at
        `BUNDLE_MAX_SOURCE_BYTES`, which would silently reject any
        legitimate 32 MiB `component` upload before it ever reached this
        app's own per-part checks.
        """
        assert app.config["MAX_CONTENT_LENGTH"] == BUNDLE_MAX_REQUEST_BYTES


class TestHealthEndpoint:
    async def test_health_returns_200_and_module_name(self, app: Quart) -> None:
        async with app.test_app():
            client = app.test_client()
            response = await client.get("/health")
            assert response.status_code == 200
            body: dict[str, Any] = await response.get_json()
            assert body["module"] == "hub-api-test"
            assert body["status"] == "healthy"

    async def test_healthz_returns_200(self, app: Quart) -> None:
        """Assert `/healthz` returns 200 with mocked, deterministic `psutil` readings.

        `/healthz` (`flask_core.api_utils`) derives from real host CPU/memory via
        `psutil` -- mock both so this assertion is deterministic regardless of host
        load. Without this, `psutil.cpu_percent` reading a transient spike (e.g. a
        heavily loaded shared dev/CI host with unrelated concurrent processes) trips
        the >95% threshold and flips this test to a flaky, intermittent 503 with no
        relation to hub-api's own health. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 10.0
            async with app.test_app():
                client = app.test_client()
                response = await client.get("/healthz")
                assert response.status_code == 200

    async def test_healthz_reports_503_when_resources_degraded(self, app: Quart) -> None:
        """Regression: the degraded-resource branch of `/healthz` must still 503.

        Pinned via mocked `psutil` (independent of real host state) so this can't
        silently bitrot into an always-200 endpoint while `test_healthz_returns_200`
        above is also mocked healthy. regression: gh-314
        """
        with (
            patch("flask_core.api_utils.psutil.virtual_memory") as mock_vmem,
            patch("flask_core.api_utils.psutil.cpu_percent", return_value=10.0),
        ):
            mock_vmem.return_value.percent = 95.0
            async with app.test_app():
                client = app.test_client()
                response = await client.get("/healthz")
                assert response.status_code == 503
                body: dict[str, Any] = await response.get_json()
                assert body["status"] == "degraded"


class TestVersionedRoutersMounted:
    async def test_v1_login_reachable(self, app: Quart) -> None:
        """v1 frozen router mounted -- the real M1 auth group answers (400), not 404.

        400 comes from quart-schema's @validate_request rejecting the empty
        body. Was a 501 placeholder before M1 (Core Identity/Auth) replaced
        the stub -- see `tests/test_v1_auth_blueprint.py` for the full suite.
        """
        async with app.test_app():
            client = app.test_client()
            response = await client.post("/api/v1/auth/login", json={})
            assert response.status_code == 400

    async def test_v2_platform_example_reachable_but_gated(self, app: Quart) -> None:
        """v2 additive router mounted -- reaches tenant_middleware (401), not 404."""
        async with app.test_app():
            client = app.test_client()
            response = await client.get("/api/v2/core/platform/default/status")
            assert response.status_code == 401


class TestSessionCookieBridgeWiredIntoRealApp:
    """security.md C4 fix -- `app.py`'s cookie->bearer bridge is actually mounted.

    The full round-trip proof (login sets the cookie, a *second* request
    carrying only that cookie -- no `Authorization` header anywhere --
    reaches an authenticated route) lives in `tests/test_v1_auth_blueprint.py::
    TestSessionCookieBridgeEndToEnd`: that file's `auth_db` fixture runs
    real (`migrate=True`) tables, while this file's `create_app()` binds
    with `migrate=False` (production: schema owned by SQL migrations --
    see `services/schema.py::bind_auth_tables`), so a login here has no
    `hub_users` table to actually query against. This class instead
    proves the hook is wired into the real app object `create_app()`
    returns, and exercises the two request paths that never touch the DB
    at all (no auth present; an explicit, invalid bearer header).
    """

    def test_bridge_is_registered_as_an_app_wide_before_request_hook(self, app: Quart) -> None:
        # Quart keys `before_request_funcs` by blueprint name; `None` is
        # the app-wide (no-blueprint) scope `app.before_request()` used.
        hooks = app.before_request_funcs.get(None, [])
        assert bridge_session_cookie_to_bearer in hooks

    async def test_authenticated_request_without_cookie_or_header_is_unauthenticated(
        self, app: Quart
    ) -> None:
        async with app.test_app():
            client = app.test_client()
            response = await client.get("/api/v1/auth/me")
            assert response.status_code == 200
            body = await response.get_json()
            assert body["user"] is None

    async def test_explicit_bearer_header_still_wins_over_no_cookie(self, app: Quart) -> None:
        """The bridge is additive -- an existing Authorization caller is untouched."""
        async with app.test_app():
            client = app.test_client()
            response = await client.post(
                "/api/v1/auth/refresh", headers={"Authorization": "Bearer not-a-real-token"}
            )
            # Rejected on the token's own merits (invalid), never silently
            # swapped for a (nonexistent) cookie -- proves header precedence.
            assert response.status_code in (400, 401)
