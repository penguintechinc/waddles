"""`blueprints/v1/community_reputation.py` -- read-only score/tier routes (gh-310).

Standalone Quart app registering `reputation_bp` against this file's own
`reputation_db` fixture -- `tests/conftest.py` is not in this task's edit
scope, same rationale as `test_v1_community_loyalty.py`'s own docstring.
Real JWTs via `tests.conftest.make_token`, real pydal queries -- no
mocking of the authz chain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_reputation as reputation_module
from blueprints.v1.community_reputation import reputation_bp
from services import community_reputation_service as reputation_svc
from services.schema import bind_auth_tables
from tests.conftest import TENANT_SLUG, make_token


@pytest.fixture
def reputation_db(tmp_path: Any) -> Any:
    """`(async_dal, community_id, tenant_id)`.

    File-backed `AsyncDAL` with auth + `reputation_tenant` bound.
    """
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'reputation_bp_test.db'}", pool_size=1)
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
    reputation_svc._ensure_reputation_tables(dal, migrate=True)  # noqa: SLF001
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


@pytest.fixture
def app(reputation_db: Any) -> Quart:
    async_dal, _community_id, _tenant_id = reputation_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(reputation_bp)
    quart_app.config["dal"] = async_dal.dal
    quart_app.config["async_dal"] = async_dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.reputation` two-gate Feature flag ON for every test here."""
    monkeypatch.setattr(reputation_module, "feature_enabled", AsyncMock(return_value=True))


def _headers(*, scope: str = "community.reputation:read", user_id: str = "7") -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(scope=scope, user_id=user_id)}"}


def _seed_member(
    dal: Any, *, community_id: int, hub_user_id: int, display_name: str, reputation: int
) -> None:
    dal.community_members.insert(
        community_id=community_id,
        user_id=str(hub_user_id),
        display_name=display_name,
        reputation=reputation,
        is_active=True,
        joined_at=datetime.now(UTC),
    )
    dal.commit()


def _seed_tenant(dal: Any, *, tenant_id: int, hub_user_id: int, score: int) -> None:
    dal.reputation_tenant.insert(
        tenant_id=tenant_id, hub_user_id=hub_user_id, score=score, total_events=3
    )
    dal.commit()


class TestAuthAndTenant:
    async def test_no_token_is_401(self, client: Any, reputation_db: Any) -> None:
        _, community_id, _tenant_id = reputation_db
        response = await client.get(f"/api/v1/community/{community_id}/reputation/me")
        assert response.status_code == 401

    async def test_wrong_scope_is_403(self, client: Any, reputation_db: Any) -> None:
        _, community_id, _tenant_id = reputation_db
        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/me",
            headers=_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get("/api/v1/community/9999/reputation/me", headers=_headers())
        assert response.status_code == 404

    async def test_leaderboard_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/community/9999/reputation/leaderboard", headers=_headers()
        )
        assert response.status_code == 404

    async def test_feature_disabled_is_402(
        self, client: Any, reputation_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, community_id, _tenant_id = reputation_db
        monkeypatch.setattr(reputation_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/me", headers=_headers()
        )
        assert response.status_code == 402


class TestGetMyReputation:
    async def test_shape_and_defaults_when_new_member(
        self, client: Any, reputation_db: Any
    ) -> None:
        _, community_id, _tenant_id = reputation_db
        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/me", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["meta"] == {"version": 1}
        data = body["data"]
        assert data == {
            "community_score": 600,
            "community_tier": "Trusted",
            "tenant_score": 600,
            "tenant_tier": "Trusted",
            "total_events": 0,
            "last_event_at": None,
        }

    async def test_reflects_seeded_scores_and_tiers(self, client: Any, reputation_db: Any) -> None:
        async_dal, community_id, tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=7, display_name="alice", reputation=850
        )
        _seed_tenant(dal, tenant_id=tenant_id, hub_user_id=7, score=300)

        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/me", headers=_headers(user_id="7")
        )
        body = await response.get_json()
        data = body["data"]
        assert data["community_score"] == 850
        assert data["community_tier"] == "Legend"
        assert data["tenant_score"] == 300
        assert data["tenant_tier"] == "Newcomer"

    async def test_tenant_score_never_leaks_from_another_tenant(
        self, client: Any, reputation_db: Any
    ) -> None:
        """A `reputation_tenant` row seeded under a DIFFERENT tenant_id must never surface here."""
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        other_tenant_id = dal.tenants.insert(
            slug="other-tenant-v1", display_name="Other Tenant", is_active=True
        )
        dal.commit()
        _seed_tenant(dal, tenant_id=other_tenant_id, hub_user_id=7, score=850)

        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/me", headers=_headers(user_id="7")
        )
        body = await response.get_json()
        data = body["data"]
        # Caller's own tenant (seeded by the `reputation_db` fixture) has no
        # row for hub_user_id=7 -- baseline, not the other tenant's 850.
        assert data["tenant_score"] == 600
        assert data["tenant_tier"] == "Trusted"


class TestGetLeaderboard:
    async def test_empty_leaderboard(self, client: Any, reputation_db: Any) -> None:
        _, community_id, _tenant_id = reputation_db
        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/leaderboard", headers=_headers()
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["entries"] == []

    async def test_orders_and_shapes_entries(self, client: Any, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        _seed_member(
            dal, community_id=community_id, hub_user_id=1, display_name="low", reputation=400
        )
        _seed_member(
            dal, community_id=community_id, hub_user_id=2, display_name="top", reputation=840
        )

        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/leaderboard", headers=_headers()
        )
        body = await response.get_json()
        entries = body["data"]["entries"]
        assert [e["display_name"] for e in entries] == ["top", "low"]
        assert entries[0] == {"display_name": "top", "score": 840, "tier": "Legend"}
        assert set(entries[0].keys()) == {"display_name", "score", "tier"}

    async def test_limit_query_param_respected(self, client: Any, reputation_db: Any) -> None:
        async_dal, community_id, _tenant_id = reputation_db
        dal = async_dal.dal
        for i in range(3):
            _seed_member(
                dal,
                community_id=community_id,
                hub_user_id=i,
                display_name=f"u{i}",
                reputation=600 + i,
            )
        response = await client.get(
            f"/api/v1/community/{community_id}/reputation/leaderboard?limit=1", headers=_headers()
        )
        body = await response.get_json()
        assert len(body["data"]["entries"]) == 1
