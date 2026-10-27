"""Prebuilt ingest -> needs-approval -> human approve -> platform re-sign -> install (Task 5)."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import bundle_approval_service as approvals
from services import bundle_signing_service as signing
from services import bundle_version_service as versions
from services import marketplace_source_ingest as ing
from services.bundle_component_validator import ComponentValidationResult
from services.bundle_manifest_v2 import parse_bundle_manifest_v2
from services.errors import ApiError
from services.marketplace_publisher_attestation import build_attestation_payload
from services.marketplace_source_scanner import BundleCandidate, ScanResult
from services.marketplace_source_service import SourceStatus
from services.vendor_bundle_authz import source_namespace_prefix, source_short
from tests.malicious_source_fixtures import make_source, manifest

WASM = b"\x00asm-fake-component"
DIGEST = hashlib.sha256(WASM).hexdigest()
_PUB = Ed25519PrivateKey.generate()
_PUB_B64 = base64.b64encode(_PUB.public_key().public_bytes_raw()).decode()


_DISABLED: set[str] = set()


@pytest.fixture
async def env(install_dal: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Source row + stubs for the external boundaries (WIT validator, bucket, signing key)."""
    monkeypatch.setattr(
        versions, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    monkeypatch.setattr(
        versions.storage_service,
        "upload_bundle_component",
        AsyncMock(side_effect=lambda a, v, d, b: f"bundles/{a}/{v}/{d}.wasm"),
    )
    sidecar = AsyncMock(return_value="sidecar-key")
    monkeypatch.setattr(signing.storage_service, "write_bundle_sidecar", sidecar)
    platform = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "BUNDLE_SIGNING_PRIVATE_KEY", base64.b64encode(platform.private_bytes_raw()).decode()
    )
    monkeypatch.setenv("BUNDLE_SIGNING_KEY_ID", "k1")
    _DISABLED.clear()
    src = dataclasses.replace(make_source("a" * 40), publisher_public_key=_PUB_B64)
    await install_dal.marketplace_sources.async_insert(public_id=str(src.public_id))

    # sqlite mirror stores public_id as text (PG has a native uuid); compare as text here.
    async def _pk(dal: Any, public_id: uuid.UUID) -> int | None:
        rows = await dal(dal.marketplace_sources.public_id == str(public_id)).select()
        row = rows.first()
        return None if row is None else int(row.id)

    async def _get(dal: Any, public_id: uuid.UUID) -> Any:
        rows = await dal(dal.marketplace_sources.public_id == str(public_id)).select()
        row = rows.first()
        if row is None:
            return None
        disabled = str(public_id) in _DISABLED  # sqlite mirror has no status column
        return dataclasses.replace(
            src, status=SourceStatus.DISABLED if disabled else SourceStatus.ACTIVE
        )

    monkeypatch.setattr(ing, "get_source_pk", _pk)
    monkeypatch.setattr(ing, "get_source", _get)
    return src, platform, sidecar


def _candidate(
    src: Any, *, name: str = "hello", version: str = "1.0.0", wasm: bytes = WASM, sign: bool = True
) -> BundleCandidate:
    app_id = f"{source_namespace_prefix(source_short(src))}{name}"
    raw = manifest(app_id, version, permissions=["messages:send"])
    raw["feature"] = app_id.rsplit(".", 1)[0]
    parsed = parse_bundle_manifest_v2(
        raw, known_custom_platforms=frozenset(), allow_wildcard_consumes=False, allow_prebuilt=True
    )
    sig = None
    if sign:
        payload = build_attestation_payload(
            app_id=app_id, version=version, digest=hashlib.sha256(wasm).hexdigest()
        )
        sig = base64.b64encode(_PUB.sign(payload)).decode()
    return BundleCandidate(
        language="python",
        dir_name=name,
        assigned_app_id=app_id,
        version=version,
        manifest=parsed,
        wasm_path=None,
        sidecar_digest=hashlib.sha256(wasm).hexdigest(),
        publisher_sig=sig,
        permission_summary={},
        raw_manifest=raw,
    )


