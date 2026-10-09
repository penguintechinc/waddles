"""Failure-path coverage for `cli/seed_core_bundles.py` -- real sqlite DAL, no capability mocks.

Complements `test_seed_core_bundles.py` (happy paths + the main regression cases). Everything
here drives the seeder's fail-loud / fail-contained branches: stall-recovery env parsing and
refusal shapes, the community_id=0 sentinel grant path, per-row reconcile failures, and the
`_run()` outer containment (one platform connection / the whole sweep failing must be counted
and surfaced as a non-zero exit, never swallowed).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from cli import seed_core_bundles as seeder
from cli.seed_core_bundles import reconcile_removed_core_bundles, seed_one
from services.errors import ApiError
from tests.test_seed_core_bundles import (
    _MANIFEST,
    _patch_run_dependencies,
    _patch_validator_and_storage,
    _seed_community,
    _seeded_system_activation,
    _write_bundle,
)

_APP = "waddles.core.example.ping"


class TestStallRecoveryEnv:
    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(seeder.CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV, raising=False)
        assert (
            seeder._core_seeder_stall_recovery_seconds()
            == seeder._DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS
        )

    def test_valid_override_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(seeder.CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV, "7")
        assert seeder._core_seeder_stall_recovery_seconds() == 7

    def test_malformed_override_falls_back_loudly(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(seeder.CORE_SEEDER_STALL_RECOVERY_SECONDS_ENV, "ten")
        with caplog.at_level(logging.WARNING):
            got = seeder._core_seeder_stall_recovery_seconds()
        assert got == seeder._DEFAULT_CORE_SEEDER_STALL_RECOVERY_SECONDS
        assert "invalid" in caplog.text


class TestRecoverStalledCoreUpload:
    async def _insert(self, install_dal: Any, *, status: str, age: timedelta) -> int:
        ts = datetime.now(UTC) - age
        return int(
            await install_dal.app_version_uploads.async_insert(
                app_id=_APP,
                version="1.0.0",
                tenant_id=1,
                artifact_kind="prebuilt",
                language="rust",
                status=status,
                created_at=ts,
                updated_at=ts,
                status_changed_at=ts,
            )
        )

    async def test_no_row_is_not_recoverable(self, install_dal: Any) -> None:
        assert (
            await seeder._recover_stalled_core_upload(install_dal, app_id=_APP, version="1.0.0")
            is False
        )

    async def test_published_row_is_never_reset_even_when_ancient(self, install_dal: Any) -> None:
        await self._insert(install_dal, status="PUBLISHED", age=timedelta(days=3))
        assert (
            await seeder._recover_stalled_core_upload(install_dal, app_id=_APP, version="1.0.0")
            is False
        )
        row = (await install_dal(install_dal.app_version_uploads.app_id == _APP).select()).first()
        assert row.status == "PUBLISHED"

    async def test_fresh_row_inside_grace_window_is_untouched(self, install_dal: Any) -> None:
        await self._insert(install_dal, status="INSPECTING", age=timedelta(seconds=1))
        assert (
            await seeder._recover_stalled_core_upload(install_dal, app_id=_APP, version="1.0.0")
            is False
        )
        row = (await install_dal(install_dal.app_version_uploads.app_id == _APP).select()).first()
        assert row.status == "INSPECTING"

    @pytest.mark.parametrize("status", ["UPLOADED", "VALIDATING", "INSPECTING", "ADDRESSING"])
    async def test_every_recoverable_stalled_status_is_rejected_with_reason(
        self, install_dal: Any, status: str
    ) -> None:
        await self._insert(install_dal, status=status, age=timedelta(minutes=10))
        assert (
            await seeder._recover_stalled_core_upload(install_dal, app_id=_APP, version="1.0.0")
            is True
        )
        row = (await install_dal(install_dal.app_version_uploads.app_id == _APP).select()).first()
        assert row.status == "REJECTED"
        assert status in row.reject_reason

    async def test_retry_failure_after_reset_is_reported_as_stalled_upload(
        self, install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_validator_and_storage(monkeypatch)
        entry = _write_bundle(tmp_path)
        monkeypatch.setattr(
            seeder,
            "create_version",
            AsyncMock(
                side_effect=[ApiError("conflict", 409, "CONFLICT"), ApiError("again", 500, "BOOM")]
            ),
        )
        monkeypatch.setattr(seeder, "_recover_stalled_core_upload", AsyncMock(return_value=True))
        with pytest.raises(ApiError) as excinfo:
            await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
        assert excinfo.value.code == "stalled_core_bundle_upload"
        assert "BOOM" in excinfo.value.message

    async def test_non_conflict_create_error_propagates_unchanged(
        self, install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_validator_and_storage(monkeypatch)
        entry = _write_bundle(tmp_path)
        monkeypatch.setattr(
            seeder, "create_version", AsyncMock(side_effect=ApiError("nope", 422, "INVALID"))
        )
        with pytest.raises(ApiError) as excinfo:
            await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
        assert excinfo.value.code == "INVALID"


class TestSentinelGrantPath:
    async def test_ensure_sentinel_is_idempotent_and_inactive_non_public(
        self, install_dal: Any
    ) -> None:
        await seeder._ensure_sentinel_community(install_dal, tenant_id=1)
        await seeder._ensure_sentinel_community(install_dal, tenant_id=1)
        rows = await install_dal(
            install_dal.communities.id == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
        ).select()
        assert len(rows) == 1
        row = rows.first()
        assert row.name == seeder._SENTINEL_COMMUNITY_NAME
        assert not row.is_active
        assert not row.is_public

    async def test_no_declared_permissions_is_a_noop_and_never_creates_sentinel(
        self, install_dal: Any
    ) -> None:
        grant = AsyncMock()
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(seeder, "grant_community_permissions", grant)
            await seeder._grant_core_bundle_permissions(
                install_dal,
                tenant_id=1,
                community_id=seeder.TENANT_WIDE_COMMUNITY_SENTINEL,
                app_id=_APP,
                version="1.0.0",
                manifest=SimpleNamespace(permission_declarations=()),  # type: ignore[arg-type]
                valkey_client=None,
            )
        finally:
            mp.undo()
        grant.assert_not_awaited()
        sentinel = await install_dal(
            install_dal.communities.id == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
        ).select()
        assert len(sentinel) == 0

    async def test_real_community_scope_does_not_create_sentinel(
        self, install_dal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        community_id = await _seed_community(install_dal)
        grant = AsyncMock()
        monkeypatch.setattr(seeder, "grant_community_permissions", grant)
        manifest = SimpleNamespace(permission_declarations=(SimpleNamespace(id="storage.kv"),))
        await seeder._grant_core_bundle_permissions(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=_APP,
            version="1.0.0",
            manifest=manifest,  # type: ignore[arg-type]
            valkey_client=None,
        )
        grant.assert_awaited_once()
        assert grant.await_args.kwargs["granted_permission_ids"] == frozenset({"storage.kv"})
        assert grant.await_args.kwargs["granted_by"] is None
        sentinel = await install_dal(
            install_dal.communities.id == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
        ).select()
        assert len(sentinel) == 0

    async def test_api_error_during_grant_is_logged_loud_not_raised(
        self,
        install_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            seeder,
            "grant_community_permissions",
            AsyncMock(side_effect=ApiError("denied", 403, "FORBIDDEN")),
        )
        manifest = SimpleNamespace(permission_declarations=(SimpleNamespace(id="storage.kv"),))
        with caplog.at_level(logging.ERROR):
            await seeder._grant_core_bundle_permissions(
                install_dal,
                tenant_id=1,
                community_id=seeder.TENANT_WIDE_COMMUNITY_SENTINEL,
                app_id=_APP,
                version="1.0.0",
                manifest=manifest,  # type: ignore[arg-type]
                valkey_client=None,
            )
        assert "auto-grant failed" in caplog.text
        assert _APP in str(caplog.records[-1].__dict__.get("app_id"))

    async def test_sentinel_failure_is_contained_as_unexpected_error(
        self,
        install_dal: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            seeder, "_ensure_sentinel_community", AsyncMock(side_effect=RuntimeError("fk"))
        )
        manifest = SimpleNamespace(permission_declarations=(SimpleNamespace(id="storage.kv"),))
        with caplog.at_level(logging.ERROR):
            await seeder._grant_core_bundle_permissions(
                install_dal,
                tenant_id=1,
                community_id=seeder.TENANT_WIDE_COMMUNITY_SENTINEL,
                app_id=_APP,
                version="1.0.0",
                manifest=manifest,  # type: ignore[arg-type]
                valkey_client=None,
            )
        assert "RuntimeError: fk" in caplog.text


class TestPermissionParsingThroughSeeder:
    async def test_bare_string_permissions_yield_zero_grants_v2_yields_one(
        self, install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bare-string `permissions: [storage.kv]` parses to NO structured grants (by design).

        V2 `{id, justification}` entries grant. Guards the known-tricky core-manifest
        conversion gap: a bare-string manifest silently seeds with zero grants.
        """
        _patch_validator_and_storage(monkeypatch)
        bare = {**_MANIFEST, "permissions": ["storage.kv"]}
        entry = _write_bundle(tmp_path, manifest=bare)
        await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
        grants = await install_dal(install_dal.community_permission_grants.app_id == _APP).select()
        assert len(grants) == 0

        tmp2 = tmp_path / "v2"
        tmp2.mkdir()
        entry2 = _write_bundle(tmp2, manifest={**_MANIFEST, "app_id": "waddles.core.example.pong"})
        await seed_one(install_dal, entry2, tmp2, valkey_client=AsyncMock())
        grants2 = await install_dal(
            install_dal.community_permission_grants.app_id == "waddles.core.example.pong"
        ).select()
        assert {g.permission_id for g in grants2} == {"storage.kv"}
        assert {g.community_id for g in grants2} == {seeder.TENANT_WIDE_COMMUNITY_SENTINEL}


