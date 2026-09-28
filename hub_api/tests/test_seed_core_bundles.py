"""Tests for `cli/seed_core_bundles.py` -- the SYSTEM-actor core-bundle seeder.

Reuses the `install_dal`/`bundle_install_db` fixtures every other bundle-install test uses
(`tests/conftest.py`) -- same schema, same `TENANT_SLUG` ("acme-corp") seeded tenant row, so a
catalog entry here targets that slug rather than "global" (the seeder never hardcodes a tenant
slug; "global" is only `bundles/core-bundles.yaml`'s own default).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from cli import seed_core_bundles as seeder
from cli.seed_core_bundles import (
    ActivationTarget,
    CatalogEntry,
    CoreBundleSeederError,
    load_catalog,
    seed_one,
)
from services.bundle_component_validator import ComponentValidationResult
from services.bundle_version_service import STATUS_PUBLISHED
from services.errors import ApiError
from tests.conftest import TENANT_SLUG

_MANIFEST: dict[str, Any] = {
    "schema_version": 2,
    "app_id": "waddles.core.example.ping",
    "name": "Ping Example Bundle (Rust)",
    "version": "1.0.0",
    "feature": "waddles.core.example",
    "module": "core",
    "provider": "builtin",
    "language": "rust",
    "artifact": "prebuilt",
    "execution_model": "native",
    "is_default": False,
    "stages": {
        "process": {
            "entry": "waddle:bundle/process-stage#transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
        "action": {"entry": "waddle:bundle/action-stage#dispatch"},
    },
}
_COMPONENT_BYTES = b"fake-wasm-ping-component-bytes"


def _write_bundle(tmp_path: Path, *, manifest: dict[str, Any] = _MANIFEST) -> CatalogEntry:
    (tmp_path / "ping.manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (tmp_path / "ping.wasm").write_bytes(_COMPONENT_BYTES)
    return CatalogEntry(
        app_id=manifest["app_id"],
        version=manifest["version"],
        language="rust",
        manifest_path="ping.manifest.yaml",
        artifact_path="ping.wasm",
        activation_targets=(ActivationTarget(tenant_slug=TENANT_SLUG),),
    )


def _patch_validator_and_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same monkeypatch shape `test_bundle_version_service.py` uses -- no real wasm-tools/MinIO."""
    from services import bundle_version_service as bvs

    monkeypatch.setattr(
        bvs, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    digest = hashlib.sha256(_COMPONENT_BYTES).hexdigest()
    monkeypatch.setattr(
        bvs.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value=f"bundles/waddles.core.example.ping/1.0.0/{digest}.wasm"),
    )


# ---------------------------------------------------------------------------
# HARD GUARD
# ---------------------------------------------------------------------------


async def test_seed_one_refuses_a_non_core_app_id(install_dal: Any, tmp_path: Path) -> None:
    entry = _write_bundle(
        tmp_path, manifest={**_MANIFEST, "app_id": "waddles.integrations.vendor-1.evil"}
    )
    with pytest.raises(CoreBundleSeederError, match="waddles.core."):
        await seed_one(install_dal, entry, tmp_path)


async def test_seed_one_refuses_before_touching_the_database(
    install_dal: Any, tmp_path: Path
) -> None:
    """The HARD GUARD runs before any manifest read/DB write -- no app_catalog row is created."""
    entry = _write_bundle(
        tmp_path, manifest={**_MANIFEST, "app_id": "waddles.integrations.vendor-1.evil"}
    )
    with pytest.raises(CoreBundleSeederError):
        await seed_one(install_dal, entry, tmp_path)
    rows = await install_dal(
        install_dal.app_catalog.app_id == "waddles.integrations.vendor-1.evil"
    ).select()
    assert not rows


