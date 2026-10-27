"""Marketplace-sources Phase 1, Task 9: the core-bundle seeder as the `is_builtin` source adapter.

Covers the unified path (kill-switch `waddles.disable-builtin-source` unseen/OFF) and the
restored legacy path (ON). The pre-existing seeder suites (`test_seed_core_bundles*.py`) run
against a schema with no `is_builtin` column and stay UNMODIFIED -- they exercise the legacy
(pre-0067 schema) path, which is the behavior-identical default for existing deploys.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import text

from cli import seed_core_bundles as seeder
from cli.seed_core_bundles import (
    BuiltinSource,
    CoreBundleSeederError,
    reconcile_removed_core_bundles,
    seed_one,
)
from services import vendor_bundle_authz
from services.errors import ApiError
from services.marketplace_source_service import get_builtin_source
from tests.conftest import TENANT_SLUG
from tests.test_seed_core_bundles import (
    _MANIFEST,
    _patch_validator_and_storage,
    _seed_community,
    _write_bundle,
)

_SHA = "a" * 40
_APP_ID = "waddles.core.example.ping"

_SOURCES_DDL = """
CREATE TABLE marketplace_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(36) NOT NULL,
    name VARCHAR(100) NOT NULL,
    is_builtin BOOLEAN NOT NULL DEFAULT 0,
    git_url TEXT,
    ref VARCHAR(255),
    pinned_commit_sha CHAR(40),
    pending_commit_sha CHAR(40),
    build_mode SMALLINT NOT NULL,
    publisher_key_fingerprint VARCHAR(128),
    publisher_public_key TEXT,
    sandbox_ack_by INTEGER,
    sandbox_ack_at TIMESTAMP,
    sandbox_ack_text_hash CHAR(64),
    sandbox_ack_commit_sha CHAR(40),
    status SMALLINT NOT NULL DEFAULT 1,
    last_scanned_at TIMESTAMP,
    last_scan_error TEXT,
    created_by INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture
async def source_dal(install_dal: Any) -> Any:
    """`install_dal` with the real 0067 sources shape: builtin (id 1) + external (id 2)."""
    async with install_dal.engine.begin() as conn:
        await conn.execute(text("DROP TABLE marketplace_sources"))
        await conn.execute(text(_SOURCES_DDL))
        await conn.execute(
            text(
                "INSERT INTO marketplace_sources (public_id, name, is_builtin, build_mode, status) "
                "VALUES ('00000000-0000-4000-8000-000000000001', 'Waddles (built-in)', 1, 3, 2)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO marketplace_sources (public_id, name, is_builtin, git_url, ref, "
                "build_mode, status) VALUES ('11111111-1111-4111-8111-111111111111', 'ext', 0, "
                "'https://example.com/r.git', 'main', 2, 2)"
            )
        )
    install_dal.metadata.remove(install_dal.metadata.tables["marketplace_sources"])
    await install_dal.reflect()
    return install_dal


async def _builtin(dal: Any) -> BuiltinSource:
    found = await get_builtin_source(dal)
    assert found is not None
    return BuiltinSource(record=found[0], pk=found[1])


async def _catalog_source_id(dal: Any, app_id: str = _APP_ID) -> Any:
    return (await dal(dal.app_catalog.app_id == app_id).select()).first().source_id


async def _version_rows(dal: Any, app_id: str = _APP_ID) -> list[Any]:
    return list(await dal(dal.app_versions.app_id == app_id).select())


def _patch_run(monkeypatch: pytest.MonkeyPatch, dal: Any) -> None:
    """Same `_run()` harness the existing seeder suite uses (fake DAL builder + config)."""

    async def _fake_build(database_url: str, pool_size: int) -> Any:
        return dal

    class _Cfg:
        database_url = "sqlite://"

    monkeypatch.setattr(seeder, "build_install_dal", _fake_build)
    monkeypatch.setattr(seeder.HubAPIConfig, "from_env", staticmethod(lambda: _Cfg()))
    monkeypatch.setattr(dal, "close", AsyncMock())
    from services import bundle_version_service as bvs

    monkeypatch.setattr(bvs.valkey_admin_client, "build_client", lambda: AsyncMock())
    monkeypatch.setattr(seeder, "feature_enabled", AsyncMock(return_value=False))


