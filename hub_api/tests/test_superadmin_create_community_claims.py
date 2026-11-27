"""`superadmin_service.create_community`: one claim per platform server; names per tenant."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import free_tier_caps, superadmin_service
from services.errors import ApiError


@pytest.fixture(autouse=True)
def _no_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(free_tier_caps, "_caps_apply", AsyncMock(return_value=False))


async def _create(db: Any, tenant_id: int, name: str, server_id: str | None = None) -> Any:
    return await superadmin_service.create_community(
        db,
        db.dal,
        name=name,
        display_name=None,
        description=None,
        platform="discord",
        platform_server_id=server_id,
        is_public=None,
        community_type=None,
        caller_id=1,
        tenant_slug=f"t{tenant_id}",
        tenant_id=tenant_id,
    )


async def test_claimed_server_id_rejected_across_tenants(automation_db: Any) -> None:
    dal = automation_db.dal
    t1 = dal.tenants.insert(slug="t1x", display_name="A", is_active=True)
    t2 = dal.tenants.insert(slug="t2x", display_name="B", is_active=True)
    dal.commit()
    row = await _create(automation_db, t1, "first", "guild-1")
    assert row.platform_server_id == "guild-1"
    with pytest.raises(ApiError) as exc:
        await _create(automation_db, t2, "second", "guild-1")
    assert exc.value.status_code == 409
    assert dal(dal.communities.name == "second").isempty()


async def test_same_name_across_tenants_ok_within_tenant_409(automation_db: Any) -> None:
    dal = automation_db.dal
    t1 = dal.tenants.insert(slug="t1y", display_name="A", is_active=True)
    t2 = dal.tenants.insert(slug="t2y", display_name="B", is_active=True)
    dal.commit()
    await _create(automation_db, t1, "Shared Name")
    await _create(automation_db, t2, "Shared Name")
    with pytest.raises(ApiError) as exc:
        await _create(automation_db, t1, "shared name")
    assert exc.value.status_code == 409
