"""Narrow unit coverage for `services/marketplace_catalog_service.py` private helpers.

Exercises edge branches the blueprint-level tests don't reach through a
real HTTP request -- specifically `visible_tenant_ids()`/`_tenant_scope_
query()`'s degenerate "no global tenant row exists at all" path, which a
production deployment (migration 058 always seeds the global tenant)
never hits but the function must still fail safe against.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydal import DAL, Field

from services.marketplace_catalog_service import _tenant_scope_query, visible_tenant_ids
from services.schema import bind_marketplace_module_tables


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


@pytest.fixture
def bare_dal(tmp_path: Any) -> Any:
    """A DAL with NO tenant rows -- the degenerate case `visible_tenant_ids()` must survive.

    File-backed (not `sqlite:memory`) for the same reason `tests/conftest.
    py::marketplace_catalog_db` is -- see that fixture's docstring.
    """
    dal = DAL(f"sqlite://{tmp_path / 'bare_test.db'}")
    dal.define_table("tenants", Field("slug", unique=True), Field("is_active", "boolean"))
    bind_marketplace_module_tables(dal, migrate=True)
    dal.marketplace_catalog.insert(
        source="core",
        source_id=1,
        name="core-widget",
        is_core=True,
        tenant_id=None,
        avg_rating=0,
        review_count=0,
        install_count=0,
        price_cents=0,
    )
    dal.commit()
    yield dal
    dal.close()


class TestVisibleTenantIdsDegenerateCase:
    def test_no_global_tenant_row_yields_empty_set(self, bare_dal: Any) -> None:
        ids = visible_tenant_ids(bare_dal, _FakeRequest())
        assert ids == frozenset()

    def test_empty_visible_ids_scopes_to_core_only(self, bare_dal: Any) -> None:
        query = _tenant_scope_query(bare_dal, frozenset())
        rows = bare_dal(query).select()
        assert [r.name for r in rows] == ["core-widget"]
