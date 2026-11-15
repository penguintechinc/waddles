"""Default-tenant slug: one source of truth, real-path public endpoints, loud failure.

Regression (alpha, 2026-11): the tenants table held only the reserved `default` and `system`
rows while a stale `global` slug was still being resolved, so the public landing surface failed
with "tenant 'global' does not exist". These tests pin the fix against a real (sqlite-file) DAL
seeded exactly like alpha: ONLY `default` and `system`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.auth import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

from app import create_app
from blueprints.v1.public import public_bp
from config import HubAPIConfig
from services.default_tenant import (
    DefaultTenantMissingError,
    require_default_tenant,
    verify_default_tenant_at_startup,
)
from services.global_identity import SYSTEM_TENANT_SLUG
from tests.test_v1_public_blueprint import _test_config


@pytest.fixture
def alpha_db(platform_db: Any) -> Any:
    """`platform_db` re-seeded like alpha: ONLY the reserved `default` and `system` tenants."""
    dal = platform_db.dal
    dal(dal.tenants.id > 0).delete()
    dal.tenants.insert(slug=DEFAULT_TENANT_SLUG, display_name="Default", is_global=True)
    dal.tenants.insert(slug=SYSTEM_TENANT_SLUG, display_name="System", is_global=False)
    dal.commit()
    return platform_db


def _make_app(db: Any, slug: str) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(public_bp)
    app.config["dal"] = db.dal
    app.config["async_dal"] = db
    app.config["HUB_API_CONFIG"] = replace(_test_config(), default_tenant_slug=slug)
    return app


def _seed_public_community(db: Any, tenant_slug: str, name: str) -> None:
    tenant = db.dal(db.dal.tenants.slug == tenant_slug).select().first()
    db.dal.communities.insert(
        name=name,
        display_name=name.title(),
        is_active=True,
        is_public=True,
        community_type="creator",
        member_count=3,
        tenant_id=tenant.id,
        created_at=datetime.now(UTC),
    )
    db.dal.commit()


def test_config_default_comes_from_the_single_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env -> `flask_core.auth.DEFAULT_TENANT_SLUG` ('default'), never a stale 'global'."""
    monkeypatch.delenv("DEFAULT_TENANT_SLUG", raising=False)
    cfg = HubAPIConfig.from_env()
    assert cfg.default_tenant_slug == DEFAULT_TENANT_SLUG == "default"


def test_config_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit `DEFAULT_TENANT_SLUG` env var (the Helm value) overrides the constant."""
    monkeypatch.setenv("DEFAULT_TENANT_SLUG", "custom-default")
    assert HubAPIConfig.from_env().default_tenant_slug == "custom-default"


class TestPublicEndpointsWithOnlyReservedTenants:
    """Alpha shape: tenants = {default, system}. Public endpoints must return data."""

    async def test_communities_listed(self, alpha_db: Any) -> None:
        _seed_public_community(alpha_db, DEFAULT_TENANT_SLUG, "visible")
        _seed_public_community(alpha_db, SYSTEM_TENANT_SLUG, "operator-plane")
        client = _make_app(alpha_db, DEFAULT_TENANT_SLUG).test_client()

        response = await client.get("/api/v1/public/communities")

        assert response.status_code == 200
        body = await response.get_json()
        assert [c["name"] for c in body["communities"]] == ["visible"]

    async def test_spotlighted_and_live_streams_ok(self, alpha_db: Any) -> None:
        _seed_public_community(alpha_db, DEFAULT_TENANT_SLUG, "visible")
        client = _make_app(alpha_db, DEFAULT_TENANT_SLUG).test_client()

        spotlighted = await client.get("/api/v1/public/communities/spotlighted")
        live = await client.get("/api/v1/public/live")

        assert spotlighted.status_code == 200
        assert live.status_code == 200
        assert (await live.get_json())["success"] is True


class TestMissingDefaultTenantIsLoud:
    """A slug with no tenants row must be a logged ERROR + structured 5xx, never empty data."""

    async def test_first_use_returns_structured_500_and_logs_error(
        self, alpha_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        _seed_public_community(alpha_db, DEFAULT_TENANT_SLUG, "visible")
        before = alpha_db.dal(alpha_db.dal.tenants).count()
        client = _make_app(alpha_db, "global").test_client()

        with caplog.at_level(logging.ERROR, logger="services.default_tenant"):
            response = await client.get("/api/v1/public/communities")

        assert response.status_code == 500
        body = await response.get_json()
        assert body["error"]["code"] == "DEFAULT_TENANT_MISSING"
        assert "global" not in body["error"]["message"]  # no tenant inventory leaks to clients
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert "'global'" in message
        assert "default" in message and "system" in message
        # Never auto-creates a tenant.
        assert alpha_db.dal(alpha_db.dal.tenants).count() == before

    async def test_inactive_default_tenant_is_also_loud(
        self, alpha_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        alpha_db.dal(alpha_db.dal.tenants.slug == DEFAULT_TENANT_SLUG).update(is_active=False)
        alpha_db.dal.commit()
        client = _make_app(alpha_db, DEFAULT_TENANT_SLUG).test_client()

        with caplog.at_level(logging.ERROR, logger="services.default_tenant"):
            response = await client.get("/api/v1/public/communities/spotlighted")

        assert response.status_code == 500
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    async def test_require_default_tenant_raises_with_existing_reserved(
        self, alpha_db: Any
    ) -> None:
        with pytest.raises(DefaultTenantMissingError) as info:
            await require_default_tenant(alpha_db, alpha_db.dal, "global")
        assert info.value.slug == "global"
        assert info.value.existing_reserved == sorted([DEFAULT_TENANT_SLUG, SYSTEM_TENANT_SLUG])


class TestStartupCheck:
    async def test_ok_when_default_tenant_exists(self, alpha_db: Any) -> None:
        outcome = await verify_default_tenant_at_startup(
            alpha_db, alpha_db.dal, DEFAULT_TENANT_SLUG
        )
        assert outcome == "ok"

    async def test_missing_logs_error_and_does_not_crash(
        self, alpha_db: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="services.default_tenant"):
            outcome = await verify_default_tenant_at_startup(alpha_db, alpha_db.dal, "global")
        assert outcome == "missing"
        assert any(
            r.levelno == logging.ERROR and "'global'" in r.getMessage() for r in caplog.records
        )

    async def test_unreadable_tenants_table_is_unverifiable_not_ok(self) -> None:
        class _AsyncNoTenants:
            """Async DAL whose `tenants` table does not exist yet (schema bootstrapping)."""

            async def select_async(self, *_: Any) -> Any:
                raise RuntimeError('relation "tenants" does not exist')

        class _Tenants:
            slug = "slug"

        class _Dal:
            """Raw DAL stand-in: calling it builds a query, `rollback` is a no-op."""

            tenants = _Tenants()

            def __call__(self, *_: Any) -> object:
                return object()

            def rollback(self) -> None:
                return None

        outcome = await verify_default_tenant_at_startup(
            _AsyncNoTenants(), _Dal(), DEFAULT_TENANT_SLUG
        )
        assert outcome == "unverifiable"

    async def test_app_startup_records_the_outcome(self) -> None:
        """`create_app` startup wires the check and publishes its outcome (sqlite: unreadable)."""
        app = create_app(replace(_test_config(), default_tenant_slug=DEFAULT_TENANT_SLUG))
        async with app.test_app():
            assert app.config["DEFAULT_TENANT_STATUS"] in {"ok", "missing", "unverifiable"}