def _write_catalog(tmp_path: Path, *, missing: bool = False) -> Path:
    entry = {
        "app_id": _APP_ID,
        "version": "1.0.0",
        "language": "rust",
        "manifest_path": "nope.manifest.yaml" if missing else "ping.manifest.yaml",
        "artifact_path": "nope.wasm" if missing else "ping.wasm",
        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
    }
    path = tmp_path / "core-bundles.yaml"
    path.write_text(yaml.safe_dump({"bundles": [entry]}), encoding="utf-8")
    return path


async def test_seed_one_stamps_builtin_source_id(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row the seeder creates carries the builtin source's numeric id."""
    _patch_validator_and_storage(monkeypatch)
    builtin = await _builtin(source_dal)
    entry = _write_bundle(tmp_path)

    await seed_one(source_dal, entry, tmp_path, valkey_client=AsyncMock(), builtin=builtin)

    assert await _catalog_source_id(source_dal) == builtin.pk == 1
    versions = await _version_rows(source_dal)
    assert [v.source_id for v in versions] == [1]


async def test_seeding_twice_is_idempotent_and_never_restamps(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running neither duplicates rows nor changes the stamped source."""
    _patch_validator_and_storage(monkeypatch)
    builtin = await _builtin(source_dal)
    entry = _write_bundle(tmp_path)

    for _ in range(2):
        await seed_one(source_dal, entry, tmp_path, valkey_client=AsyncMock(), builtin=builtin)

    assert len(await source_dal(source_dal.app_catalog.app_id == _APP_ID).select()) == 1
    assert [v.source_id for v in await _version_rows(source_dal)] == [1]


async def test_backfill_stamps_null_core_rows_once(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-source (NULL) `waddles.core.*` rows are adopted by the builtin; a re-run is a no-op."""
    _patch_validator_and_storage(monkeypatch)
    builtin = await _builtin(source_dal)
    entry = _write_bundle(tmp_path)
    await seed_one(source_dal, entry, tmp_path, valkey_client=AsyncMock())  # legacy: no stamp
    assert await _catalog_source_id(source_dal) is None

    first = await seeder.backfill_builtin_source_id(source_dal, builtin)
    second = await seeder.backfill_builtin_source_id(source_dal, builtin)

    assert first >= 2  # catalog row + version row
    assert second == 0
    assert await _catalog_source_id(source_dal) == 1
    assert [v.source_id for v in await _version_rows(source_dal)] == [1]


async def test_seed_one_fails_loud_when_catalog_names_no_real_bundle(
    source_dal: Any, tmp_path: Path
) -> None:
    """A yaml entry with no bundle on disk is an explicit refusal, never a silent skip."""
    builtin = await _builtin(source_dal)
    entry = _write_bundle(tmp_path)
    ghost = dataclasses.replace(entry, manifest_path="ghost.manifest.yaml")

    with pytest.raises(CoreBundleSeederError, match="no real bundle"):
        await seed_one(source_dal, ghost, tmp_path, builtin=builtin)
    assert not await source_dal(source_dal.app_catalog.app_id == _APP_ID).select()


async def test_seed_one_fails_loud_when_manifest_app_id_differs_from_catalog(
    source_dal: Any, tmp_path: Path
) -> None:
    """The scanned manifest must be the bundle the yaml names."""
    builtin = await _builtin(source_dal)
    entry = _write_bundle(tmp_path, manifest={**_MANIFEST, "app_id": "waddles.core.example.other"})
    mismatched = dataclasses.replace(entry, app_id=_APP_ID)

    with pytest.raises(CoreBundleSeederError, match="no real bundle"):
        await seed_one(source_dal, mismatched, tmp_path, builtin=builtin)


async def test_core_namespace_valid_only_for_the_real_builtin_row(
    source_dal: Any, tmp_path: Path
) -> None:
    """Regression vs the T2 gate, with the REAL seeded row (not the synthetic placeholder)."""
    builtin = await _builtin(source_dal)
    assert builtin.record.is_builtin
    assert builtin.record.public_id.int != 0  # not the all-zero synthetic placeholder
    vendor_bundle_authz.enforce_source_namespace(app_id=_APP_ID, source=builtin.record)

    external = dataclasses.replace(builtin.record, is_builtin=False)
    with pytest.raises(ApiError):
        vendor_bundle_authz.enforce_source_namespace(app_id=_APP_ID, source=external)

    entry = _write_bundle(tmp_path)
    with pytest.raises(ApiError):  # SYSTEM install requires the builtin source
        await seed_one(source_dal, entry, tmp_path, builtin=BuiltinSource(record=external, pk=2))


async def test_reconcile_scoped_to_builtin_leaves_external_and_human_rows(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile deletes only builtin-owned SYSTEM rows -- never external-source/human ones."""
    _patch_validator_and_storage(monkeypatch)
    builtin = await _builtin(source_dal)
    community_id = await _seed_community(source_dal)
    ext_app = "waddles.core.example.extowned"
    human_app = "waddles.core.example.humanowned"
    for app_id in (_APP_ID, ext_app, human_app):
        entry = _write_bundle(
            tmp_path,
            manifest={**_MANIFEST, "app_id": app_id},
            community_id=community_id,
        )
        await seed_one(source_dal, entry, tmp_path, valkey_client=AsyncMock(), builtin=builtin)
    # Re-home one app to the external source; flip another to a human approval.
    async with source_dal.engine.begin() as conn:
        await conn.execute(
            text("UPDATE app_catalog SET source_id = 2 WHERE app_id = :a"), {"a": ext_app}
        )
        await conn.execute(
            text("UPDATE app_install_approvals SET approval_source = 'human' WHERE app_id = :a"),
            {"a": human_app},
        )

    results, failures = await reconcile_removed_core_bundles(
        source_dal, catalog_app_ids=frozenset(), builtin_source_pk=builtin.pk
    )

    assert failures == 0
    assert [r.app_id for r in results] == [_APP_ID]  # only the builtin-owned row


async def test_run_default_path_stamps_source_and_syncs_pinned_sha(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill-switch unseen/OFF: `_run()` resolves the real row, stamps, and records the image SHA."""
    _patch_validator_and_storage(monkeypatch)
    _patch_run(monkeypatch, source_dal)
    monkeypatch.setenv(seeder.COMMIT_SHA_ENV, _SHA)
    _write_bundle(tmp_path)

    exit_code = await seeder._run(tmp_path, _write_catalog(tmp_path))

    assert exit_code == 0
    assert await _catalog_source_id(source_dal) == 1
    found = await get_builtin_source(source_dal)
    assert found is not None and found[0].pinned_commit_sha == _SHA


async def test_run_kill_switch_on_restores_legacy_path(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`waddles.disable-builtin-source` ON: no stamping, no SHA sync -- exactly today's behavior."""
    _patch_validator_and_storage(monkeypatch)
    _patch_run(monkeypatch, source_dal)
    flag = AsyncMock(side_effect=lambda key, **_: key == seeder.FLAG_DISABLE_BUILTIN_SOURCE)
    monkeypatch.setattr(seeder, "feature_enabled", flag)
    monkeypatch.setenv(seeder.COMMIT_SHA_ENV, _SHA)
    _write_bundle(tmp_path)

    exit_code = await seeder._run(tmp_path, _write_catalog(tmp_path))

    assert exit_code == 0
    assert await _catalog_source_id(source_dal) is None
    assert [v.source_id for v in await _version_rows(source_dal)] == [None]
    found = await get_builtin_source(source_dal)
    assert found is not None and found[0].pinned_commit_sha is None


async def test_run_fails_loud_when_a_yaml_entry_has_no_real_bundle(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch driver reports a non-zero exit for a yaml entry with nothing on disk."""
    _patch_run(monkeypatch, source_dal)

    exit_code = await seeder._run(tmp_path, _write_catalog(tmp_path, missing=True))

    assert exit_code == 1
    assert not await source_dal(source_dal.app_catalog.app_id == _APP_ID).select()


async def test_run_fails_loud_when_the_builtin_row_is_missing(
    source_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0067 schema with no builtin row is an integrity error, not a silent legacy fallback."""
    _patch_run(monkeypatch, source_dal)
    async with source_dal.engine.begin() as conn:
        await conn.execute(text("DELETE FROM marketplace_sources WHERE is_builtin = 1"))
    _write_bundle(tmp_path)

    assert await seeder._run(tmp_path, _write_catalog(tmp_path)) == 1
    assert not await source_dal(source_dal.app_catalog.app_id == _APP_ID).select()


async def test_invalid_commit_sha_env_is_ignored_not_written(
    source_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-40-hex env value never reaches the DB (the column CHECK would reject it anyway)."""
    builtin = await _builtin(source_dal)
    monkeypatch.setenv(seeder.COMMIT_SHA_ENV, "latest")

    assert await seeder.sync_builtin_pinned_sha(source_dal, builtin) is False
    monkeypatch.delenv(seeder.COMMIT_SHA_ENV)
    assert await seeder.sync_builtin_pinned_sha(source_dal, builtin) is False