async def _fetch(c: BundleCandidate) -> bytes:
    return WASM


def _scan(src: Any, *cands: BundleCandidate) -> ScanResult:
    return ScanResult(src.public_id, "a" * 40, tuple(cands), (), len(cands))


async def _ingest(install_dal: Any, src: Any, *cands: BundleCandidate) -> ing.IngestSummary:
    return await ing.ingest_candidates(
        install_dal,
        src,
        _scan(src, *cands),
        fetch_wasm=_fetch,
        tenant_id=1,
        tenant_slug="acme",
        valkey_client=AsyncMock(),
    )


async def _vrow(install_dal: Any, app_id: str, version: str = "1.0.0") -> Any:
    return (
        await install_dal(
            (install_dal.app_versions.app_id == app_id)
            & (install_dal.app_versions.version == version)
        ).select()
    ).first()


async def test_valid_attestation_creates_unapproved_version(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    cand = _candidate(src)
    summary = await _ingest(install_dal, src, cand)
    assert (summary.created, summary.rejected) == (1, 0)
    b = summary.bundles[0]
    assert b.outcome == ing.OUTCOME_NEEDS_APPROVAL
    assert b.permission_summary is not None and b.permission_hash
    assert b.key_fingerprint
    row = await _vrow(install_dal, cand.assigned_app_id)
    assert row.source_id is not None and row.artifact_digest == DIGEST
    assert row.artifact_signature is None  # not signed yet
    assert (await approvals.list_global_installs(install_dal)) == []  # NOT auto-installed
    audit = await install_dal(
        install_dal.audit_log.action == "marketplace_prebuilt_attested"
    ).select()
    assert audit.first().details["publisher_key_fingerprint"] == b.key_fingerprint
    assert audit.first().details["wasm_sha256"] == DIGEST


@pytest.mark.parametrize("mode", ["unsigned", "forged", "wrong_digest", "no_key"])
async def test_bad_attestation_creates_no_version(install_dal: Any, env: Any, mode: str) -> None:
    src, _, _ = env
    cand = _candidate(src, sign=mode != "unsigned")
    if mode == "forged":
        forged = Ed25519PrivateKey.generate().sign(b"x")
        cand = dataclasses.replace(cand, publisher_sig=base64.b64encode(forged).decode())
    if mode == "wrong_digest":
        cand = dataclasses.replace(cand, sidecar_digest="0" * 64)
    if mode == "no_key":
        src = dataclasses.replace(src, publisher_public_key=None)
    summary = await _ingest(install_dal, src, cand)
    assert (summary.created, summary.rejected) == (0, 1)
    assert await _vrow(install_dal, cand.assigned_app_id) is None
    uploads = await install_dal(
        install_dal.app_version_uploads.app_id == cand.assigned_app_id
    ).select()
    assert len(uploads) == 0


async def test_core_namespace_from_external_source_rejected(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    cand = dataclasses.replace(_candidate(src), assigned_app_id="waddles.core.evil")
    summary = await _ingest(install_dal, src, cand)
    assert summary.rejected == 1
    assert summary.bundles[0].reason == "namespace_violation"
    assert await _vrow(install_dal, "waddles.core.evil") is None


async def test_same_version_identical_skipped_different_digest_refused(
    install_dal: Any, env: Any
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    again = await _ingest(install_dal, src, cand)
    assert again.skipped_identical == 1 and again.created == 0
    other = b"\x00asm-other"
    changed = _candidate(src, wasm=other)

    async def fetch_other(c: BundleCandidate) -> bytes:
        return other

    refused = await ing.ingest_candidates(
        install_dal,
        src,
        _scan(src, changed),
        fetch_wasm=fetch_other,
        tenant_id=1,
        tenant_slug="acme",
        valkey_client=AsyncMock(),
    )
    assert refused.refused_conflict == 1
    assert (await _vrow(install_dal, cand.assigned_app_id)).artifact_digest == DIGEST


async def test_builtin_or_unknown_source_refused(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    with pytest.raises(ApiError):
        await _ingest(install_dal, dataclasses.replace(src, is_builtin=True))
    ghost = dataclasses.replace(src, public_id=uuid.uuid4())
    with pytest.raises(ApiError) as exc:
        await _ingest(install_dal, ghost)
    assert exc.value.status_code == 404


async def test_approve_resigns_with_platform_key_and_installs(install_dal: Any, env: Any) -> None:
    src, platform, sidecar = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    out = await ing.approve_source_version(
        install_dal,
        src,
        app_id=cand.assigned_app_id,
        version="1.0.0",
        approved_by=7,
        expected_permission_hash=b.permission_hash,
    )
    installs = await approvals.list_global_installs(install_dal)
    assert len(installs) == 1 and installs[0].install_source == "human"
    assert installs[0].installed_by == 7
    row = await _vrow(install_dal, cand.assigned_app_id)
    assert row.artifact_signature == out["signature"] and row.artifact_signature_key_id == "k1"
    payload = signing.build_signing_payload(
        app_id=cand.assigned_app_id, version="1.0.0", digest=DIGEST, approval_id=installs[0].id
    )
    platform.public_key().verify(base64.b64decode(out["signature"]), payload)  # executor check
    sidecar.assert_awaited_once()


async def test_approve_requires_signing_key_and_does_not_install(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY")
    with pytest.raises(ApiError):
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=None,
        )
    assert await approvals.list_global_installs(install_dal) == []


async def test_install_rechecks_namespace_for_external_version(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    other = dataclasses.replace(src, public_id=uuid.uuid4())  # different src- namespace
    with pytest.raises(ApiError):
        await approvals.install_version_globally(
            install_dal,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            installed_by=7,
            source=other,
        )
    with pytest.raises(ApiError):  # SYSTEM auto-install refused for external
        await approvals.install_version_globally(
            install_dal,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            installed_by=None,
            install_source="system:core-seeder",
            source=src,
        )


async def test_approve_by_wrong_source_forbidden(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    other = dataclasses.replace(src, public_id=uuid.uuid4())
    await install_dal.marketplace_sources.async_insert(public_id=str(other.public_id))
    with pytest.raises(ApiError):
        await ing.approve_source_version(
            install_dal,
            other,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=None,
        )


async def _disable(install_dal: Any, src: Any) -> None:
    _DISABLED.add(str(src.public_id))


async def test_disabled_source_ingest_and_approve_refused(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]  # active: normal ingest works
    await _disable(install_dal, src)
    with pytest.raises(ApiError) as exc:
        await _ingest(install_dal, src, _candidate(src, name="other"))
    assert exc.value.status_code == 409
    assert await _vrow(install_dal, _candidate(src, name="other").assigned_app_id) is None
    with pytest.raises(ApiError) as exc2:
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=b.permission_hash,
        )
    assert exc2.value.status_code == 409
    assert (await approvals.list_global_installs(install_dal)) == []


async def test_source_disabled_mid_ingest_creates_no_version(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, _ = env
    cand = _candidate(src)

    async def _fetch_then_disable(c: BundleCandidate) -> bytes:
        await _disable(install_dal, src)  # disabled after the entry check, before create_version
        return WASM

    summary = await ing.ingest_candidates(
        install_dal, src, _scan(src, cand), fetch_wasm=_fetch_then_disable,
        tenant_id=1, tenant_slug="acme", valkey_client=AsyncMock(),
    )  # fmt: skip
    assert (summary.created, summary.rejected) == (0, 1)
    assert await _vrow(install_dal, cand.assigned_app_id) is None
