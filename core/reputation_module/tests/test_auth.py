"""Reputation-Module Authentication/Authorization Tests.

`app.py`'s `api_bp` (community/global reputation reads) and `admin_bp`
(community-scoped reputation writes/config) previously had ZERO
authentication -- `internal_bp` already gated on `_verify_service_key()`,
but these two did not, including several routes taking `community_id`
straight off the URL (BOLA, OWASP A01). This is the fix's regression
suite.

Deliberately self-contained (its own fixtures, not `tests/conftest.py`) --
that conftest is scoped to `test_reputation_tables.py`'s real-Postgres
migration-integration suite (its own `dal`/`seeded_ids` fixtures skip
without a live Postgres); this suite runs against a throwaway sqlite DB
via `app.test_app()`, same shape as `core/security_core_module/tests/
conftest.py`, and must not collide with or replace that file.

Fail-first proof: with `install_community_scoped_auth(api_bp)` and
`install_community_scoped_auth(admin_bp)` (app.py's module-level calls)
commented out, `test_leaderboard_requires_token`,
`test_non_member_leaderboard_is_403`, and
`test_member_forbidden_from_admin_config_write` all went green->red as
expected (200 instead of 401/403). Reverted after confirming; see PR
report for the exact before/after run.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import flask_core  # noqa: F401 - must import before `app`; see module docstring

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from flask_core.auth import create_jwt_token  # noqa: E402
from flask_core.community_access import bind_shared_read_tables  # noqa: E402

_SECRET_KEY = "change-me-in-production"
_TENANT_SLUG = "acme-corp"


@pytest_asyncio.fixture
async def app_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any]]:
    """A running app (startup/shutdown fired) against a throwaway sqlite DB."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SECRET_KEY", _SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite://{db_path}")
    monkeypatch.setenv("DB_MIGRATE", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key")
    monkeypatch.setenv("GRPC_PORT", "0")
    # app.py's startup() unconditionally calls bind_secure_port(), which
    # fails closed without real TLS cert/key material (flask_core.grpc_tls)
    # -- the explicit dev-only escape hatch is required to boot the test app.
    monkeypatch.setenv("GRPC_TLS_INSECURE_DEV", "true")

    for mod_name in ("app", "config"):
        sys.modules.pop(mod_name, None)

    import app as app_module

    async with app_module.app.test_app() as running:
        yield app_module, running.test_client()


@pytest_asyncio.fixture
async def client(app_and_client: tuple[Any, Any]) -> Any:
    return app_and_client[1]


@pytest.fixture
def app_dal_pair(app_and_client: tuple[Any, Any]) -> tuple[Any, Any]:
    app_module, _ = app_and_client
    return app_module.app.config["async_dal"], app_module.app.config["dal"]


def _seed_tenant(dal: Any, *, slug: str = _TENANT_SLUG) -> int:
    bind_shared_read_tables(dal, migrate=True)
    tenant_id: int = dal.tenants.insert(slug=slug, is_active=True)
    dal.commit()
    return tenant_id


def _seed_community(dal: Any, *, tenant_id: int) -> int:
    bind_shared_read_tables(dal, migrate=True)
    community_id: int = dal.communities.insert(tenant_id=tenant_id)
    dal.commit()
    return community_id


def _seed_membership(dal: Any, *, community_id: int, user_id: int, role: str = "member") -> None:
    bind_shared_read_tables(dal, migrate=True)
    dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role=role, is_active=True
    )
    dal.commit()


def _make_token(*, sub: str = "1", tenant: str = _TENANT_SLUG) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.com",
        roles=[],
        secret_key=_SECRET_KEY,
        tenant=tenant,
    )


class TestApiBpAuth:
    async def test_status_requires_token(self, client: Any) -> None:
        response = await client.get("/api/v1/status")
        assert response.status_code == 401

    async def test_leaderboard_requires_token(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        response = await client.get(f"/api/v1/reputation/{community_id}/leaderboard")
        assert response.status_code == 401

    async def test_non_member_leaderboard_is_403(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        token = _make_token(sub="999")
        response = await client.get(
            f"/api/v1/reputation/{community_id}/leaderboard",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_member_passes_read_route(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        _seed_membership(dal, community_id=community_id, user_id=1, role="member")
        token = _make_token(sub="1")
        response = await client.get(
            f"/api/v1/reputation/{community_id}/leaderboard",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code not in (401, 403)


class TestAdminBpAuth:
    async def test_config_write_requires_token(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        response = await client.put(f"/api/v1/admin/{community_id}/reputation/config", json={})
        assert response.status_code == 401

    async def test_member_forbidden_from_admin_config_write(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        """A plain member cannot write admin config -- only community-admin."""
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        _seed_membership(dal, community_id=community_id, user_id=1, role="member")
        token = _make_token(sub="1")
        response = await client.put(
            f"/api/v1/admin/{community_id}/reputation/config",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert response.status_code == 403

    async def test_admin_passes_config_write_gate(
        self, client: Any, app_dal_pair: tuple[Any, Any]
    ) -> None:
        _, dal = app_dal_pair
        tenant_id = _seed_tenant(dal)
        community_id = _seed_community(dal, tenant_id=tenant_id)
        _seed_membership(dal, community_id=community_id, user_id=2, role="community-admin")
        token = _make_token(sub="2")
        response = await client.put(
            f"/api/v1/admin/{community_id}/reputation/config",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        assert response.status_code not in (401, 403)


class TestInternalBpStillGated:
    """Regression check -- the pre-existing `_verify_service_key()` gate is untouched."""

    async def test_events_requires_service_key(self, client: Any) -> None:
        response = await client.post("/api/v1/internal/events", json={})
        assert response.status_code == 401
