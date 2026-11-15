"""Primary-vs-read-replica routing (`services/db_routing.py`, `docs/hub-api-db-routing.md`).

One class per routing class from the doc:

* READ-ONLY      -- replica used when configured; primary fallback + a single WARN when not.
* AUTH/SESSION   -- never touches the replica API (runtime + source guard).
* READ-AFTER-WRITE -- modules that write-then-read stay on the primary (source guard).
* WATERMARK      -- the `bundle_active_set` watermark job can never be handed a replica DAL.

Fail-first proof (executed, not narrated): temporarily pointing `get_read_dal()` at the primary
even when `read_dal` is set turned `TestReadOnlyUsesReplica` red; reverting turned it green.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask_core.database import install_db_resilience
from pydal import DAL, Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1 import marketplace_catalog as catalog_module
from blueprints.v1.live_status import live_status_internal_bp, live_status_public_bp
from blueprints.v1.public import public_bp
from services import db_routing
from services.bundle_active_set_watermark_job import BundleActiveSetWatermarkJob
from services.db_routing import (
    HANDLER_MARK,
    DbTarget,
    get_read_dal,
    get_read_dals,
    is_replica_dal,
    mark_replica,
    reads_from_replica,
    record_primary_read,
    reset_fallback_warning,
)
from services.schema import bind_auth_tables, bind_streaming_tables
from tests.conftest import TENANT_SLUG

HUB_API = Path(__file__).resolve().parent.parent
LOGGER = "waddles.hub_api.db_routing"


def _dal(tmp_path: Path, name: str) -> DAL:
    """Return a file-backed sqlite pydal DAL with tenants/auth/streaming tables created."""
    dal = DAL(f"sqlite://{tmp_path / name}", pool_size=1)
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
    bind_streaming_tables(dal, migrate=True)
    return dal


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


def _app(primary: Any, replica: Any | None) -> Quart:
    app = Quart(__name__)
    app.config["dal"] = primary

    async def select_async(query: Any, *args: Any, **kwargs: Any) -> Any:
        """Run on the DAL the query was built from, like `AsyncDAL.select_async`."""
        return query.select(*args, **kwargs)

    app.config["async_dal"] = SimpleNamespace(
        dal=primary, read_dal=replica, select_async=select_async
    )
    return app


class TestReadOnlyUsesReplica:
    async def test_replica_returned_marked_and_counted(self, tmp_path: Path, counter: Any) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        async with _app(primary, replica).app_context():
            got = get_read_dal("unit.route")
        assert got is replica
        assert is_replica_dal(replica) and not is_replica_dal(primary)
        counter.add.assert_called_once_with(1, {"target": "replica", "route": "unit.route"})

    async def test_lazy_binders_run_once_per_dal(self, tmp_path: Path, counter: Any) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        calls: list[Any] = []

        def binder(dal: Any) -> None:
            calls.append(dal)

        async with _app(primary, replica).app_context():
            get_read_dal("a", binders=(binder,))
            get_read_dal("a", binders=(binder,))
        assert calls == [replica]

    async def test_debug_log_names_serving_target(
        self, tmp_path: Path, counter: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        async with _app(primary, replica).app_context():
            get_read_dal("unit.route")
        rec = [r for r in caplog.records if r.message == "db_routing.read"]
        assert len(rec) == 1 and rec[0].target == "replica" and rec[0].route == "unit.route"  # type: ignore[attr-defined]

    async def test_decorator_label_used_and_marker_set(self, tmp_path: Path, counter: Any) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")

        @reads_from_replica("deco.route")
        async def handler() -> Any:
            return get_read_dal()

        async with _app(primary, replica).app_context():
            assert await handler() is replica
        assert getattr(handler, HANDLER_MARK) == "deco.route"
        counter.add.assert_called_once_with(1, {"target": "replica", "route": "deco.route"})

    async def test_get_read_dals_pair(self, tmp_path: Path, counter: Any) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        app = _app(primary, replica)
        async with app.app_context():
            async_dal, dal = get_read_dals("pair")
        assert async_dal is app.config["async_dal"] and dal is replica

    async def test_live_status_route_reads_replica_not_primary(
        self, tmp_path: Path, counter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Community only on primary: replica configured -> 404 proves the replica was read."""
        monkeypatch.setenv("SERVICE_API_KEY", "k")
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        for d in (primary, replica):
            d.tenants.insert(slug=TENANT_SLUG, is_active=True)
            d.commit()
        tid = primary(primary.tenants.slug == TENANT_SLUG).select().first().id
        cid = primary.communities.insert(name="c", tenant_id=tid, is_active=True, is_public=True)
        primary.commit()
        app = _app(primary, replica)
        QuartSchema(app)
        app.register_blueprint(live_status_public_bp)
        app.register_blueprint(live_status_internal_bp)
        client = app.test_client()

        miss = await client.get(f"/api/v1/public/communities/{cid}/live")
        assert miss.status_code == 404

        rtid = replica(replica.tenants.slug == TENANT_SLUG).select().first().id
        replica.communities.insert(id=cid, name="c", tenant_id=rtid, is_active=True, is_public=True)
        replica.commit()
        hit = await client.get(f"/api/v1/public/communities/{cid}/live")
        assert hit.status_code == 200
        assert set(_targets(counter)) == {"replica"}

    async def test_catalog_route_receives_replica_dal(self, tmp_path: Path, counter: Any) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        app = _app(primary, replica)
        QuartSchema(app)
        app.register_blueprint(catalog_module.catalog_bp)
        with patch.object(catalog_module, "get_categories", return_value=[]) as spy:
            resp = await app.test_client().get("/api/v1/marketplace/catalog/categories")
        assert resp.status_code == 200
        spy.assert_called_once_with(replica)


