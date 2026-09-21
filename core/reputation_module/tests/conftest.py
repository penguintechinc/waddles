"""Shared fixtures for reputation_module's real-Postgres integration tests.

Deliberately connects to a *real* Postgres running the actual
``config/postgres/migrations/*.sql`` files rather than a hand-defined
pydal/SQLite schema -- the bug this suite guards against (reputation_events /
reputation_global missing their CREATE TABLE migration, see migration
080_add_reputation_tables.sql) is invisible to any test that defines its own
schema independently of the migration files.

Skips the whole module if no reachable Postgres is configured via
``TEST_DATABASE_URL``/``DATABASE_URL`` -- this is an integration suite, not a
unit test, and CI does not currently provision Postgres for this module (see
docstring in test_reputation_tables.py for local run instructions).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "..", "libs", "flask_core"),
)

from pydal import DAL  # noqa: E402


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _try_connect() -> DAL | None:
    uri = _test_database_url()
    if not uri:
        return None
    # PyDAL uses the postgres:// scheme, same conversion flask_core.database
    # does for the real app.
    converted = uri.replace("postgresql://", "postgres://", 1)
    try:
        d = DAL(converted, migrate=False, pool_size=1)
        d.executesql("SELECT 1")
        return d
    except Exception:
        return None


class NullLogger:
    """No-op logger satisfying ReputationService/WeightManager's logger interface."""

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def audit(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.fixture()
def dal() -> Iterator[DAL]:
    connection = _try_connect()
    if connection is None:
        pytest.skip(
            "requires a live Postgres with config/postgres/migrations applied "
            "(set TEST_DATABASE_URL or DATABASE_URL)"
        )
    yield connection
    connection.close()


@pytest.fixture()
def seeded_ids(dal: DAL) -> Iterator[tuple[int, int]]:
    """Create a throwaway tenant/community/hub_user row set for one test."""
    suffix = uuid.uuid4().int % 1_000_000
    tenant_id = 900_000_000 + suffix
    community_id = 900_000_000 + suffix
    hub_user_id = 900_000_000 + suffix

    dal.executesql(
        "INSERT INTO tenants (id, slug, display_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        [tenant_id, f"repmig-tenant-{suffix}", "Reputation Migration Test Tenant"],
    )
    dal.executesql(
        "INSERT INTO communities "
        "(id, name, display_name, primary_platform, platform, tenant_id) "
        "VALUES (%s, %s, %s, 'discord', 'discord', %s) ON CONFLICT DO NOTHING",
        [
            community_id,
            f"repmig-community-{suffix}",
            "Reputation Migration Test Community",
            tenant_id,
        ],
    )
    dal.executesql(
        "INSERT INTO hub_users (id, username, display_name) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        [hub_user_id, f"repmig-user-{suffix}", "Reputation Migration Test User"],
    )
    dal.commit()

    yield community_id, hub_user_id

    dal.executesql("DELETE FROM reputation_events WHERE community_id = %s", [community_id])
    dal.executesql("DELETE FROM reputation_global WHERE hub_user_id = %s", [hub_user_id])
    dal.executesql("DELETE FROM community_members WHERE community_id = %s", [community_id])
    dal.executesql("DELETE FROM communities WHERE id = %s", [community_id])
    dal.executesql("DELETE FROM hub_users WHERE id = %s", [hub_user_id])
    dal.executesql("DELETE FROM tenants WHERE id = %s", [tenant_id])
    dal.commit()