class TestReconcileFailureContainment:
    async def test_tenant_slug_for_unknown_tenant_fails_loud(self, install_dal: Any) -> None:
        with pytest.raises(ApiError) as excinfo:
            await seeder._tenant_slug_for_id(install_dal, 987654)
        assert excinfo.value.code == "tenant_slug_missing"

    async def test_non_not_found_api_error_counts_as_failure_and_keeps_row(
        self,
        install_dal: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        app_id, community_id = await _seeded_system_activation(install_dal, tmp_path, monkeypatch)
        monkeypatch.setattr(
            seeder,
            "deactivate_for_community",
            AsyncMock(side_effect=ApiError("db", 500, "INTERNAL")),
        )
        with caplog.at_level(logging.ERROR):
            results, failures = await reconcile_removed_core_bundles(
                install_dal, catalog_app_ids=frozenset()
            )
        assert (results, failures) == ([], 1)
        assert "INTERNAL" in caplog.text
        assert app_id in caplog.text
        assert community_id is not None

    async def test_unexpected_exception_counts_as_failure_with_type_in_message(
        self,
        install_dal: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _seeded_system_activation(install_dal, tmp_path, monkeypatch)
        monkeypatch.setattr(
            seeder, "deactivate_for_community", AsyncMock(side_effect=ValueError("weird"))
        )
        with caplog.at_level(logging.ERROR):
            results, failures = await reconcile_removed_core_bundles(
                install_dal, catalog_app_ids=frozenset()
            )
        assert (results, failures) == ([], 1)
        assert "ValueError: weird" in caplog.text

    async def test_not_found_is_an_idempotent_noop(
        self, install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _seeded_system_activation(install_dal, tmp_path, monkeypatch)
        monkeypatch.setattr(
            seeder,
            "deactivate_for_community",
            AsyncMock(side_effect=ApiError("gone", 404, "NOT_FOUND")),
        )
        assert await reconcile_removed_core_bundles(install_dal, catalog_app_ids=frozenset()) == (
            [],
            0,
        )

    async def test_tenant_wide_row_uses_tenant_wide_deactivation(
        self, install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_validator_and_storage(monkeypatch)
        entry = _write_bundle(tmp_path)  # community_id=None -> tenant-wide
        await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
        results, failures = await reconcile_removed_core_bundles(
            install_dal, catalog_app_ids=frozenset()
        )
        assert failures == 0
        assert [r.outcome for r in results] == ["uninstalled_reconcile"]
        assert "community_id=None" in results[0].detail
        remaining = await install_dal(install_dal.app_active_versions.app_id == _APP).select()
        assert len(remaining) == 0


class TestRunOuterContainment:
    def _catalog(self, tmp_path: Path) -> Path:
        path = tmp_path / "core-bundles.yaml"
        path.write_text(yaml.safe_dump({"bundles": []}), encoding="utf-8")
        return path

    async def test_platform_connection_failure_is_counted_and_exit_nonzero(
        self,
        install_dal: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_run_dependencies(install_dal, monkeypatch)
        monkeypatch.setattr(seeder, "load_catalog", lambda p: [])
        monkeypatch.setattr(
            seeder,
            "load_platform_connections",
            lambda p: [SimpleNamespace(platform="twitch", source_id="src-1")],
        )
        monkeypatch.setattr(
            seeder, "seed_platform_connection", AsyncMock(side_effect=RuntimeError("db"))
        )
        with caplog.at_level(logging.ERROR):
            code = await seeder._run(tmp_path, self._catalog(tmp_path))
        assert code == 1
        assert "platform connection failed" in caplog.text

    async def test_whole_sweep_failure_is_surfaced_not_swallowed(
        self,
        install_dal: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_run_dependencies(install_dal, monkeypatch)
        monkeypatch.setattr(seeder, "load_platform_connections", lambda p: [])
        monkeypatch.setattr(
            seeder, "reconcile_removed_core_bundles", AsyncMock(side_effect=RuntimeError("sweep"))
        )
        with caplog.at_level(logging.ERROR):
            code = await seeder._run(tmp_path, self._catalog(tmp_path))
        assert code == 1
        assert "reconcile-uninstall sweep failed" in caplog.text
        assert "RuntimeError: sweep" in caplog.text

    async def test_clean_empty_run_exits_zero_and_logs_denominators(
        self,
        install_dal: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _patch_run_dependencies(install_dal, monkeypatch)
        monkeypatch.setattr(seeder, "load_platform_connections", lambda p: [])
        with caplog.at_level(logging.INFO):
            code = await seeder._run(tmp_path, self._catalog(tmp_path))
        assert code == 0
        assert "examined=0" in caplog.text


def test_main_configures_basic_logging_when_no_handler_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    basic = MagicMock()
    monkeypatch.setattr(logging, "basicConfig", basic)
    monkeypatch.setattr(seeder, "_run", AsyncMock(return_value=0))
    assert seeder.main(["--bundles-dir", str(tmp_path)]) == 0
    basic.assert_called_once()
