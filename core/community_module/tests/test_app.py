"""community_module Quart application tests.

Covers the module's actual surface: the shared health/metrics blueprint
(`flask_core.create_health_blueprint`), the single `/api/v1/status`
endpoint, the app-wide security-header and rate-limiting installs, startup
DB wiring, and standard Quart error handling (404/405) -- mirroring
`test-api.sh`'s existing coverage of the same endpoints, now runnable in
CI via pytest instead of only against a live server.
"""

from __future__ import annotations

from typing import Any


class TestHealthBlueprint:
    """The shared `flask_core.create_health_blueprint` endpoints."""

    async def test_health_returns_200_and_module_name(self, client: Any) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "healthy"
        assert body["module"] == "community_module"
        assert body["version"] == "2.0.0"

    async def test_healthz_returns_checks_payload(self, client: Any) -> None:
        # 200 (healthy) or 503 (degraded) are both legitimate outcomes here --
        # `create_health_blueprint`'s own docstring notes the CPU check is
        # exposed to transient host scheduling noise under concurrent CI
        # load, so this asserts the response shape rather than pinning one
        # status code and risking exactly that flake.
        response = await client.get("/healthz")
        assert response.status_code in (200, 503)
        body = await response.get_json()
        assert body["status"] in ("healthy", "degraded")
        assert "memory" in body["checks"]
        assert "cpu" in body["checks"]

    async def test_metrics_returns_prometheus_text(self, client: Any) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        body = (await response.get_data()).decode()
        assert 'module="community_module"' in body
        assert "waddlebot_requests_total" in body


class TestStatusEndpoint:
    """`/api/v1/status` -- the module's only business-logic route today."""

    async def test_status_returns_operational(self, client: Any) -> None:
        response = await client.get("/api/v1/status")
        assert response.status_code == 200
        body = await response.get_json()
        assert body["success"] is True
        assert body["data"]["status"] == "operational"
        assert body["data"]["module"] == "community_module"
        assert "timestamp" in body

    async def test_status_rejects_unsupported_method(self, client: Any) -> None:
        response = await client.delete("/api/v1/status")
        assert response.status_code == 405


class TestErrorHandling:
    """Standard Quart 404 behavior for unregistered routes."""

    async def test_unknown_endpoint_returns_404(self, client: Any) -> None:
        response = await client.get("/api/v1/nonexistent")
        assert response.status_code == 404


class TestSecurityHeaders:
    """`install_security_headers(app)` -- security.md A05, JSON-only default-deny CSP."""

    async def test_response_carries_security_headers(self, client: Any) -> None:
        response = await client.get("/api/v1/status")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "no-referrer"
        assert "Content-Security-Policy" in response.headers


class TestStartupWiring:
    """`startup()` -- database + rate limiter get bound onto `app.config`."""

    async def test_dal_is_bound_on_app_config(self, app_and_client: tuple[Any, Any]) -> None:
        app_module, _ = app_and_client
        assert app_module.app.config["dal"] is not None
        assert app_module.dal is app_module.app.config["dal"]

    async def test_rate_limiter_is_installed(self, app_and_client: tuple[Any, Any]) -> None:
        app_module, _ = app_and_client
        assert app_module.app.config.get("rate_limiter") is not None