@pytest.mark.parametrize(
    "app_id",
    [
        pytest.param("waddles.corex.example.ping", id="near-miss-not-a-dot-boundary"),
        pytest.param("waddles.core.example.pÿng", id="latin-supplement-not-ascii"),
        # Fullwidth 'l' (U+FF4C) NFKC-normalizes to ASCII 'l' -- normalization changes the
        # string, so it is refused outright regardless of what it visually resembles.
        pytest.param("waddles.core.exampｌe.ping", id="nfkc-changing-fullwidth-l"),
        # Cyrillic 'а' (U+0430) is NOT touched by NFKC (different script, not canonically
        # equivalent to Latin 'a') -- caught by the ASCII-only charset regex instead.
        pytest.param("wаddles.core.example.ping", id="cyrillic-homoglyph-a"),
        pytest.param("Waddles.Core.Example.Ping", id="uppercase"),
        pytest.param("waddles..core.example.ping", id="consecutive-dots-empty-segment"),
        pytest.param("waddles.core.example.p_ng", id="underscore-not-in-charset"),
        pytest.param("waddles.core.example.ping.", id="trailing-dot-empty-segment"),
    ],
)
def test_guard_core_namespace_refuses_lookalike_and_malformed_app_ids(app_id: str) -> None:
    with pytest.raises(CoreBundleSeederError):
        seeder._guard_core_namespace(app_id)


def test_guard_core_namespace_accepts_a_well_formed_core_app_id() -> None:
    seeder._guard_core_namespace("waddles.core.example.ping")  # must not raise


# ---------------------------------------------------------------------------
# Happy path: publish + activate under the SYSTEM actor
# ---------------------------------------------------------------------------


async def test_seed_one_publishes_and_activates_under_system_actor(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)

    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    assert [r.outcome for r in results] == ["activated"]

    catalog_row = (
        await install_dal(install_dal.app_catalog.app_id == entry.app_id).select()
    ).first()
    assert catalog_row is not None
    assert catalog_row.module == "core"

    version_row = (
        await install_dal(
            (install_dal.app_versions.app_id == entry.app_id)
            & (install_dal.app_versions.version == entry.version)
        ).select()
    ).first()
    assert version_row is not None
    assert version_row.artifact_kind == "prebuilt"

    upload_row = (
        await install_dal(install_dal.app_version_uploads.app_id == entry.app_id).select()
    ).first()
    assert upload_row.status == STATUS_PUBLISHED
    assert upload_row.requested_by is None  # SYSTEM actor, never a fake hub_users row

    approval_row = (
        await install_dal(install_dal.app_install_approvals.app_id == entry.app_id).select()
    ).first()
    assert approval_row is not None
    assert approval_row.approved_by is None
    assert approval_row.approval_source == "system:core-seeder"

    active_row = (
        await install_dal(
            (install_dal.app_active_versions.app_id == entry.app_id)
            & (install_dal.app_active_versions.community_id == 0)
        ).select()
    ).first()
    assert active_row is not None
    assert active_row.version_id == version_row.id
    assert active_row.activated_by is None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_seed_one_reruns_are_a_no_op(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same app_id+version+digest, already active -> no new approval row, `outcome == "no_op"`."""
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)

    first = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in first] == ["activated"]

    second = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in second] == ["no_op"]

    approvals = await install_dal(install_dal.app_install_approvals.app_id == entry.app_id).select()
    assert len(approvals) == 1, "a no-op re-run must not write a second approval row"

    versions = await install_dal(install_dal.app_versions.app_id == entry.app_id).select()
    assert len(versions) == 1, "a no-op re-run must not publish a second app_versions row"


async def test_seed_one_new_digest_publishes_a_new_version(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bumped catalog version + new artifact activates a NEW version_id, not a no-op."""
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)
    await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    new_manifest = {**_MANIFEST, "version": "1.0.1"}
    (tmp_path / "ping.manifest.yaml").write_text(yaml.safe_dump(new_manifest), encoding="utf-8")
    (tmp_path / "ping.wasm").write_bytes(_COMPONENT_BYTES + b"-v2")
    bumped_entry = CatalogEntry(
        app_id=entry.app_id,
        version="1.0.1",
        language="rust",
        manifest_path="ping.manifest.yaml",
        artifact_path="ping.wasm",
        activation_targets=entry.activation_targets,
    )
    digest = hashlib.sha256(_COMPONENT_BYTES + b"-v2").hexdigest()
    from services import bundle_version_service as bvs

    monkeypatch.setattr(
        bvs.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value=f"bundles/{entry.app_id}/1.0.1/{digest}.wasm"),
    )

    results = await seed_one(install_dal, bumped_entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in results] == ["activated"]

    versions = await install_dal(install_dal.app_versions.app_id == entry.app_id).select()
    assert len(versions) == 2

    active_row = (
        await install_dal(
            (install_dal.app_active_versions.app_id == entry.app_id)
            & (install_dal.app_active_versions.community_id == 0)
        ).select()
    ).first()
    new_version_row = (
        await install_dal(
            (install_dal.app_versions.app_id == entry.app_id)
            & (install_dal.app_versions.version == "1.0.1")
        ).select()
    ).first()
    assert active_row.version_id == new_version_row.id


