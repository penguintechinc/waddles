"""`install_bundle()` persists the denormalized `app_catalog.kind` column."""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.app_registry import get_registry

from services.marketplace_lifecycle_service import install_bundle


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    """Clear the process-wide registry singleton around each test."""
    get_registry().clear()
    yield
    get_registry().clear()


def _manifest(app_id: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "app_id": app_id,
        "name": "Kind Test",
        "version": "1.0.0",
        "feature": "waddles.bot.shoutout",
        "module": "bot",
        "provider": "builtin",
        "execution_model": "native",
        "is_default": False,
        "compatible_with": [],
        "incompatible_with": [],
        "platform_compatibility": {"tested_with": "release/v3.0.X"},
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_install_persists_inferred_kind(lifecycle_db: Any) -> None:
    """A manifest without explicit `kind` stores the inferred value, queryable by kind."""
    await install_bundle(lifecycle_db.dal, manifest_data=_manifest("waddles.bot.shoutout.kindtest"))
    dal = lifecycle_db.dal
    row = dal(dal.app_catalog.app_id == "waddles.bot.shoutout.kindtest").select().first()
    assert row.kind == "app"
    assert dal(dal.app_catalog.kind == "app").count() == 1
    assert dal(dal.app_catalog.kind == "service").count() == 0