class TestPrimaryFallback:
    async def test_no_replica_falls_back_with_single_warn(
        self, tmp_path: Path, counter: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        primary = _dal(tmp_path, "p.db")
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        async with _app(primary, None).app_context():
            first = get_read_dal("r1")
            second = get_read_dal("r2")
        assert first is primary and second is primary
        assert _targets(counter) == ["primary_fallback", "primary_fallback"]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1 and "DATABASE_READ_REPLICA_URL" in warns[0].getMessage()

    async def test_live_status_route_still_works_without_replica(
        self, tmp_path: Path, counter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SERVICE_API_KEY", "k")
        primary = _dal(tmp_path, "p.db")
        primary.tenants.insert(slug=TENANT_SLUG, is_active=True)
        primary.commit()
        tid = primary(primary.tenants.slug == TENANT_SLUG).select().first().id
        cid = primary.communities.insert(name="c", tenant_id=tid, is_active=True, is_public=True)
        primary.commit()
        app = _app(primary, None)
        QuartSchema(app)
        app.register_blueprint(live_status_public_bp)
        resp = await app.test_client().get(f"/api/v1/public/communities/{cid}/live")
        assert resp.status_code == 200
        assert set(_targets(counter)) == {"primary_fallback"}

    async def test_missing_async_dal_config_uses_raw_dal(
        self, tmp_path: Path, counter: Any
    ) -> None:
        primary = _dal(tmp_path, "p.db")
        app = Quart(__name__)
        app.config["dal"] = primary
        async with app.app_context():
            assert get_read_dal("x") is primary


class TestAuthAndFreshnessPathsNeverUseReplica:
    """Signup-settings gates registration: primary even with a replica configured."""

    async def test_signup_settings_uses_primary_with_replica_configured(
        self, tmp_path: Path, counter: Any
    ) -> None:
        primary, replica = _dal(tmp_path, "p.db"), _dal(tmp_path, "r.db")
        app = _app(primary, replica)
        QuartSchema(app)
        app.register_blueprint(public_bp)
        from blueprints.v1 import public as public_module

        with patch.object(
            public_module.svc, "get_signup_settings", new=AsyncMock(return_value=(True, []))
        ) as spy:
            resp = await app.test_client().get("/api/v1/public/signup-settings")
        assert resp.status_code == 200
        assert spy.call_args.args[1] is primary
        assert _targets(counter) == ["primary"]

    async def test_record_primary_read_counts_primary(self, counter: Any) -> None:
        record_primary_read("auth.login")
        counter.add.assert_called_once_with(1, {"target": "primary", "route": "auth.login"})

    # Modules whose DB access is auth/session/OAuth-state/MFA/rate-limit/idempotency or a
    # write-then-read flow. They must never reference the replica API.
    PRIMARY_ONLY = (
        "blueprints/v1/auth.py",
        "blueprints/v1/sso.py",
        "blueprints/v1/passkey.py",
        "blueprints/v1/access_token.py",
        "blueprints/v1/one_time_secrets.py",
        "blueprints/v1/identity.py",
        "blueprints/v1/profile.py",
        "blueprints/v1/user_management.py",
        "blueprints/v1/admin.py",
        "blueprints/v1/superadmin.py",
        "blueprints/v1/platform.py",
        "blueprints/v1/tenant.py",
        "blueprints/v1/bundle_activation.py",
        "blueprints/v1/bundle_approvals.py",
        "blueprints/v1/join_request.py",
        "blueprints/v1/cookie_consent.py",
        "blueprints/v1/data_privacy.py",
        "services/auth_service.py",
        "services/oauth_service.py",
        "services/session_service.py",
        "services/session_cookie.py",
        "services/passkey_service.py",
        "services/sso_service.py",
        "services/access_token_service.py",
        "services/rate_limiting.py",
        "services/community_authz.py",
        "services/tenant_service.py",
    )

    @pytest.mark.parametrize("rel", PRIMARY_ONLY)
    def test_primary_only_module_never_references_replica_api(self, rel: str) -> None:
        path = HUB_API / rel
        assert path.is_file(), f"guard points at a missing file: {rel}"
        src = path.read_text()
        assert not re.search(
            r"db_routing|get_read_dal|reads_from_replica|\.read_dal\b|read_replica", src
        ), f"{rel} is classified primary-only but references the replica API"


class TestMarkedHandlersAreReadOnly:
    def test_every_marked_handler_is_get_only_and_enumerated(self) -> None:
        from app import create_app
        from tests.telemetry_support import _test_config

        app = create_app(_test_config())
        marked = {
            rule.endpoint: rule.methods
            for rule in app.url_map.iter_rules()
            if getattr(app.view_functions[rule.endpoint], HANDLER_MARK, None)
        }
        assert len(marked) >= 14, f"expected the migrated read set, found {len(marked)}"
        for endpoint, methods in marked.items():
            assert set(methods or ()) <= {
                "GET",
                "HEAD",
                "OPTIONS",
            }, f"{endpoint} is @reads_from_replica but accepts {methods}"


class TestWatermarkAlwaysPrimary:
    def test_job_rejects_a_replica_marked_dal(self) -> None:
        replica_like = SimpleNamespace()
        mark_replica(replica_like)
        with pytest.raises(ValueError, match="PRIMARY"):
            BundleActiveSetWatermarkJob(replica_like)  # type: ignore[arg-type]

    def test_job_accepts_an_unmarked_primary_dal(self) -> None:
        BundleActiveSetWatermarkJob(SimpleNamespace())  # type: ignore[arg-type]

    def test_job_module_never_references_replica_routing_api(self) -> None:
        src = (HUB_API / "services/bundle_active_set_watermark_job.py").read_text()
        for needle in ("get_read_dal", "reads_from_replica", "read_dal", "read_replica"):
            assert needle not in src, f"watermark job references {needle}"

    def test_app_builds_job_from_primary_install_dal_only(self) -> None:
        src = (HUB_API / "app.py").read_text()
        assert "build_install_dal(cfg.database_url" in src
        assert "BundleActiveSetWatermarkJob(install_dal)" in src
        assert (
            "database_read_replica_url"
            not in src.split("build_install_dal(")[1].split("BundleActiveSetWatermarkJob(")[0]
        )


class TestReplicaTeardown:
    async def test_replica_connection_is_committed_after_each_request(self) -> None:
        replica = MagicMock()
        app = Quart(__name__)
        app.config["read_dal"] = replica
        install_db_resilience(app, dal_key="read_dal")

        @app.route("/ping")
        async def ping() -> str:
            return "ok"

        resp = await app.test_client().get("/ping")
        assert resp.status_code == 200
        replica.commit.assert_called_once()


def test_target_enum_values() -> None:
    assert {t.value for t in DbTarget} == {"replica", "primary_fallback", "primary"}
