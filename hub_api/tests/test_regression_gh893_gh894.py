"""Regression tests for gh-893 (GET /api/v1/modules 500, HTML leak) and gh-894 (analytics).

gh-893: `hub_modules` was defined by several schema binders with divergent
column lists and no per-table guards, so the startup bind order produced
`table already defined` / missing `is_featured`, and with no app-level
error handler Quart leaked a raw HTML 500. gh-894: `analytics_service._relay`
masked an unreachable `analytics-core` as a generic 500 (now: transport/5xx -> 502,
4xx passthrough).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from quart import Quart

from app import create_app
from services import analytics_service as svc
from services.analytics_proxy import AnalyticsCoreProxyClient, AnalyticsProxyConfig, ProxyResult
from services.schema import bind_marketplace_module_tables
from tests.test_app_factory import _test_config

_FAMILY = (
    "hub_modules",
    "hub_module_reviews",
    "hub_module_installations",
    "marketplace_catalog",
    "marketplace_subscriptions",
    "marketplace_settings",
    "vendor_discount_codes",
    "discount_code_redemptions",
)

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
    async def test_startup_binds_every_table_exactly_once_and_modules_is_200_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pydal import DAL

        calls: list[str] = []
        original = DAL.define_table

        def spy(self: DAL, tablename: str, *a: Any, **kw: Any) -> Any:
            calls.append(tablename)
            return original(self, tablename, *a, **kw)

        monkeypatch.setattr(DAL, "define_table", spy)
        app = create_app(_test_config())
        async with app.test_app():
            dal = app.config["dal"]
            for name in _FAMILY:
                assert calls.count(name) == 1, f"{name} defined {calls.count(name)}x at startup"
            assert "is_featured" in dal.hub_modules.fields
            assert "module_name" in dal.hub_module_installations.fields
            for sql in _CREATE_SQL:
                dal.executesql(sql)
            response = await app.test_client().get("/api/v1/modules")
            assert response.status_code == 200
            assert response.content_type.startswith("application/json")
            body = await response.get_json()
            assert body["success"] is True
            assert body["modules"] == []

    # regression: gh-893
    def test_genuine_double_define_fails_loud(self, tmp_path: Any) -> None:
        from pydal import DAL

        dal = DAL(f"sqlite://{tmp_path}/dd.db", folder=str(tmp_path))
        bind_marketplace_module_tables(dal)
        with pytest.raises(SyntaxError, match="already defined"):
            bind_marketplace_module_tables(dal)

    # regression: gh-893
    def test_no_domain_binder_redefines_a_family_table(self, tmp_path: Any) -> None:
        from pydal import DAL

        from services import schema

        binders = [
            schema.bind_platform_tables,
            schema.bind_admin_tables,
            schema.bind_tenant_tables,
            schema.bind_marketplace_vendor_tables,
            schema.bind_marketplace_billing_tables,
        ]
        for i, binder in enumerate(binders):
            dal = DAL(f"sqlite://{tmp_path}/d{i}.db", folder=str(tmp_path))
            bind_marketplace_module_tables(dal)
            binder(dal)  # must not raise "table already defined" on a family table


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
    @pytest.mark.parametrize("status", [400, 404, 422, 429])
    def test_relay_upstream_4xx_passes_through_unchanged(self, status: int) -> None:
        envelope = {"success": False, "error": {"message": "nope", "code": "X"}}
        body, out = svc._relay(ProxyResult(ok=False, status_code=status, body=envelope))
        assert out == status
        assert body == envelope

    # regression: gh-894
    def test_relay_downstream_5xx_is_502_naming_upstream(self) -> None:
        body, status = svc._relay(ProxyResult(ok=False, status_code=503, body=None))
        assert status == 502
        assert "HTTP 503" in body["error"]["message"]
