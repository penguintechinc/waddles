"""Namespace isolation + install-source gates for marketplace sources (security-critical)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from services import bundle_manifest_v2
from services import vendor_bundle_authz as va
from services.errors import ApiError
from services.marketplace_source_service import BuildMode, SourceRecord, SourceStatus


def _src(*, builtin: bool, pid: uuid.UUID | None = None) -> SourceRecord:
    now = datetime.now(UTC)
    return SourceRecord(
        public_id=pid or uuid.uuid4(),
        name="s",
        is_builtin=builtin,
        git_url=None,
        ref=None,
        pinned_commit_sha=None,
        pending_commit_sha=None,
        build_mode=BuildMode.PREBUILT,
        publisher_key_fingerprint=None,
        publisher_public_key=None,
        sandbox_ack_by=None,
        sandbox_ack_at=None,
        sandbox_ack_text_hash=None,
        sandbox_ack_commit_sha=None,
        status=SourceStatus.ACTIVE,
        last_scanned_at=None,
        last_scan_error=None,
        created_by=None,
        created_at=now,
        updated_at=now,
    )


def test_external_accepts_own_prefix() -> None:
    s = _src(builtin=False)
    va.enforce_source_namespace(
        app_id=f"waddles.integrations.src-{va.source_short(s)}.app", source=s
    )


@pytest.mark.parametrize(
    "app_id",
    [
        "waddles.core.foo.bar",
        "waddles.integrations.src-aaaaaaaaaaaa.x",
        "waddles.integrations.vendor-1.x",
        "waddles.utilities.thing.x",
    ],
)
def test_external_rejects_core_cross_source_and_escape(app_id: str) -> None:
    with pytest.raises(ApiError) as ei:
        va.enforce_source_namespace(app_id=app_id, source=_src(builtin=False))
    assert ei.value.status_code == 403 if hasattr(ei.value, "status_code") else True


def test_external_rejects_bare_prefix() -> None:
    s = _src(builtin=False)
    with pytest.raises(ApiError):
        va.enforce_source_namespace(app_id=va.source_namespace_prefix(va.source_short(s)), source=s)


def test_builtin_may_use_core_only() -> None:
    b = _src(builtin=True)
    va.enforce_source_namespace(app_id="waddles.core.foo.bar", source=b)
    with pytest.raises(ApiError):
        va.enforce_source_namespace(app_id="waddles.integrations.src-x.y", source=b)


def test_unknown_source_fails_closed() -> None:
    with pytest.raises(ApiError):
        va.enforce_source_namespace(app_id="waddles.core.foo.bar", source=None)


def test_install_authority_matrix() -> None:
    va.require_install_authority(
        source=_src(builtin=True), install_source="system:core-seeder", installed_by=None
    )
    va.require_install_authority(source=_src(builtin=False), install_source="human", installed_by=7)
    for src in (_src(builtin=False), None):
        with pytest.raises(ApiError):
            va.require_install_authority(
                source=src, install_source="system:core-seeder", installed_by=None
            )
        with pytest.raises(ApiError):
            va.require_install_authority(source=src, install_source="human", installed_by=None)


@pytest.mark.asyncio
async def test_install_version_globally_refuses_system_for_external_before_db() -> None:
    from services import bundle_approval_service as svc

    with pytest.raises(ApiError):
        await svc.install_version_globally(
            None,  # type: ignore[arg-type]  # gate must fire before any DB access
            app_id="waddles.integrations.src-x.y",
            version="1.0.0",
            installed_by=None,
            install_source="system:core-seeder",
            source=_src(builtin=False),
        )
    with pytest.raises(ApiError):
        await svc.install_version_globally(
            None,  # type: ignore[arg-type]
            app_id="waddles.core.a.b",
            version="1.0.0",
            installed_by=None,
            install_source="system:core-seeder",
            source=None,
        )


def test_app_id_regex_unchanged() -> None:
    assert bundle_manifest_v2._APP_ID_RE.pattern == (
        rf"^waddles\.{bundle_manifest_v2._SEGMENT}\.{bundle_manifest_v2._SEGMENT}"
        rf"\.{bundle_manifest_v2._SEGMENT}$"
    )
