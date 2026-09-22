"""Shared fixtures for labels-core's tests.

Same local-dev import-ordering workaround as `core/security_core_module/
tests/conftest.py` -- import `flask_core` before `app` so `app.py`'s own
`sys.path.insert(0, <repo_root>/libs)` can't shadow the real, pip-installed
package with a broken namespace-package resolution. See that module's own
docstring for the full explanation.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import flask_core  # noqa: F401 - see module docstring; must import before `app`

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from flask_core.auth import create_jwt_token  # noqa: E402
from flask_core.community_access import bind_shared_read_tables  # noqa: E402

SECRET_KEY = "change-me-in-production"
TENANT_SLUG = "acme-corp"
OTHER_TENANT_SLUG = "other-corp"


@pytest_asyncio.fixture
async def app_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any]]:
    """A running app (startup/shutdown fired) against a throwaway sqlite DB."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite://{db_path}")
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
def dal_pair(app_and_client: tuple[Any, Any]) -> tuple[Any, Any]:
    app_module, _ = app_and_client
    return app_module.app.config["async_dal"], app_module.app.config["dal"]


def seed_tenant(dal: Any, *, slug: str = TENANT_SLUG) -> int:
    bind_shared_read_tables(dal, migrate=True)
    tenant_id: int = dal.tenants.insert(slug=slug, is_active=True)
    dal.commit()
    return tenant_id


def make_token(*, sub: str = "1", tenant: str = TENANT_SLUG, roles: list[str] | None = None) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.com",
        roles=roles or [],
        secret_key=SECRET_KEY,
        tenant=tenant,
    )
