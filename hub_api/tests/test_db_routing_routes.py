"""Route-level proof that split routes read data from the replica and authz from the primary.

Companion to `test_db_routing.py`. The replica in each test is an independent sqlite file that
deliberately holds NO tenant/community authz rows: if a handler resolved tenant, scope or the
community-existence gate on the replica the request would fail, so a 200 carrying replica-only
data proves "authz primary, data replica".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydal import DAL, Field
from quart import Quart
from quart_schema import QuartSchema

from services import db_routing
from services.db_routing import get_read_dals, reset_fallback_warning
from services.schema import bind_auth_tables
from tests.conftest import TENANT_SLUG


@pytest.fixture
def counter() -> Any:
    """Replace the OTel counter with a spy; yields it."""
    spy = MagicMock()
    with patch.object(db_routing, "_reads_counter", spy):
        yield spy


@pytest.fixture(autouse=True)
def _rearm_warning() -> None:
    reset_fallback_warning()


def _targets(spy: Any) -> list[str]:
    return [call.args[1]["target"] for call in spy.add.call_args_list]


def _tenants(dal: DAL) -> None:
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )


def _file_dal(tmp_path: Path, name: str, binders: tuple[Any, ...]) -> DAL:
    """Independent sqlite DAL with `tenants` plus `binders` created (empty)."""
    dal = DAL(f"sqlite://{tmp_path / name}", pool_size=1)
    _tenants(dal)
    for binder in binders:
        binder(dal, migrate=True)
    return dal


def _wrapper(primary: Any, replica: Any | None) -> Any:
    """Minimal `AsyncDAL` stand-in: query-bound select, primary write helpers."""

    async def select_async(query: Any, *args: Any, **kwargs: Any) -> Any:
        return query.select(*args, **kwargs)

    async def insert_async(table: Any, **fields: Any) -> Any:
        new_id = table.insert(**fields)
        primary.commit()
        return new_id

    return SimpleNamespace(
        dal=primary,
        read_dal=replica,
        executor=None,
        select_async=select_async,
        insert_async=insert_async,
    )


class TestReplicaAsyncDalView:
    """`get_read_dals` hands services a read-only wrapper bound entirely to the replica."""

    async def test_view_has_no_write_helpers(self, tmp_path: Path, counter: Any) -> None:
        primary = _file_dal(tmp_path, "p.db", ())
        replica = _file_dal(tmp_path, "r.db", ())
        app = Quart(__name__)
        app.config["dal"] = primary
        app.config["async_dal"] = _wrapper(primary, replica)
        async with app.app_context():
            view, _ = get_read_dals("v")
        for write in ("insert_async", "update_async", "delete_async", "transaction_async"):
            assert not hasattr(view, write), f"replica view must not expose {write}"

    async def test_select_count_and_raw_sql_run_on_replica(
        self, tmp_path: Path, counter: Any
    ) -> None:
        primary = _file_dal(tmp_path, "p.db", ())
        replica = _file_dal(tmp_path, "r.db", ())
        primary.tenants.insert(slug="primary-only")
        primary.commit()
        for slug in ("replica-a", "replica-b"):
            replica.tenants.insert(slug=slug)
        replica.commit()
        app = Quart(__name__)
        app.config["dal"] = primary
        app.config["async_dal"] = _wrapper(primary, replica)
        async with app.app_context():
            view, rdal = get_read_dals("v")
            rows = await view.select_async(rdal(rdal.tenants.id > 0))
            assert sorted(r.slug for r in rows) == ["replica-a", "replica-b"]
            assert await view.count_async(rdal.tenants.id > 0) == 2
            raw = await view.executesql_async("SELECT COUNT(*) FROM tenants")
            assert raw[0][0] == 2

    async def test_no_replica_returns_the_real_primary_wrapper(
        self, tmp_path: Path, counter: Any
    ) -> None:
        primary = _file_dal(tmp_path, "p.db", ())
        app = Quart(__name__)
        app.config["dal"] = primary
        app.config["async_dal"] = _wrapper(primary, None)
        async with app.app_context():
            async_dal, dal = get_read_dals("v")
        assert async_dal is app.config["async_dal"] and dal is primary


class TestSplitRoutes:
    async def test_platform_audit_log_data_from_replica_authz_from_primary(
        self, tmp_path: Path, platform_db: Any, counter: Any
    ) -> None:
        from blueprints.v1.platform import platform_bp
        from services.schema import bind_platform_tables
        from tests.conftest import make_user_token

        platform_db.dal.audit_log.insert(
            user_id=1, action="PRIMARY_ONLY", created_at=datetime.now(UTC)
        )
        platform_db.dal.commit()
        replica = _file_dal(tmp_path, "replica.db", (bind_platform_tables,))
        replica.audit_log.insert(user_id=2, action="REPLICA_ONLY", created_at=datetime.now(UTC))
        replica.commit()

        app = Quart(__name__)
        QuartSchema(app)
        app.register_blueprint(platform_bp)
        app.config["dal"] = platform_db.dal
        app.config["async_dal"] = platform_db
        token = make_user_token(
            user_id=1,
            scope="*:read *:write *:admin *:delete settings:write users:admin",
            tenant=TENANT_SLUG,
        )
        headers = {"Authorization": f"Bearer {token}"}
        client = app.test_client()

        platform_db.read_dal = replica
        try:
            resp = await client.get("/api/v1/platform/audit-log", headers=headers)
            assert resp.status_code == 200
            actions = [e["action"] for e in (await resp.get_json())["entries"]]
            assert actions == ["REPLICA_ONLY"]
        finally:
            platform_db.read_dal = None
        resp = await client.get("/api/v1/platform/audit-log", headers=headers)
        assert [e["action"] for e in (await resp.get_json())["entries"]] == ["PRIMARY_ONLY"]
        assert "replica" in _targets(counter) and "primary_fallback" in _targets(counter)

    async def test_loyalty_internal_leaderboard_gate_primary_data_replica(
        self, tmp_path: Path, counter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import blueprints.v1.community_loyalty as loyalty_module
        from blueprints.v1.community_loyalty import loyalty_internal_bp
        from services.schema import bind_loyalty_tables

        monkeypatch.setenv("SERVICE_API_KEY", "svc-key")
        monkeypatch.setattr(loyalty_module, "feature_enabled", AsyncMock(return_value=True))
        primary = _file_dal(tmp_path, "primary.db", (bind_auth_tables, bind_loyalty_tables))
        tid = primary.tenants.insert(slug=TENANT_SLUG, is_active=True)
        cid = primary.communities.insert(name="c", tenant_id=tid, is_active=True)
        primary.loyalty_balances.insert(
            community_id=cid, platform="twitch", platform_user_id="PRIMARY_USER", balance=5
        )
        primary.commit()
        replica = _file_dal(tmp_path, "replica.db", (bind_auth_tables, bind_loyalty_tables))
        replica.loyalty_balances.insert(
            community_id=cid, platform="twitch", platform_user_id="REPLICA_USER", balance=9
        )
        replica.commit()  # no communities row on the replica: the existence gate must be primary

        app = Quart(__name__)
        QuartSchema(app)
        app.register_blueprint(loyalty_internal_bp)
        app.config["dal"] = primary
        app.config["async_dal"] = _wrapper(primary, replica)

        resp = await app.test_client().get(
            f"/api/v1/internal/loyalty/leaderboard?community_id={cid}",
            headers={"X-Service-Key": "svc-key"},
        )
        assert resp.status_code == 200
        entries = (await resp.get_json())["data"]["entries"]
        assert [e["platform_user_id"] for e in entries] == ["REPLICA_USER"]

    async def test_pricing_and_published_modules_receive_replica(
        self, tmp_path: Path, counter: Any
    ) -> None:
        from blueprints.v1 import marketplace_billing as billing_module
        from blueprints.v1 import marketplace_vendor as vendor_module
        from services.schema import bind_marketplace_vendor_tables

        primary = _file_dal(tmp_path, "p.db", ())
        replica = _file_dal(tmp_path, "r.db", (bind_marketplace_vendor_tables,))
        app = Quart(__name__)
        QuartSchema(app)
        app.config["dal"] = primary
        app.config["async_dal"] = _wrapper(primary, replica)
        app.register_blueprint(billing_module.premium_bp)
        app.register_blueprint(vendor_module.vendor_public_bp)
        client = app.test_client()
        with patch.object(
            billing_module.svc, "get_pricing_config", return_value=(1, 2, 3)
        ) as pricing:
            assert (await client.get("/api/v1/marketplace/premium/pricing")).status_code == 200
        pricing.assert_called_once_with(replica)
        with patch.object(
            vendor_module.review, "get_published_modules", return_value={"modules": []}
        ) as listing:
            resp = await client.get("/api/v1/marketplace/public/vendor/modules")
        assert resp.status_code == 200
        assert listing.call_args.args[0] is replica
