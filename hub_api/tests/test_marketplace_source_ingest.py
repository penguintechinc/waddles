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

    async def _by_pk(dal: Any, pk: int) -> Any:
        row = (await dal(dal.marketplace_sources.id == pk).select()).first()
        return None if row is None else await _get(dal, uuid.UUID(str(row.public_id)))

    async def _lock(conn: Any, dal: Any, public_id: uuid.UUID) -> SourceStatus | None:
        """Sqlite mirror: no status column / FOR UPDATE; honour the _DISABLED stand-in."""
        if str(public_id) in _DISABLED:
            return SourceStatus.DISABLED
        return SourceStatus.ACTIVE

    monkeypatch.setattr(approvals, "lock_source_status", _lock)
    monkeypatch.setattr(ing, "get_source_by_pk", _by_pk)
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
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY")
    with pytest.raises(ApiError):
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=b.permission_hash,
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
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    other = dataclasses.replace(src, public_id=uuid.uuid4())
    await install_dal.marketplace_sources.async_insert(public_id=str(other.public_id))
    with pytest.raises(ApiError) as exc:
        await ing.approve_source_version(
            install_dal,
            other,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=b.permission_hash,
        )
    assert exc.value.status_code == 403


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


async def test_disable_between_status_read_and_install_refused(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (TOCTOU): a disable landing after approve's status re-read must not install.

    The install txn re-checks status under the source-row lock, so the late disable wins.
    """
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    real_pk = ing.get_source_pk

    async def _pk_then_disable(dal: Any, public_id: uuid.UUID) -> int | None:
        result = await real_pk(dal, public_id)  # runs AFTER the status read, BEFORE the install
        await _disable(install_dal, src)
        return result

    monkeypatch.setattr(ing, "get_source_pk", _pk_then_disable)
    with pytest.raises(ApiError) as exc:
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=b.permission_hash,
        )
    assert exc.value.status_code == 409
    assert (await approvals.list_global_installs(install_dal)) == []


def test_source_lock_statement_is_for_update() -> None:
    """The approve/disable serialization lock must compile to `SELECT ... FOR UPDATE` on PG."""
    from sqlalchemy import Column, Integer, MetaData, Table
    from sqlalchemy.dialects import postgresql

    from services.marketplace_source_service import lock_source_status_stmt

    t = Table(
        "marketplace_sources", MetaData(), Column("status", Integer), Column("public_id", Integer)
    )
    sql = str(lock_source_status_stmt(t, 1).compile(dialect=postgresql.dialect()))  # type: ignore[arg-type]
    assert sql.rstrip().endswith("FOR UPDATE") and "marketplace_sources" in sql


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


# --- generic /bundle_approvals approve route must be source-aware (bypass regression) ---------


@pytest.fixture
async def route_app(bundle_install_db: Any, install_dal: Any) -> Any:
    """Quart app serving the GENERIC approve route against the sqlite install DAL."""
    from quart import Quart
    from quart_schema import QuartSchema

    from blueprints.v1.bundle_approvals import BLUEPRINTS

    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def _route_approve(app: Any, app_id: str, version: str = "1.0.0", **body: Any) -> Any:
    from tests.conftest import make_token

    token = make_token(scope="platform:admin", user_id="7")
    return await app.test_client().post(
        f"/api/v1/apps/{app_id}/versions/{version}/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"permissionHash": body.get("permissionHash")},
    )


async def _audit_actions(install_dal: Any) -> list[str]:
    return [r.action for r in await install_dal(install_dal.audit_log.id > 0).select()]


async def test_generic_route_runs_marketplace_gates_for_sourced_version(
    install_dal: Any, env: Any, route_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: the generic approve route skipped every marketplace gate for source_id rows."""
    src, platform, sidecar = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    recorded: list[str] = []
    monkeypatch.setattr(
        ing.mkt_telemetry, "record_approval", lambda *, outcome: recorded.append(outcome)
    )
    sign_calls = AsyncMock(wraps=signing.sign_and_record_version)
    monkeypatch.setattr(signing, "sign_and_record_version", sign_calls)

    resp = await _route_approve(route_app, cand.assigned_app_id, permissionHash=b.permission_hash)
    assert resp.status_code == 200, await resp.get_json()

    installs = await approvals.list_global_installs(install_dal)
    assert len(installs) == 1 and installs[0].install_source == "human"
    assert installs[0].installed_by == 7
    actions = await _audit_actions(install_dal)
    assert "marketplace_version_approved" in actions
    assert "marketplace_signature_issued" in actions
    assert recorded == ["signed"]
    # signed EXACTLY once, bound to the global-install id the executor verifies
    assert sign_calls.await_count == 1
    assert sign_calls.await_args.kwargs["approval_id"] == installs[0].id
    sidecar.assert_awaited_once()
    row = await _vrow(install_dal, cand.assigned_app_id)
    assert row.artifact_signature_key_id == "k1"
    # no auto-grant of permissions for a non-builtin source
    assert list(await install_dal(install_dal.app_permission_grant_versions.id > 0).select()) == []


async def test_generic_route_refuses_disabled_source_version(
    install_dal: Any, env: Any, route_app: Any
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    await _disable(install_dal, src)
    resp = await _route_approve(route_app, cand.assigned_app_id, permissionHash=b.permission_hash)
    assert resp.status_code == 409
    assert await approvals.list_global_installs(install_dal) == []
    assert "marketplace_version_approved" not in await _audit_actions(install_dal)


async def test_generic_route_sourced_requires_platform_key_before_install(
    install_dal: Any, env: Any, route_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY")
    resp = await _route_approve(route_app, cand.assigned_app_id, permissionHash=b.permission_hash)
    assert resp.status_code >= 400
    assert await approvals.list_global_installs(install_dal) == []


async def test_generic_route_sourced_namespace_enforced(
    install_dal: Any, env: Any, route_app: Any
) -> None:
    """A sourced version whose app_id left its source namespace is refused at install."""
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    await install_dal(install_dal.app_versions.app_id == cand.assigned_app_id).update(
        app_id="waddles.core.evil"
    )
    await install_dal(install_dal.app_version_uploads.app_id == cand.assigned_app_id).update(
        app_id="waddles.core.evil"
    )
    resp = await _route_approve(route_app, "waddles.core.evil", permissionHash="0" * 64)
    assert resp.status_code in (403, 409)
    assert await approvals.list_global_installs(install_dal) == []


async def test_generic_route_legacy_version_unchanged(
    install_dal: Any, env: Any, route_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source_id NULL: legacy branch -- no marketplace audit/metric, no signing at approve."""
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    await install_dal(install_dal.app_versions.app_id == cand.assigned_app_id).update(
        source_id=None
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        ing.mkt_telemetry, "record_approval", lambda *, outcome: recorded.append(outcome)
    )
    sign_calls = AsyncMock(wraps=signing.sign_and_record_version)
    monkeypatch.setattr(signing, "sign_and_record_version", sign_calls)
    resp = await _route_approve(route_app, cand.assigned_app_id)
    assert resp.status_code == 200
    installs = await approvals.list_global_installs(install_dal)
    assert len(installs) == 1 and installs[0].install_source == "human"
    actions = await _audit_actions(install_dal)
    assert "app_installed_globally" in actions
    assert "marketplace_version_approved" not in actions
    assert "marketplace_signature_issued" not in actions
    assert recorded == [] and sign_calls.await_count == 0  # legacy signs at activation, not here


async def test_resolve_approval_source_routing(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    app_id = cand.assigned_app_id
    got = await ing.resolve_approval_source(install_dal, app_id=app_id, version="1.0.0")
    assert got is not None and got.public_id == src.public_id
    # unknown version -> legacy path (which 404s itself)
    assert await ing.resolve_approval_source(install_dal, app_id=app_id, version="9.9.9") is None

    async def _builtin(dal: Any, pk: int) -> Any:
        return dataclasses.replace(src, is_builtin=True)

    monkeypatch.setattr(ing, "get_source_by_pk", _builtin)
    assert await ing.resolve_approval_source(install_dal, app_id=app_id, version="1.0.0") is None

    async def _missing(dal: Any, pk: int) -> None:
        return None

    monkeypatch.setattr(ing, "get_source_by_pk", _missing)
    with pytest.raises(ApiError) as exc:  # fail closed, never the ungated legacy path
        await ing.resolve_approval_source(install_dal, app_id=app_id, version="1.0.0")
    assert exc.value.status_code == 409


# --- H2: external approvals must be bound to the reviewed permission hash ----------------------


async def test_external_approve_without_permission_hash_refused(
    install_dal: Any, env: Any, route_app: Any
) -> None:
    """regression: permissionHash=None skipped the consent binding for source_id versions."""
    src, _, _ = env
    cand = _candidate(src)
    await _ingest(install_dal, src, cand)
    with pytest.raises(ApiError) as exc:
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=None,
        )
    assert (exc.value.status_code, exc.value.code) == (422, "permission_hash_required")
    resp = await _route_approve(route_app, cand.assigned_app_id)
    assert resp.status_code == 422
    assert await approvals.list_global_installs(install_dal) == []


async def test_external_approve_stale_hash_refused_correct_hash_ok(
    install_dal: Any, env: Any, route_app: Any
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    resp = await _route_approve(route_app, cand.assigned_app_id, permissionHash="f" * 64)
    assert resp.status_code == 409
    assert await approvals.list_global_installs(install_dal) == []
    resp = await _route_approve(route_app, cand.assigned_app_id, permissionHash=b.permission_hash)
    assert resp.status_code == 200
    assert len(await approvals.list_global_installs(install_dal)) == 1


# --- H3: a sign failure must not strand the app with no installed version ----------------------


async def test_sign_failure_restores_prior_install(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: failure uninstalled_globally() the app, losing the prior good version."""
    src, _, _ = env
    c1, c2 = _candidate(src), _candidate(src, version="1.1.0")
    b1 = (await _ingest(install_dal, src, c1)).bundles[0]
    b2 = (await _ingest(install_dal, src, c2)).bundles[0]
    kw = {"app_id": c1.assigned_app_id, "approved_by": 7}
    await ing.approve_source_version(
        install_dal, src, version="1.0.0", expected_permission_hash=b1.permission_hash, **kw
    )
    (good,) = await approvals.list_global_installs(install_dal)
    uninstall = AsyncMock(wraps=approvals.uninstall_globally)
    monkeypatch.setattr(approvals, "uninstall_globally", uninstall)
    monkeypatch.setattr(
        signing, "upload_signed_sidecar", AsyncMock(side_effect=RuntimeError("bucket down"))
    )
    with pytest.raises(RuntimeError):
        await ing.approve_source_version(
            install_dal, src, version="1.1.0", expected_permission_hash=b2.permission_hash, **kw
        )
    uninstall.assert_not_awaited()  # tenants/communities never cascaded
    current = [r for r in await approvals.list_global_installs(install_dal) if r.revoked_at is None]
    assert [(r.id, r.version, r.revoked_at) for r in current] == [(good.id, "1.0.0", None)]
    failed = await _vrow(install_dal, c2.assigned_app_id, "1.1.0")
    assert failed.approval_id is None
    rows = await install_dal(install_dal.app_global_installs.version == "1.1.0").select()
    assert all(r.revoked_at is not None for r in rows)


async def test_sign_failure_first_install_leaves_nothing_installed(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    monkeypatch.setattr(
        signing, "upload_signed_sidecar", AsyncMock(side_effect=RuntimeError("bucket down"))
    )
    with pytest.raises(RuntimeError):
        await ing.approve_source_version(
            install_dal,
            src,
            app_id=cand.assigned_app_id,
            version="1.0.0",
            approved_by=7,
            expected_permission_hash=b.permission_hash,
        )
    rows = await install_dal(install_dal.app_global_installs.id > 0).select()
    assert len(rows) == 1 and rows.first().revoked_at is not None


# --- H3b: rollback must keep <=1 current install per app and a consistent row ------------------


async def _three_versions(install_dal: Any, src: Any) -> tuple[Any, Any, Any, Any]:
    cs = [_candidate(src, version=v) for v in ("1.0.0", "1.1.0", "1.2.0")]
    bs = [(await _ingest(install_dal, src, c)).bundles[0] for c in cs]
    return cs[0].assigned_app_id, cs, bs, {"approved_by": 7}


async def _current(install_dal: Any) -> list[Any]:
    rows = await install_dal(install_dal.app_global_installs.id > 0).select()
    return [r for r in rows if r.superseded_by is None and r.revoked_at is None]


async def test_rollback_after_concurrent_supersede_keeps_exactly_one_current(
    install_dal: Any, env: Any
) -> None:
    """regression: A rolled back after B superseded A un-superseded P -> P and B both current."""
    src, _, _ = env
    app_id, cs, bs, kw = await _three_versions(install_dal, src)
    p = await approvals.install_version_globally(
        install_dal, app_id=app_id, version="1.0.0", installed_by=7,
        expected_permission_hash=bs[0].permission_hash, source=src,
    )  # fmt: skip
    a = await approvals.install_version_globally(
        install_dal, app_id=app_id, version="1.1.0", installed_by=7,
        expected_permission_hash=bs[1].permission_hash, source=src,
    )  # fmt: skip
    b = await approvals.install_version_globally(
        install_dal, app_id=app_id, version="1.2.0", installed_by=7,
        expected_permission_hash=bs[2].permission_hash, source=src,
    )  # fmt: skip
    vrow = await _vrow(install_dal, app_id, "1.1.0")
    await ing._rollback_install(
        install_dal, new_install_id=int(a.id), version_id=int(vrow.id), revoked_by=7
    )
    cur = await _current(install_dal)
    assert [r.id for r in cur] == [b.id]
    rows = {r.id: r for r in await install_dal(install_dal.app_global_installs.id > 0).select()}
    assert rows[p.id].superseded_by is not None  # P not resurrected
    assert rows[a.id].revoked_at is not None


async def test_rollback_does_not_resurrect_revoked_prior(install_dal: Any, env: Any) -> None:
    src, _, _ = env
    app_id, cs, bs, kw = await _three_versions(install_dal, src)
    p = await approvals.install_version_globally(
        install_dal, app_id=app_id, version="1.0.0", installed_by=7,
        expected_permission_hash=bs[0].permission_hash, source=src,
    )  # fmt: skip
    a = await approvals.install_version_globally(
        install_dal, app_id=app_id, version="1.1.0", installed_by=7,
        expected_permission_hash=bs[1].permission_hash, source=src,
    )  # fmt: skip
    from datetime import UTC, datetime

    await install_dal(install_dal.app_global_installs.id == p.id).update(
        revoked_at=datetime.now(UTC), revoked_by=9
    )
    vrow = await _vrow(install_dal, app_id, "1.1.0")
    await ing._rollback_install(
        install_dal, new_install_id=int(a.id), version_id=int(vrow.id), revoked_by=7
    )
    assert await _current(install_dal) == []


async def test_rollback_clears_stale_signature_fields(
    install_dal: Any, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regression: rollback left artifact_signed_approval_id pointing at the revoked approval."""
    src, _, _ = env
    cand = _candidate(src)
    b = (await _ingest(install_dal, src, cand)).bundles[0]
    monkeypatch.setattr(
        signing, "upload_signed_sidecar", AsyncMock(side_effect=RuntimeError("bucket down"))
    )
    with pytest.raises(RuntimeError):
        await ing.approve_source_version(
            install_dal, src, app_id=cand.assigned_app_id, version="1.0.0",
            approved_by=7, expected_permission_hash=b.permission_hash,
        )  # fmt: skip
    v = await _vrow(install_dal, cand.assigned_app_id)
    assert v.approval_id is None
    assert v.artifact_signature is None
    assert v.artifact_signature_key_id is None
    assert v.artifact_signed_approval_id is None
    assert v.artifact_signed_at is None
