"""Shared fixtures for engagement-module's tests."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from flask_core.auth import create_jwt_token  # noqa: E402
from flask_core.community_access import bind_shared_read_tables  # noqa: E402

SECRET_KEY = "change-me-in-production"
TENANT_SLUG = "acme-corp"


@pytest_asyncio.fixture
async def app_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any]]:
    """A running app (startup/shutdown fired) against a throwaway sqlite DB."""
    databases_dir = tmp_path / "databases"
    databases_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("MODULE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("JWT_SECRET", "jwt-secret-key-change-in-prod")
    monkeypatch.setenv("DATABASE_URL", "sqlite://test.db")
    monkeypatch.setenv("DB_MIGRATE", "true")

    for mod_name in ("app", "config"):
        sys.modules.pop(mod_name, None)

    import app as app_module

    async with app_module.app.test_app() as running:
        yield app_module, running.test_client()


@pytest_asyncio.fixture
async def client(app_and_client: tuple[Any, Any]) -> Any:
    return app_and_client[1]


@pytest.fixture
def app_db(app_and_client: tuple[Any, Any]) -> Any:
    app_module, _ = app_and_client
    return app_module.db


def seed_tenant(db: Any, *, slug: str = TENANT_SLUG) -> int:
    bind_shared_read_tables(db, migrate=True)
    tenant_id: int = db.tenants.insert(slug=slug, is_active=True)
    db.commit()
    return tenant_id


def make_token(*, sub: str = "1", tenant: str = TENANT_SLUG) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.com",
        roles=[],
        secret_key=SECRET_KEY,
        tenant=tenant,
    )
