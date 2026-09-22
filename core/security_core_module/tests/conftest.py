"""Shared fixtures for security-core's tests.

security_core_module isn't installed as a package -- its own directory has
to be put on `sys.path` explicitly for `from app import app` to resolve,
matching every other core Quart service's test conftest in this monorepo
(see `core/svc_streaming/tests/conftest.py`).

`flask_core` is imported here FIRST, before `app` -- `app.py` itself does
`sys.path.insert(0, <repo_root>/libs)` at import time (line 10) so it can
find `flask_core` when run un-containerized straight from a repo checkout;
in that checkout layout `libs/flask_core` is the flask_core *project* root
(setup.py, tests/, the nested `flask_core/` package dir), not the
importable package itself, so that insert makes `flask_core` resolve as a
broken PEP 420 namespace package instead of the real, pip-installed one
(`waddlebot-flask-core`, this venv's `flask_core/flask_core/__init__.py`).
Importing the real module into `sys.modules` before `app.py` ever runs
means its own `from flask_core import (...)` finds the already-cached good
module and never re-searches `sys.path` -- this is a test-harness-only
workaround for a local-dev-only import ordering quirk (the Dockerfile
`pip install`s flask_core into site-packages and `app.py`'s `dirname()`
chain lands outside `/app` in the container, so this never bites
production).
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
    """A running security-core app (startup/shutdown fired) against a throwaway sqlite DB."""
    db_path = tmp_path / "security_core_test.db"
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite://{db_path}")
    monkeypatch.setenv("DB_MIGRATE", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key")

    # config.py / app.py both read env vars at import time -- force a fresh
    # import per test so each test's monkeypatched env actually takes.
    for mod_name in ("app", "config"):
        sys.modules.pop(mod_name, None)

    import app as app_module

    async with app_module.app.test_app() as running:
        yield app_module, running.test_client()


@pytest_asyncio.fixture
async def client(app_and_client: tuple[Any, Any]) -> Any:
    """Just the test client half of `app_and_client`, for tests that don't need the app module."""
    return app_and_client[1]


@pytest.fixture
def dal_pair(app_and_client: tuple[Any, Any]) -> tuple[Any, Any]:
    """`(async_dal, dal)` from the running app -- for direct seeding/assertions."""
    app_module, _ = app_and_client
    return app_module.app.config["async_dal"], app_module.app.config["dal"]


def seed_tenant(dal: Any, *, slug: str = TENANT_SLUG) -> int:
    """Insert one active `tenants` row; returns its id."""
    bind_shared_read_tables(dal, migrate=True)
    tenant_id: int = dal.tenants.insert(slug=slug, is_active=True)
    dal.commit()
    return tenant_id


def seed_community(dal: Any, *, tenant_id: int) -> int:
    """Insert one `communities` row scoped to `tenant_id`; returns its id."""
    bind_shared_read_tables(dal, migrate=True)
    community_id: int = dal.communities.insert(tenant_id=tenant_id)
    dal.commit()
    return community_id


def seed_membership(dal: Any, *, community_id: int, user_id: int, role: str = "member") -> None:
    """Insert one `community_members` row."""
    bind_shared_read_tables(dal, migrate=True)
    dal.community_members.insert(
        community_id=community_id, user_id=str(user_id), role=role, is_active=True
    )
    dal.commit()


def make_token(*, sub: str = "1", tenant: str = TENANT_SLUG, roles: list[str] | None = None) -> str:
    return create_jwt_token(
        user_id=sub,
        username="alice",
        email="alice@example.com",
        roles=roles or [],
        secret_key=SECRET_KEY,
        tenant=tenant,
    )
