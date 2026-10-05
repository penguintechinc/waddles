"""`services/community_reputation_service.py` -- direct service-layer tests (gh-310).

Own fixture (`reputation_db`, this file only -- `hub_api/services/
community_reputation_service.py` and `hub_api/blueprints/v1/
community_reputation.py` are the only files this task may edit, `tests/
conftest.py` is not), mirroring `test_services_community_loyalty.py`'s
`loyalty_db` shape: real `AsyncDAL` (file-backed sqlite, `pool_size=1`),
real `bind_auth_tables()` + this module's own `_ensure_reputation_tables()`
(exercising the actual binding code path production uses, not a
hand-duplicated Field list).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from flask_core.database import AsyncDAL
from pydal import Field

from services import community_reputation_service as svc
from services.schema import bind_auth_tables

TENANT_SLUG = "acme-corp"


@pytest.fixture
def reputation_db(tmp_path: Any) -> Any:
    """`(async_dal, community_id, tenant_id)`.

    File-backed `AsyncDAL` with auth + `reputation_tenant` bound.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'reputation_service_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    svc._ensure_reputation_tables(dal, migrate=True)  # noqa: SLF001 - exercising real bind path
    tenant_id = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    community_id = dal.communities.insert(
        name="test-community", tenant_id=tenant_id, is_active=True
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal, community_id, tenant_id
    dal.close()


def _seed_member(
    dal: Any,
    *,
    community_id: int,
    hub_user_id: int,
    display_name: str,
    reputation: int,
    active: bool = True,
) -> None:
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(hub_user_id),
        display_name=display_name,
        reputation=reputation,
        is_active=active,
        joined_at=datetime.now(UTC),
    )
    dal.commit()


def _seed_tenant(
    dal: Any, *, tenant_id: int, hub_user_id: int, score: int, total_events: int = 5
) -> None:
    dal.reputation_tenant.insert(
        tenant_id=tenant_id,
        hub_user_id=hub_user_id,
        score=score,
        total_events=total_events,
        last_event_at=datetime.now(UTC),
    )
    dal.commit()


class TestReputationTier:
    """`reputation_tier()` boundary coverage -- see module docstring for the rescaled table."""

    @pytest.mark.parametrize(
        "score,expected",
        [
            (300, "Newcomer"),
            (464, "Newcomer"),
            (465, "Regular"),
            (574, "Regular"),
            (575, "Trusted"),
            (600, "Trusted"),  # REPUTATION_DEFAULT
            (657, "Trusted"),
            (658, "Respected"),
            (739, "Respected"),
            (740, "Champion"),
            (794, "Champion"),
            (795, "Legend"),
            (850, "Legend"),  # REPUTATION_MAX
        ],
    )
    def test_boundaries(self, score: int, expected: str) -> None:
        assert svc.reputation_tier(score) == expected


class TestGetMyReputation:
    async def test_defaults_to_baseline_when_no_rows_exist(self, reputation_db: Any) -> None:
        async_dal, community_id, tenant_id = reputation_db
        dal = async_dal.dal
        result = await svc.get_my_reputation(
            async_dal, dal, community_id=community_id, hub_user_id=42, tenant_id=tenant_id
        )
        assert result.community_score == 600
        assert result.community_tier == "Trusted"
        assert result.tenant_score == 600
        assert result.tenant_tier == "Trusted"
        assert result.total_events == 0
        assert result.last_event_at is None

    async def test_reads_both_scores_when_present(self, reputation_db: Any) -> None:
        async_dal, community_id, tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=7, display_name="alice", reputation=720
        )
        _seed_tenant(dal, tenant_id=tenant_id, hub_user_id=7, score=820, total_events=12)

        result = await svc.get_my_reputation(
            async_dal, dal, community_id=community_id, hub_user_id=7, tenant_id=tenant_id
        )
        assert result.community_score == 720
        assert result.community_tier == "Respected"
        assert result.tenant_score == 820
        assert result.tenant_tier == "Legend"
        assert result.total_events == 12
        assert result.last_event_at is not None

    async def test_community_member_without_tenant_row_defaults_tenant_only(
        self, reputation_db: Any
    ) -> None:
        async_dal, community_id, tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=9, display_name="bob", reputation=300
        )

        result = await svc.get_my_reputation(
            async_dal, dal, community_id=community_id, hub_user_id=9, tenant_id=tenant_id
        )
        assert result.community_score == 300
        assert result.community_tier == "Newcomer"
        assert result.tenant_score == 600
        assert result.tenant_tier == "Trusted"

    async def test_tenant_score_never_leaks_from_another_tenant(self, reputation_db: Any) -> None:
        """A `reputation_tenant` row seeded under a DIFFERENT tenant_id must never surface here."""
        async_dal, community_id, tenant_id = reputation_db
        dal = async_dal.dal
        other_tenant_id = dal.tenants.insert(
            slug="other-tenant", display_name="Other Tenant", is_active=True
        )
        dal.commit()
        _seed_tenant(dal, tenant_id=other_tenant_id, hub_user_id=99, score=850, total_events=40)

        result = await svc.get_my_reputation(
            async_dal, dal, community_id=community_id, hub_user_id=99, tenant_id=tenant_id
        )
        # Caller's own tenant has no row for hub_user_id=99 -- baseline, not
        # the other tenant's 850/Legend.
        assert result.tenant_score == 600
        assert result.tenant_tier == "Trusted"


class TestGetLeaderboard:
    async def test_empty_community_returns_empty_list(self, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        entries = await svc.get_leaderboard(async_dal, dal, community_id=community_id)
        assert entries == []

    async def test_orders_highest_first_and_includes_tier(self, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=1, display_name="low", reputation=350
        )
        _seed_member(
            dal, community_id=community_id, hub_user_id=2, display_name="high", reputation=830
        )
        _seed_member(
            dal, community_id=community_id, hub_user_id=3, display_name="mid", reputation=600
        )

        entries = await svc.get_leaderboard(async_dal, dal, community_id=community_id)
        assert [e.display_name for e in entries] == ["high", "mid", "low"]
        assert entries[0].tier == "Legend"
        assert entries[1].tier == "Trusted"
        assert entries[2].tier == "Newcomer"

    async def test_excludes_inactive_members(self, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal,
            community_id=community_id,
            hub_user_id=1,
            display_name="gone",
            reputation=850,
            active=False,
        )
        _seed_member(
            dal, community_id=community_id, hub_user_id=2, display_name="here", reputation=500
        )

        entries = await svc.get_leaderboard(async_dal, dal, community_id=community_id)
        assert [e.display_name for e in entries] == ["here"]

    async def test_no_pii_fields_on_entry(self, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=1, display_name="alice", reputation=600
        )
        entries = await svc.get_leaderboard(async_dal, dal, community_id=community_id)
        field_names = {f.name for f in entries[0].__dataclass_fields__.values()}
        assert field_names == {"display_name", "score", "tier"}

    async def test_limit_is_clamped(self, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        for i in range(5):
            _seed_member(
                dal,
                community_id=community_id,
                hub_user_id=i,
                display_name=f"u{i}",
                reputation=600 + i,
            )
        entries = await svc.get_leaderboard(async_dal, dal, community_id=community_id, limit=2)
        assert len(entries) == 2