async def test_seed_one_same_version_different_digest_is_refused(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content drift under an UNCHANGED version string is a conflict, never a silent overwrite."""
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)
    await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    (tmp_path / "ping.wasm").write_bytes(_COMPONENT_BYTES + b"-different-content")

    with pytest.raises(ApiError) as excinfo:
        await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "digest_conflict"


# ---------------------------------------------------------------------------
# Multiple activation targets
# ---------------------------------------------------------------------------


async def test_seed_one_activates_every_configured_target_independently(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_validator_and_storage(monkeypatch)
    other_tenant_id = await install_dal.tenants.async_insert(
        slug="other-tenant", display_name="Other Tenant", is_active=True
    )
    entry = _write_bundle(tmp_path)
    entry = CatalogEntry(
        app_id=entry.app_id,
        version=entry.version,
        language=entry.language,
        manifest_path=entry.manifest_path,
        artifact_path=entry.artifact_path,
        activation_targets=(
            ActivationTarget(tenant_slug=TENANT_SLUG),
            ActivationTarget(tenant_slug="other-tenant"),
        ),
    )

    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in results] == ["activated", "activated"]

    active_rows = await install_dal(install_dal.app_active_versions.app_id == entry.app_id).select()
    assert {r.tenant_id for r in active_rows} == {1, int(other_tenant_id)}


# ---------------------------------------------------------------------------
# load_catalog() -- bundles/core-bundles.yaml's own shape
# ---------------------------------------------------------------------------


def test_load_catalog_parses_the_real_repo_catalog() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    entries = load_catalog(repo_root / "bundles" / "core-bundles.yaml")
    assert {e.app_id for e in entries} == {
        "waddles.core.example.ping",
        "waddles.core.example.pyping",
        "waddles.core.shoutout.default",
    }
    for entry in entries:
        assert entry.activation_targets == (ActivationTarget(tenant_slug="global"),)


def test_load_catalog_defaults_a_missing_activation_targets_to_global(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "bundles": [
                    {
                        "app_id": "waddles.core.example.ping",
                        "version": "1.0.0",
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    entries = load_catalog(catalog_path)
    assert entries[0].activation_targets == (ActivationTarget(tenant_slug="global"),)


# ---------------------------------------------------------------------------
# CORE_BUNDLES_TENANT_SLUGS env override
# ---------------------------------------------------------------------------


def test_env_override_replaces_every_entrys_activation_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORE_BUNDLES_TENANT_SLUGS", "acme-corp, other-corp")
    entry = CatalogEntry(
        app_id="waddles.core.example.ping",
        version="1.0.0",
        language="rust",
        manifest_path="ping.manifest.yaml",
        artifact_path="ping.wasm",
        activation_targets=(ActivationTarget(tenant_slug="global"),),
    )
    resolved = seeder._resolve_activation_targets(entry)
    assert resolved == (
        ActivationTarget(tenant_slug="acme-corp"),
        ActivationTarget(tenant_slug="other-corp"),
    )


# ---------------------------------------------------------------------------
# Error paths -- unknown tenant, catalog/manifest version mismatch, stalled upload
# ---------------------------------------------------------------------------


async def test_resolve_tenant_id_raises_for_an_unknown_tenant_slug(install_dal: Any) -> None:
    with pytest.raises(ApiError) as excinfo:
        await seeder._resolve_tenant_id(install_dal, "does-not-exist")
    assert excinfo.value.code == "tenant_not_found"


async def test_seed_one_refuses_when_catalog_version_does_not_match_manifest(
    install_dal: Any, tmp_path: Path
) -> None:
    entry = _write_bundle(tmp_path)
    mismatched = CatalogEntry(
        app_id=entry.app_id,
        version="9.9.9",
        language=entry.language,
        manifest_path=entry.manifest_path,
        artifact_path=entry.artifact_path,
        activation_targets=entry.activation_targets,
    )
    with pytest.raises(CoreBundleSeederError, match="does not match manifest version"):
        await seed_one(install_dal, mismatched, tmp_path)


async def test_resolve_or_publish_version_reports_a_stalled_upload_clearly(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior crashed run left an app_version_uploads row that never reached PUBLISHED."""
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id=entry.app_id,
        version=entry.version,
        tenant_id=1,
        artifact_kind="prebuilt",
        language="rust",
        status="INSPECTING",
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(ApiError) as excinfo:
        await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert excinfo.value.code == "stalled_core_bundle_upload"


# ---------------------------------------------------------------------------
# _run() -- the batch driver: success + refused + failed outcomes in one pass
# ---------------------------------------------------------------------------


async def test_run_examines_every_catalog_entry_and_reports_a_nonzero_exit_on_any_failure(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_validator_and_storage(monkeypatch)

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(seeder, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(seeder.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())
    # _run() calls seed_one() with no valkey_client override (that param is test-only, see
    # seed_one()'s own docstring) -- process_prebuilt_component()'s action-stage group
    # provisioning would otherwise reach for a real Valkey connection.
    from services import bundle_version_service as bvs_module

    monkeypatch.setattr(bvs_module.valkey_admin_client, "build_client", lambda: AsyncMock())

    _write_bundle(tmp_path)  # ping.manifest.yaml + ping.wasm at tmp_path
    catalog_path = tmp_path / "core-bundles.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "bundles": [
                    {
                        "app_id": "waddles.core.example.ping",
                        "version": "1.0.0",
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    },
                    {
                        "app_id": "waddles.integrations.vendor-1.evil",
                        "version": "1.0.0",
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    },
                    {
                        "app_id": "waddles.core.example.missing",
                        "version": "1.0.0",
                        "language": "rust",
                        "manifest_path": "missing.manifest.yaml",
                        "artifact_path": "missing.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    exit_code = await seeder._run(tmp_path, catalog_path)

    assert exit_code == 1  # non-zero: 2 of 3 bundles failed
    active = await install_dal(
        install_dal.app_active_versions.app_id == "waddles.core.example.ping"
    ).select()
    assert len(active) == 1  # the one good bundle still seeded successfully


# ---------------------------------------------------------------------------
# main() -- CLI wiring
# ---------------------------------------------------------------------------


def test_main_wires_bundles_dir_and_catalog_and_returns_runs_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    async def _fake_run(bundles_dir: Path, catalog_path: Path) -> int:
        captured["bundles_dir"] = bundles_dir
        captured["catalog_path"] = catalog_path
        return 7

    monkeypatch.setattr(seeder, "_run", _fake_run)

    exit_code = seeder.main(["--bundles-dir", str(tmp_path)])

    assert exit_code == 7
    assert captured["bundles_dir"] == tmp_path
    assert captured["catalog_path"] == tmp_path / seeder.DEFAULT_CATALOG_FILENAME


def test_main_honors_an_explicit_catalog_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    async def _fake_run(bundles_dir: Path, catalog_path: Path) -> int:
        captured["catalog_path"] = catalog_path
        return 0

    monkeypatch.setattr(seeder, "_run", _fake_run)
    custom_catalog = tmp_path / "custom.yaml"

    exit_code = seeder.main(["--bundles-dir", str(tmp_path), "--catalog", str(custom_catalog)])

    assert exit_code == 0
    assert captured["catalog_path"] == custom_catalog


# ---------------------------------------------------------------------------
# Platform connections -- registered before bundle activation (see module docstring)
# ---------------------------------------------------------------------------

_PYPING_MANIFEST: dict[str, Any] = {
    "schema_version": 2,
    "app_id": "waddles.core.example.pyping",
    "name": "Ping Example Bundle (Python)",
    "version": "1.0.0",
    "feature": "waddles.core.example",
    "module": "core",
    "provider": "builtin",
    "language": "python",
    "artifact": "prebuilt",
    "execution_model": "native",
    "is_default": False,
    "stages": {
        "process": {
            "entry": "waddle:bundle/process-stage#transform",
            "consumes": [
                {"platform": "twitch", "event_types": ["chat.message"]},
                {"platform": "discord", "event_types": ["chat.message"]},
            ],
        },
        "action": {"entry": "waddle:bundle/action-stage#dispatch"},
    },
}
_PYPING_COMPONENT_BYTES = b"fake-wasm-pyping-component-bytes"


def _write_pyping_bundle(tmp_path: Path) -> CatalogEntry:
    (tmp_path / "pyping.manifest.yaml").write_text(
        yaml.safe_dump(_PYPING_MANIFEST), encoding="utf-8"
    )
    (tmp_path / "pyping.wasm").write_bytes(_PYPING_COMPONENT_BYTES)
    return CatalogEntry(
        app_id=_PYPING_MANIFEST["app_id"],
        version=_PYPING_MANIFEST["version"],
        language="python",
        manifest_path="pyping.manifest.yaml",
        artifact_path="pyping.wasm",
        activation_targets=(ActivationTarget(tenant_slug=TENANT_SLUG),),
    )


def test_load_platform_connections_extends_the_static_catalog_with_the_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = tmp_path / "core-bundles.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "platform_connections": [
                    {
                        "tenant_slug": "global",
                        "platform": "twitch",
                        "source_id": "static-source",
                        "label": "static",
                    }
                ],
                "bundles": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        seeder._PLATFORM_CONNECTIONS_ENV,
        json.dumps(
            [
                {
                    "tenant_slug": "global",
                    "platform": "discord",
                    "source_id": "dg-474965105759748096",
                }
            ]
        ),
    )

    connections = seeder.load_platform_connections(catalog_path)

    assert connections == (
        seeder.PlatformConnection(
            tenant_slug="global", platform="twitch", source_id="static-source", label="static"
        ),
        seeder.PlatformConnection(
            tenant_slug="global",
            platform="discord",
            source_id="dg-474965105759748096",
            label="dg-474965105759748096",
        ),
    )


async def test_seed_platform_connection_is_idempotent(install_dal: Any) -> None:
    connection = seeder.PlatformConnection(
        tenant_slug=TENANT_SLUG,
        platform="discord",
        source_id="dg-474965105759748096",
        label="svc-ingest Discord guild",
    )

    first = await seeder.seed_platform_connection(install_dal, connection)
    assert first.outcome == "created"

    second = await seeder.seed_platform_connection(install_dal, connection)
    assert second.outcome == "no_op"

    rows = await install_dal(
        install_dal.ingest_sources.source_id == "dg-474965105759748096"
    ).select()
    assert len(rows) == 1
    assert rows.first().secret_ciphertext is None


async def test_run_registers_platform_connections_before_activating_a_bundle_that_binds_to_it(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pyping consumes discord -- auto-bind only grants it if the connection registered FIRST."""
    _patch_validator_and_storage(monkeypatch)
    digest = hashlib.sha256(_PYPING_COMPONENT_BYTES).hexdigest()
    from services import bundle_version_service as bvs

    monkeypatch.setattr(
        bvs.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value=f"bundles/waddles.core.example.pyping/1.0.0/{digest}.wasm"),
    )
    monkeypatch.setattr(bvs.valkey_admin_client, "build_client", lambda: AsyncMock())

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(seeder, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(seeder.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())

    _write_pyping_bundle(tmp_path)
    catalog_path = tmp_path / "core-bundles.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "platform_connections": [
                    {
                        "tenant_slug": TENANT_SLUG,
                        "platform": "discord",
                        "source_id": "dg-474965105759748096",
                        "label": "svc-ingest Discord guild",
                    }
                ],
                "bundles": [
                    {
                        "app_id": "waddles.core.example.pyping",
                        "version": "1.0.0",
                        "language": "python",
                        "manifest_path": "pyping.manifest.yaml",
                        "artifact_path": "pyping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = await seeder._run(tmp_path, catalog_path)

    assert exit_code == 0
    connection_row = (
        await install_dal(install_dal.ingest_sources.source_id == "dg-474965105759748096").select()
    ).first()
    assert connection_row is not None
    assert connection_row.secret_ciphertext is None

    bindings = await install_dal(
        install_dal.app_source_bindings.app_id == "waddles.core.example.pyping"
    ).select()
    assert {b.platform for b in bindings} == {"discord"}
    assert bindings.first().source_id == "dg-474965105759748096"
