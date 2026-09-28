"""Shared fixtures for community_module's tests.

community_module isn't installed as a package -- its own directory has to
be put on `sys.path` explicitly for `from app import app` to resolve,
matching every other core Quart service's test conftest in this monorepo
(see `core/security_core_module/tests/conftest.py`).

`flask_core` is imported here FIRST, before `app` -- `app.py` itself does
`sys.path.insert(0, <repo_root>/libs)` at import time so it can find
`flask_core` when run un-containerized straight from a repo checkout; in
that checkout layout `libs/flask_core` is the flask_core *project* root
(setup.py, tests/, the nested `flask_core/` package dir), not the
importable package itself, so that insert makes `flask_core` resolve as a
broken PEP 420 namespace package instead of the real, pip-installed one.
Importing the real module into `sys.modules` before `app.py` ever runs
means its own `from flask_core import (...)` finds the already-cached good
module and never re-searches `sys.path`.
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

SECRET_KEY = "change-me-in-production"


@pytest_asyncio.fixture
async def app_and_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, Any]]:
    """A running community_module app (startup/shutdown fired) against a throwaway sqlite DB."""
    db_path = tmp_path / "community_module_test.db"
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", f"sqlite://{db_path}")

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
