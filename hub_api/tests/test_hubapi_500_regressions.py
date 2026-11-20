"""Regression tests for the alpha hub-api 500s found by the webui sweep (PR #1466).

Root causes covered here:
- `bind_tenant_tables` redefined `communities` with a minimal column list, dropping
  `description`/`platform`/`deleted_at`/`license_tier`/... so profile, admin settings,
  superadmin dashboard/communities/platform-communities 500'd with `AttributeError`.
- `tenants` lacked `updated_at` after `bind_tenant_tables` (superadmin tenants 500).
- Bot sync-DAL calls left an aborted Postgres transaction on pooled threads
  (`InFailedSqlTransaction` on shoutout/creators, rcon/servers).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydal import DAL, Field
from quart import Quart

from blueprints.v1 import bot as bot_blueprint
from services.schema import bind_auth_tables, bind_superadmin_tenant_fields, bind_tenant_tables

# Columns the services read off `communities` (regression: sweep 2026-11-20).
COMMUNITY_COLUMNS = {
    "description",
    "platform",
    "deleted_at",
    "updated_at",
    "license_tier",
    "join_mode",
    "is_public",
    "visibility",
    "logo_url",
    "banner_url",
    "owner_name",
    "public_id",
    "tenant_id",
}


@pytest.fixture
def bare_dal() -> Any:
    """Empty in-memory pydal DB (no tables bound)."""
    dal = DAL("sqlite:memory")
    yield dal
    dal.close()


def test_tenant_binder_alone_keeps_every_community_column(bare_dal: Any) -> None:
    """regression: sweep-500 -- tenant binder must not drop `deleted_at`/`description`/..."""
    bind_tenant_tables(bare_dal, migrate=True)
    assert COMMUNITY_COLUMNS <= set(bare_dal.communities.fields)


def test_auth_then_tenant_binder_does_not_narrow_communities(bare_dal: Any) -> None:
    """regression: sweep-500 -- re-binding via the tenant binder keeps the full column set."""
    bind_auth_tables(bare_dal, migrate=False)
    full = set(bare_dal.communities.fields)
    bind_tenant_tables(bare_dal, migrate=False)
    assert set(bare_dal.communities.fields) == full
    assert COMMUNITY_COLUMNS <= full


def test_profile_active_community_query_works_after_tenant_binder(bare_dal: Any) -> None:
    """The exact `deleted_at == None` filter profile uses must build and return the row."""
    bind_tenant_tables(bare_dal, migrate=True)
    tenant_id = bare_dal.tenants.insert(slug="t1", is_active=True)
    cid = bare_dal.communities.insert(
        tenant_id=tenant_id, name="c", description="d", platform="discord", is_active=True
    )
    rows = bare_dal(
        (bare_dal.communities.id == cid)
        & (bare_dal.communities.is_active == True)  # noqa: E712
        & (bare_dal.communities.deleted_at == None)  # noqa: E711
    ).select()
    assert len(rows) == 1
    assert rows.first().description == "d"
    assert rows.first().platform == "discord"


def test_tenants_have_updated_at_for_superadmin_listing(bare_dal: Any) -> None:
    """regression: sweep-500 -- `superadmin/tenants` reads `tenants.updated_at`."""
    bind_tenant_tables(bare_dal, migrate=True)
    assert "updated_at" in bare_dal.tenants.fields


def test_superadmin_tenant_fields_binder_adds_updated_at_when_missing(bare_dal: Any) -> None:
    """The superadmin binder's idempotence guard must not skip a table lacking `updated_at`."""
    bare_dal.define_table(
        "tenants",
        Field("slug", "string"),
        Field("seat_limit", "integer"),
        migrate=False,
    )
    bind_superadmin_tenant_fields(bare_dal, migrate=False)
    assert "updated_at" in bare_dal.tenants.fields


@pytest.mark.asyncio
async def test_bot_to_thread_rolls_back_on_failure_and_reraises() -> None:
    """regression: sweep-500 -- a failing sync-DAL call rolls back its thread's connection."""
    app = Quart(__name__)
    dal = MagicMock()
    app.config["dal"] = dal

    def boom() -> None:
        raise RuntimeError("statement failed")

    async with app.app_context():
        with pytest.raises(RuntimeError, match="statement failed"):
            await bot_blueprint._to_thread(boom)
    dal.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_bot_to_thread_success_does_not_rollback() -> None:
    """The happy path returns the value and never rolls back."""
    app = Quart(__name__)
    dal = MagicMock()
    app.config["dal"] = dal
    async with app.app_context():
        assert await bot_blueprint._to_thread(lambda a, b=0: a + b, 2, b=3) == 5
    dal.rollback.assert_not_called()
