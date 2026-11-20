"""Concurrency regression for `services/marketplace_lifecycle_service.py`.

Companion to `test_v1_marketplace_lifecycle_blueprint.py` (which covers
the HTTP/authz surface with sequential calls only) -- this module isolates
the service layer's atomicity property under CONCURRENT load, the same
class of bug `services/token_billing_service.py`'s module docstring (and
`test_token_billing_service.py::TestConcurrentDebitsDoNotOversell`)
documents for `AsyncDAL.transaction_async()`.

Fail-first verification performed (executed, not narrated): checked out
this module's pre-fix revision of `marketplace_lifecycle_service.py`
(the `async with async_dal.transaction_async(): await
async_dal.update_async(...)` / `await async_dal.insert_async(...)` shape)
against `test_concurrent_make_available_upserts_never_duplicate` below --
went RED (`AssertionError: assert 2 == 1`, TWO `app_tenant_availability`
rows for the same `(tenant_id, app_id)` pair, since the pre-fix code's
`existing = await async_dal.select_async(...)` read happened BEFORE
entering `transaction_async()`, so N concurrent callers could all
observe "no row yet" and all take the INSERT branch -- the classic
TOCTOU this module's own fix closes by re-deriving the UPDATE-vs-INSERT
decision INSIDE one non-preemptible executor job). Restoring the fixed
`_guarded_upsert_availability_sync`/`_guarded_upsert_activation_sync`
shape turned it GREEN.
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.marketplace_lifecycle_service import activate_bundle, make_available
from tests.conftest import LIFECYCLE_COMMUNITY_ID, LIFECYCLE_TENANT_ID

APP_ID = "acme.widgets.concurrency-test"


def _seed_catalog_row(lifecycle_db: Any, *, app_id: str = APP_ID) -> None:
    """Insert a bare `app_catalog` row directly (bypass `install_bundle()`'s registry side effect).

    `make_available`/`activate_bundle`'s own tier checks
    (`check_availability_insert_allowed`/`check_activation_insert_allowed`)
    only need a real `app_catalog` row with `status='active'` -- they
    never touch the in-memory `AppRegistry` singleton, so this test
    doesn't need `install_bundle()`'s full manifest-parse-and-register
    path (nor the `_clear_registry` autouse fixture the blueprint test
    module carries for that singleton's cross-test bleed).
    """
    dal = lifecycle_db.dal
    dal.app_catalog.insert(
        app_id=app_id,
        name="Concurrency Test Bundle",
        manifest_version="1.0.0",
        module="bot",
        feature="waddles.bot.shoutout",
        provider="builtin",
        execution_model="native",
        is_default=False,
        compatible_with=[],
        incompatible_with=[],
        platform_compatibility={"tested_with": "release/v3.0.X"},
        status="active",
    )
    dal.commit()


class TestConcurrentMakeAvailableNeverDuplicates:
    """`make_available()` upserts on `(tenant_id, app_id)` -- concurrent callers must not race."""

    async def test_concurrent_make_available_upserts_never_duplicate(
        self, lifecycle_db: Any
    ) -> None:
        _seed_catalog_row(lifecycle_db)
        async_dal, dal = lifecycle_db, lifecycle_db.dal

        # 20 concurrent make_available() calls for the SAME (tenant_id,
        # app_id) pair -- with no row existing yet, every one of them
        # independently has to decide UPDATE-vs-INSERT.
        results = await asyncio.gather(
            *[
                make_available(
                    async_dal,
                    dal,
                    tenant_id=LIFECYCLE_TENANT_ID,
                    app_id=APP_ID,
                    config_defaults={"call_index": i},
                )
                for i in range(20)
            ]
        )

        assert all(r is not None for r in results)

        rows = dal(
            (dal.app_tenant_availability.tenant_id == LIFECYCLE_TENANT_ID)
            & (dal.app_tenant_availability.app_id == APP_ID)
        ).select()
        # The critical assertion: exactly ONE row for this (tenant_id,
        # app_id) pair -- never duplicated, never lost.
        assert len(rows) == 1
        assert rows.first().available is True


class TestConcurrentActivateBundleNeverDuplicates:
    """`activate_bundle()` upserts on `(community_id, app_id)` -- same atomicity property."""

    async def test_concurrent_activate_bundle_upserts_never_duplicate(
        self, lifecycle_db: Any
    ) -> None:
        _seed_catalog_row(lifecycle_db)
        async_dal, dal = lifecycle_db, lifecycle_db.dal
        dal.app_tenant_availability.insert(
            tenant_id=LIFECYCLE_TENANT_ID, app_id=APP_ID, available=True, config_defaults={}
        )
        dal.commit()

        results = await asyncio.gather(
            *[
                activate_bundle(
                    async_dal,
                    dal,
                    community_id=LIFECYCLE_COMMUNITY_ID,
                    tenant_id=LIFECYCLE_TENANT_ID,
                    app_id=APP_ID,
                    config={"call_index": i},
                    activated_by=1,
                )
                for i in range(20)
            ]
        )

        assert all(r is not None for r in results)

        rows = dal(
            (dal.app_activations.community_id == LIFECYCLE_COMMUNITY_ID)
            & (dal.app_activations.app_id == APP_ID)
        ).select()
        assert len(rows) == 1
        assert rows.first().enabled is True
