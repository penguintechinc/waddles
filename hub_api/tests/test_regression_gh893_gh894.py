"""Regression tests for gh-893 (GET /api/v1/modules 500, HTML leak) and gh-894 (analytics).

gh-893: `hub_modules` was defined by several schema binders with divergent
column lists and no per-table guards, so the startup bind order produced
`table already defined` / missing `is_featured`, and with no app-level
error handler Quart leaked a raw HTML 500. gh-894: `analytics_service._relay`
masked an unreachable `analytics-core` as a generic 500.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest
from quart import Quart

from app import create_app
from services import analytics_service as svc
from services.analytics_proxy import AnalyticsCoreProxyClient, AnalyticsProxyConfig, ProxyResult
from services.schema import (
    bind_admin_tables,
    bind_marketplace_billing_tables,
    bind_marketplace_catalog_tables,
    bind_platform_tables,
    bind_tenant_tables,
)
from tests.test_app_factory import _test_config

_BINDERS = [
    bind_admin_tables,
    bind_tenant_tables,
    bind_marketplace_catalog_tables,
    bind_marketplace_billing_tables,
]

_CREATE_SQL = [
    "CREATE TABLE IF NOT EXISTS hub_modules (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name VARCHAR(255) NOT NULL UNIQUE, display_name VARCHAR(255), description TEXT, "
    "version VARCHAR(50), author VARCHAR(255), category VARCHAR(100), icon_url TEXT, "
    "is_published CHAR(1), is_core CHAR(1), is_featured CHAR(1), config_schema TEXT, "
    "created_at TIMESTAMP, updated_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS hub_module_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "module_id INTEGER, community_id INTEGER, user_id INTEGER, rating INTEGER, "
    "review_text TEXT, admin_notes TEXT, created_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS hub_module_installations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "community_id INTEGER, module_id INTEGER, installed_by INTEGER, config TEXT, "
    "is_enabled CHAR(1), installed_at TIMESTAMP, updated_at TIMESTAMP)",
]


class _BoomError(RuntimeError):
    """Sentinel exception for the unhandled-exception handler test."""


@pytest.fixture
def app() -> Quart:
    return create_app(_test_config())


class TestHubModulesBinding:
    # regression: gh-893
    async def test_modules_endpoint_after_real_startup_bind_order_is_200_json(
        self, app: Quart
    ) -> None:
        async with app.test_app():
            dal = app.config["dal"]
            # startup's bind_platform_tables already defined hub_modules: re-binding via the
            # catalog binder must neither raise nor lose is_featured.
            bind_marketplace_catalog_tables(dal)
            assert "is_featured" in dal.hub_modules.fields
            for sql in _CREATE_SQL:
                dal.executesql(sql)
            response = await app.test_client().get("/api/v1/modules")
            assert response.status_code == 200
            assert response.content_type.startswith("application/json")
            body = await response.get_json()
            assert body["success"] is True
            assert body["modules"] == []

    # regression: gh-893
    @pytest.mark.parametrize("order", list(itertools.permutations(range(len(_BINDERS)))))
    def test_every_binder_order_is_idempotent_with_one_canonical_hub_modules(
        self, order: tuple[int, ...], tmp_path: Any
    ) -> None:
        from pydal import DAL

        dal = DAL(f"sqlite://{tmp_path}/b{'_'.join(map(str, order))}.db", folder=str(tmp_path))
        bind_platform_tables(dal)  # what app startup runs first (also defines hub_modules)
        for i in order:
            _BINDERS[i](dal)  # each hub_modules-bearing binder, any order, must not raise
        fields = set(dal.hub_modules.fields)
        assert {"is_featured", "icon_url", "author", "config_schema", "updated_at"} <= fields


class TestGlobalErrorHandler:
    # regression: gh-893 (defense in depth -- no endpoint leaks Quart's HTML 500)
    async def test_unhandled_exception_returns_json_envelope_not_html(
        self, app: Quart, caplog: pytest.LogCaptureFixture
    ) -> None:
        @app.route("/_boom")
        async def boom() -> str:
            raise _BoomError("secret internal detail")

        async with app.test_app():
            with caplog.at_level("ERROR"):
                response = await app.test_client().get("/_boom")
        assert response.status_code == 500
        assert response.content_type.startswith("application/json")
        body = await response.get_json()
        assert body["success"] is False
        assert body["error"]["code"] == "ERROR_500"
        assert "secret internal detail" not in str(body)
        assert any(r.exc_info and r.exc_info[0] is _BoomError for r in caplog.records)

    async def test_http_exceptions_keep_their_status(self, app: Quart) -> None:
        async with app.test_app():
            response = await app.test_client().get("/definitely/not/a/route")
        assert response.status_code == 404


_CFG = AnalyticsProxyConfig(base_url="http://analytics-core:8040", api_key="k", timeout_seconds=1.0)


class TestAnalyticsFailLoud:
    # regression: gh-894
    @pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
    async def test_relay_upstream_unreachable_is_502_envelope(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__

        def patched(self: httpx.AsyncClient, *a: object, **kw: object) -> None:
            kw["transport"] = transport
            original(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        client = AnalyticsCoreProxyClient(_CFG)
        for call in (
            svc.reputation_distribution(client),
            svc.activity_breakdown(client),
            svc.platform_overview(client),
        ):
            body, status = await call
            assert status == 502
            assert body["success"] is False
            assert "analytics-core" in body["error"]["message"]
            assert "unreachable" in body["error"]["message"]

    # regression: gh-894
    def test_relay_downstream_5xx_is_502_naming_upstream(self) -> None:
        body, status = svc._relay(ProxyResult(ok=False, status_code=503, body=None))
        assert status == 502
        assert "HTTP 503" in body["error"]["message"]
