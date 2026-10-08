"""Tests for `cli/seed_core_bundles.py` -- the SYSTEM-actor core-bundle seeder.

Reuses the `install_dal`/`bundle_install_db` fixtures every other bundle-install test uses
(`tests/conftest.py`) -- same schema, same `TENANT_SLUG` ("acme-corp") seeded tenant row, so a
catalog entry here targets that slug rather than "global" (the seeder never hardcodes a tenant
slug; "global" is only `bundles/core-bundles.yaml`'s own default).
"""

from __future__ import annotations

import hashlib
import json
import logging
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


async def _seed_community(install_dal: Any, *, tenant_id: int = 1, name: str = "acme") -> int:
    """A `communities` row -- COMMUNITY-tier activation targets need a real community_id."""
    return int(await install_dal.communities.async_insert(tenant_id=tenant_id, name=name))


def _write_bundle(
    tmp_path: Path, *, manifest: dict[str, Any] = _MANIFEST, community_id: int | None = None
) -> CatalogEntry:
    """`community_id`, when given, makes the default target a full COMMUNITY-tier activation.

    `None` (the default) is TENANT-tier availability only under the
    3-tier split -- see `ActivationTarget`'s own docstring.
    """
    (tmp_path / "ping.manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (tmp_path / "ping.wasm").write_bytes(_COMPONENT_BYTES)
    return CatalogEntry(
        app_id=manifest["app_id"],
        version=manifest["version"],
        language="rust",
        manifest_path="ping.manifest.yaml",
        artifact_path="ping.wasm",
        activation_targets=(ActivationTarget(tenant_slug=TENANT_SLUG, community_id=community_id),),
    )


def _patch_validator_and_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same monkeypatch shape `test_bundle_version_service.py` uses -- no real wasm-tools/S3."""
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
    community_id = await _seed_community(install_dal)
    entry = _write_bundle(tmp_path, community_id=community_id)

    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())

    assert [r.outcome for r in results] == ["made_available", "activated"]

    installed_row = (
        await install_dal(install_dal.app_global_installs.app_id == entry.app_id).select()
    ).first()
    assert installed_row is not None
    assert installed_row.installed_by is None  # SYSTEM actor, never a fake hub_users row
    assert installed_row.install_source == "system:core-seeder"

    availability_row = (
        await install_dal(
            (install_dal.bundle_tenant_availability.tenant_id == 1)
            & (install_dal.bundle_tenant_availability.app_id == entry.app_id)
        ).select()
    ).first()
    assert availability_row is not None
    assert availability_row.available is True

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
            & (install_dal.app_active_versions.community_id == community_id)
        ).select()
    ).first()
    assert active_row is not None
    assert active_row.version_id == version_row.id
    assert active_row.activated_by is None


async def test_ensure_app_catalog_row_logs_without_a_reserved_logrecord_key_collision(
    install_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a brand-new `app_catalog` row must log successfully, not crash.

    `_ensure_app_catalog_row()`'s "row created" log call previously passed
    `extra={"module": manifest.module}` -- "module" collides with
    `logging.LogRecord`'s own reserved `module` attribute (the calling module's
    name, always present on every record), so `Logger.makeRecord()` unconditionally
    raises `KeyError: "Attempt to overwrite 'module' in LogRecord"` the instant this
    logger's effective level allows INFO through. That crash was invisible under
    pytest's default logging config (root logger defaults to WARNING, so
    `logger.info(...)`'s `isEnabledFor(INFO)` fast-path skips `makeRecord()`
    entirely) -- but very real under the deployed seeder's `main()`, which calls
    `logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))`, enabling INFO and
    triggering the crash on every genuinely first-time catalog entry (a pre-existing
    row skips this log call entirely via its `if existing: return` early-out, which
    is why a repeat/upgrade install never surfaced it). `caplog.set_level(INFO, ...)`
    below reproduces that same "INFO enabled" condition a plain pytest run would
    otherwise mask.
    """
    _patch_validator_and_storage(monkeypatch)
    caplog.set_level(logging.INFO, logger="waddles.hub_api.core_bundle_seeder")
    entry = _write_bundle(tmp_path)

    # Must not raise -- a fresh app_id's first seed always hits the "row created"
    # log call; before the fix this raised KeyError before ever reaching the DB
    # insert's caller (seed_one), surfacing in _run() as a generic "bundle failed".
    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert results

    catalog_row = (
        await install_dal(install_dal.app_catalog.app_id == entry.app_id).select()
    ).first()
    assert catalog_row is not None
    assert any(
        record.message == "core-bundle-seeder: app_catalog row created" for record in caplog.records
    )


async def test_seed_one_with_no_community_id_activates_tenant_wide(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`community_id=None` (`bundles/core-bundles.yaml`'s own default) activates tenant-wide.

    Regression: seeder skipped activation for community_id null (alpha
    2026-10-02) -- a catalog entry with no `community_id` used to make the
    app available in the tenant's marketplace and then `continue`,
    permanently skipping `app_active_versions`/`app_source_bindings`
    (via `services.bundle_approval_service.activate_tenant_wide()`'s
    schema sentinel split -- see its own docstring). Never silently skip:
    `bundles/core-bundles.yaml`'s every real entry declares
    `community_id: null`, so a skip here means the DB-driven data plane
    never loads waddles.core.* at all.
    """
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)  # community_id=None (default)

    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in results] == ["made_available", "activated"]

    availability_row = (
        await install_dal(
            (install_dal.bundle_tenant_availability.tenant_id == 1)
            & (install_dal.bundle_tenant_availability.app_id == entry.app_id)
        ).select()
    ).first()
    assert availability_row is not None
    assert availability_row.available is True

    active_row = (
        await install_dal(
            (install_dal.app_active_versions.app_id == entry.app_id)
            & (
                install_dal.app_active_versions.community_id
                == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
            )
        ).select()
    ).first()
    assert active_row is not None

    approval_row = (
        await install_dal(install_dal.app_install_approvals.app_id == entry.app_id).select()
    ).first()
    assert approval_row is not None
    assert approval_row.community_id is None
    assert approval_row.approval_source == "system:core-seeder"


async def test_seed_one_with_no_community_id_rerun_is_a_no_op(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant-wide activation has its own no-op check, keyed on the DB sentinel, not `None`."""
    _patch_validator_and_storage(monkeypatch)
    entry = _write_bundle(tmp_path)

    first = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in first] == ["made_available", "activated"]

    second = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in second] == ["no_op"]

    approvals = await install_dal(install_dal.app_install_approvals.app_id == entry.app_id).select()
    assert len(approvals) == 1, "a no-op re-run must not write a second approval row"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_seed_one_reruns_are_a_no_op(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same app_id+version+digest, already active -> no new approval row, `outcome == "no_op"`."""
    _patch_validator_and_storage(monkeypatch)
    community_id = await _seed_community(install_dal)
    entry = _write_bundle(tmp_path, community_id=community_id)

    first = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in first] == ["made_available", "activated"]

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
    community_id = await _seed_community(install_dal)
    entry = _write_bundle(tmp_path, community_id=community_id)
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
            & (install_dal.app_active_versions.community_id == community_id)
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
    # The message is the operator's only signal when this fires from a Helm hook Job log --
    # it must say what happened and exactly what to do, not just carry a machine code.
    assert "ACTION REQUIRED" in excinfo.value.message
    assert "bundles/core-bundles.yaml" in excinfo.value.message
    assert "immutable" in excinfo.value.message


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
    community_id = await _seed_community(install_dal, tenant_id=1, name="acme")
    other_community_id = await _seed_community(
        install_dal, tenant_id=int(other_tenant_id), name="other-community"
    )
    entry = _write_bundle(tmp_path)
    entry = CatalogEntry(
        app_id=entry.app_id,
        version=entry.version,
        language=entry.language,
        manifest_path=entry.manifest_path,
        artifact_path=entry.artifact_path,
        activation_targets=(
            ActivationTarget(tenant_slug=TENANT_SLUG, community_id=community_id),
            ActivationTarget(tenant_slug="other-tenant", community_id=other_community_id),
        ),
    )

    results = await seed_one(install_dal, entry, tmp_path, valkey_client=AsyncMock())
    assert [r.outcome for r in results] == [
        "made_available",
        "activated",
        "made_available",
        "activated",
    ]

    active_rows = await install_dal(install_dal.app_active_versions.app_id == entry.app_id).select()
    assert {r.tenant_id for r in active_rows} == {1, int(other_tenant_id)}


# ---------------------------------------------------------------------------
# load_catalog() -- bundles/core-bundles.yaml's own shape
# ---------------------------------------------------------------------------


def test_load_catalog_parses_the_real_repo_catalog() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    entries = load_catalog(repo_root / "bundles" / "core-bundles.yaml")

    # Subset, not exact-match (fix/seed-catalog-subset-assertion): core-bundles.yaml grows
    # continuously -- the `command` bundle (gh-613-adjacent) and the ~12-bundle bot_process
    # migration queued behind it both add entries here. An exact `==` against a frozen set
    # would red this test on every single addition. This proves every KNOWN core bundle is
    # still present (and the catalog still parses) without forbidding new ones.
    known_app_ids = {
        "waddles.core.example.ping",
        "waddles.core.example.pyping",
        "waddles.core.example.csping",
        # PR batch 1 (2026-10-03): token-safe Python command bundles -- relay/kv only, no
        # handle echoed back, so these ship ahead of the PII-tokenization pipeline #427/#429.
        "waddles.core.example.eightball",
        "waddles.core.example.roll",
        "waddles.core.example.lurk",
        "waddles.core.example.count",
    }
    catalog_app_ids = {e.app_id for e in entries}
    missing = known_app_ids - catalog_app_ids
    assert not missing, f"expected core bundles missing from bundles/core-bundles.yaml: {missing}"

    for entry in entries:
        # ActivationTarget(tenant_slug="global") defaults community_id=None -- every real
        # catalog entry activates tenant-wide, never scoped to one community (seeder.
        # ActivationTarget's own docstring: community_id=None is NOT a skip, see
        # activate_tenant_wide()).
        assert entry.activation_targets == (ActivationTarget(tenant_slug="global"),)

    # regression: csping 1.0.0 is permanently retired (fix/csping-version-bump) -- alpha's
    # app_versions already had a 1.0.0 row published from the pre-#573 Dockerfile.core-bundles
    # (missing the `COPY sdk/waddle-sdk-cs` layer), and #573's fix changed the compiled
    # csping.wasm for that same nominal version, so re-seeding 1.0.0 now 409s as
    # digest_conflict. 1.0.1 is the first version built from the corrected Dockerfile.
    versions_by_app_id = {e.app_id: e.version for e in entries}
    assert versions_by_app_id["waddles.core.example.csping"] == "1.0.1"


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
    # This catalog's own activation_targets carry no community_id -- TENANT-tier
    # availability only (see `ActivationTarget`'s own docstring), not COMMUNITY activation.
    available = await install_dal(
        (install_dal.bundle_tenant_availability.app_id == "waddles.core.example.ping")
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    assert len(available) == 1  # the one good bundle still seeded successfully


def _patch_run_dependencies(install_dal: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same `_run()`-level plumbing `test_run_examines_every_catalog_entry_...` sets up."""

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(seeder, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(seeder.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())
    from services import bundle_version_service as bvs_module

    monkeypatch.setattr(bvs_module.valkey_admin_client, "build_client", lambda: AsyncMock())


async def test_run_rerun_with_the_same_digest_is_a_clean_no_op_at_process_level(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact Helm-hook re-run scenario: same catalog, same artifact, run twice.

    Regression scope: a re-run of an already-seeded core bundle (e.g. a Helm
    post-upgrade hook firing again with no new artifact) must exit 0, never be treated as
    a failure -- see module docstring's Idempotency section.
    """
    _patch_validator_and_storage(monkeypatch)
    _patch_run_dependencies(install_dal, monkeypatch)

    _write_bundle(tmp_path)
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
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    first_exit = await seeder._run(tmp_path, catalog_path)
    second_exit = await seeder._run(tmp_path, catalog_path)

    assert first_exit == 0
    assert second_exit == 0  # idempotent re-run: no-op, not a failure

    versions = await install_dal(
        install_dal.app_versions.app_id == "waddles.core.example.ping"
    ).select()
    assert len(versions) == 1  # never republished


async def test_run_reports_a_clear_digest_conflict_as_a_warning_and_clean_exit(
    install_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """regression: gh-576 -- a digest conflict is logged clearly but no longer fails the Job.

    `digest_conflict` is in `RECOVERABLE_API_ERROR_CODES` (componentize-py's Python bundles
    can legitimately drift byte-for-byte across rebuilds with no source change -- see that
    constant's own docstring); `_run()` now logs it at WARNING with the full operator-facing
    detail and exits 0 rather than blocking every subsequent `helm upgrade` forever on a
    cosmetic artifact-byte mismatch the operator cannot even durably fix (the NEXT rebuild can
    drift again). Guards against the original operator-facing regression too: `str(ApiError
    (...))` renders as a raw `(message, status_code, code)` args tuple (ApiError has no
    `Exception.__init__()` call, see services/errors.py) unless `_run()` special-cases
    `ApiError` and embeds `.message`/`.code` in the RENDERED log line itself -- not only in
    `extra` (which `caplog` captures regardless of formatter, masking the gap in CI; a plain
    `kubectl logs` tail never renders `extra` under this module's bare `logging.basicConfig()`).
    """
    _patch_validator_and_storage(monkeypatch)
    _patch_run_dependencies(install_dal, monkeypatch)

    entry = _write_bundle(tmp_path)
    catalog_path = tmp_path / "core-bundles.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "bundles": [
                    {
                        "app_id": entry.app_id,
                        "version": entry.version,
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    first_exit = await seeder._run(tmp_path, catalog_path)
    assert first_exit == 0

    # Simulate the real alpha incident: a different digest shows up under the SAME
    # catalog version string (e.g. a non-reproducible build drifting between runs).
    (tmp_path / "ping.wasm").write_bytes(_COMPONENT_BYTES + b"-drifted")

    # INFO (not WARNING) -- need both the WARNING conflict line AND the INFO summary line.
    with caplog.at_level("INFO", logger="waddles.hub_api.core_bundle_seeder"):
        second_exit = await seeder._run(tmp_path, catalog_path)

    assert second_exit == 0  # recoverable: never blocks the Helm hook

    conflict_records = [
        r
        for r in caplog.records
        if r.name == "waddles.hub_api.core_bundle_seeder"
        and r.levelname == "WARNING"
        and getattr(r, "error_code", None) == "digest_conflict"
    ]
    assert conflict_records, "expected a logged warning for the conflicting bundle"
    record = conflict_records[0]
    # The app_id, version, code, and message are in the RENDERED message itself -- never
    # only in `extra`.
    rendered = record.getMessage()
    assert entry.app_id in rendered
    assert "digest_conflict" in rendered
    assert "ACTION REQUIRED" in rendered
    assert "DIFFERENT digest" in rendered
    assert record.error_code == "digest_conflict"
    assert record.status_code == 409

    summary_records = [
        r for r in caplog.records if r.getMessage().startswith("core-bundle-seeder: summary")
    ]
    assert summary_records, "expected a summary log line"
    expected = f"{entry.app_id}@{entry.version} (digest_conflict)"
    assert expected in summary_records[-1].skipped_conflicts[0]


async def test_run_a_fatal_api_error_still_fails_the_run_and_other_bundles_still_seed(
    install_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every ApiError code outside RECOVERABLE_API_ERROR_CODES still fails the run.

    Full detail still lands in the rendered line (not only in `extra`), and one bundle's
    failure never stops the remaining catalog entries from being processed.
    """
    _patch_validator_and_storage(monkeypatch)
    _patch_run_dependencies(install_dal, monkeypatch)

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
                        "activation_targets": [{"tenant_slug": "no-such-tenant"}],
                    },
                    {
                        "app_id": "waddles.core.example.ping",
                        "version": "1.0.0",
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("ERROR", logger="waddles.hub_api.core_bundle_seeder"):
        exit_code = await seeder._run(tmp_path, catalog_path)

    assert exit_code == 1  # tenant_not_found is NOT in RECOVERABLE_API_ERROR_CODES

    failure_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "tenant_not_found" in r.getMessage()
    ]
    assert failure_records, "expected the tenant_not_found ApiError logged with full detail"
    assert "no-such-tenant" in failure_records[0].getMessage()

    # The second catalog entry (same bundle, a valid tenant) still seeded despite the first
    # entry's failure -- one bundle's fatal error never aborts the batch.
    available = await install_dal(
        (install_dal.bundle_tenant_availability.app_id == "waddles.core.example.ping")
        & (install_dal.bundle_tenant_availability.available == True)  # noqa: E712
    ).select()
    assert len(available) == 1


async def test_run_logs_the_exception_type_and_message_on_an_unexpected_storage_failure(
    install_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """regression: the alpha `!ping` blocker.

    A boto3 `ClientError` (bad S3 credentials / bucket-grant mismatch, see storage_service's
    `BUNDLE_BUCKET_NAME` split) landed in `_run()`'s generic `except Exception` branch and
    rendered as a bare "core-bundle-seeder: bundle failed" with NO detail under this module's
    own `logging.basicConfig()` (default format drops every `extra` key). The exception's type
    and message must now be in the message string itself, not only in `extra` (which `caplog`
    captures regardless, but a plain `kubectl logs` tail never renders).
    """
    from services import bundle_version_service as bvs

    monkeypatch.setattr(
        bvs, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    monkeypatch.setattr(
        bvs.storage_service,
        "upload_bundle_component",
        AsyncMock(side_effect=ConnectionError("access denied: dummy InvalidAccessKeyId")),
    )
    _patch_run_dependencies(install_dal, monkeypatch)

    entry = _write_bundle(tmp_path)
    catalog_path = tmp_path / "core-bundles.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "bundles": [
                    {
                        "app_id": entry.app_id,
                        "version": entry.version,
                        "language": "rust",
                        "manifest_path": "ping.manifest.yaml",
                        "artifact_path": "ping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level("ERROR", logger="waddles.hub_api.core_bundle_seeder"):
        exit_code = await seeder._run(tmp_path, catalog_path)

    assert exit_code == 1

    failure_records = [
        r
        for r in caplog.records
        if getattr(r, "app_id", None) == entry.app_id and r.levelname == "ERROR"
    ]
    assert failure_records, "expected a logged failure for the storage exception"
    record = failure_records[0]
    # The error type/message are in the rendered message itself -- never only in `extra`.
    assert "ConnectionError" in record.getMessage()
    assert "access denied: dummy InvalidAccessKeyId" in record.getMessage()
    assert record.error_type == "ConnectionError"
    assert record.error == "access denied: dummy InvalidAccessKeyId"


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

    # Auto-bind (app_source_binding_service.sync_bindings()) only runs at COMMUNITY-tier
    # activation under the 3-tier split -- both the platform connection AND the bundle's
    # own activation target need the SAME real community_id (sync_bindings() matches
    # ingest_sources.community_id exactly, no tenant-wide fallback for a real community_id).
    community_id = await _seed_community(install_dal)

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
                        "community_id": community_id,
                    }
                ],
                "bundles": [
                    {
                        "app_id": "waddles.core.example.pyping",
                        "version": "1.0.0",
                        "language": "python",
                        "manifest_path": "pyping.manifest.yaml",
                        "artifact_path": "pyping.wasm",
                        "activation_targets": [
                            {"tenant_slug": TENANT_SLUG, "community_id": community_id}
                        ],
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


async def test_run_registers_platform_connection_and_binds_tenant_wide_to_discord(
    install_dal: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL shape: `bundles/core-bundles.yaml` declares `community_id: null` everywhere.

    Regression: seeder skipped activation for community_id null (alpha
    2026-10-02) -- this is the end-to-end reproduction of the alpha bug,
    `CORE_BUNDLES_PLATFORM_CONNECTIONS`-shaped connection included (the
    exact object shape `k8s/helm/waddlebot/templates/core-bundle-seeder-
    job.yaml` renders from `pipeline.rustDataPlane.svcProcess.
    processIngestPlatform`/`processIngestSourceId`), proving the fix
    activates tenant-wide AND auto-binds the tenant-wide Discord ingest
    source in one `_run()` pass -- no real `communities` row anywhere in
    this test.
    """
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
                        "label": "svc-ingest platform connection",
                        "community_id": None,
                    }
                ],
                "bundles": [
                    {
                        "app_id": "waddles.core.example.pyping",
                        "version": "1.0.0",
                        "language": "python",
                        "manifest_path": "pyping.manifest.yaml",
                        "artifact_path": "pyping.wasm",
                        "activation_targets": [{"tenant_slug": TENANT_SLUG, "community_id": None}],
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
    assert connection_row.community_id is None

    active_row = (
        await install_dal(
            (install_dal.app_active_versions.app_id == "waddles.core.example.pyping")
            & (
                install_dal.app_active_versions.community_id
                == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
            )
        ).select()
    ).first()
    assert active_row is not None

    bindings = await install_dal(
        install_dal.app_source_bindings.app_id == "waddles.core.example.pyping"
    ).select()
    assert {b.platform for b in bindings} == {"discord"}
    assert bindings.first().source_id == "dg-474965105759748096"
    assert bindings.first().community_id == seeder.TENANT_WIDE_COMMUNITY_SENTINEL
