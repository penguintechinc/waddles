"""Tests for `cli/refresh_landing_stats.py` -- the 5-minute landing-stats snapshot writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from cli.refresh_landing_stats import refresh
from services import landing_stats_cache
from tests.conftest import TENANT_SLUG
from tests.test_v1_public_blueprint import FakeValkey, _test_config


async def test_refresh_writes_snapshot_with_computed_at(platform_db: Any) -> None:
    platform_db.dal.communities.insert(
        name="acme",
        display_name="Acme",
        is_active=True,
        is_public=True,
        tenant_id=1,
        created_at=datetime.now(UTC),
    )
    platform_db.dal.coordination.insert(
        entity_id="t1", platform="twitch", server_id="s", is_live=True, viewer_count=7
    )
    platform_db.dal.commit()
    fake = FakeValkey()

    snapshot = await refresh(platform_db, platform_db.dal, _test_config(), fake)

    stored = json.loads(fake.store[landing_stats_cache.SNAPSHOT_KEY])
    assert stored == snapshot
    assert stored["computed_at"]
    assert stored["stats"]["communities"] == 1
    assert stored["stats"]["twitch"] == {"channels": 1, "live": 1, "viewers": 7}
    assert fake.ttls[landing_stats_cache.SNAPSHOT_KEY] == landing_stats_cache.SNAPSHOT_TTL_SECONDS


async def test_refresh_fails_loudly_without_default_tenant(platform_db: Any) -> None:
    platform_db.dal(platform_db.dal.tenants.slug == TENANT_SLUG).update(is_active=False)
    platform_db.dal.commit()
    fake = FakeValkey()
    with pytest.raises(LookupError):
        await refresh(platform_db, platform_db.dal, _test_config(), fake)
    assert fake.store == {}
