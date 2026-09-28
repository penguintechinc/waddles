"""Tests for bundle_version_service's create/get/list_versions() and advance_state()."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from services import bundle_version_service as svc
from services.bundle_component_validator import (
    ComponentValidationResult,
    ComponentValidatorUnavailableError,
)
from services.bundle_version_service import (
    STATUS_ADDRESSING,
    STATUS_INSPECTING,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_REJECTED,
    STATUS_UPLOADED,
    STATUS_VALIDATING,
    advance_state,
    create_version,
    get_version,
    list_versions,
    process_prebuilt_component,
    valid_transition,
)
from services.errors import ApiError

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
}


async def test_create_version_happy_path(install_dal: Any) -> None:
    row = await create_version(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        requested_by=1,
        manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
        source_bytes=b"fake-tarball",
        component_bytes=None,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert row.status == STATUS_UPLOADED
    assert row.version == "3.0.1"


async def test_create_version_rejects_duplicate(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 409


async def test_create_version_rejects_bad_manifest(install_dal: Any) -> None:
    bad_manifest = {**_MANIFEST, "schema_version": 1}
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(bad_manifest).encode(),
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "unsupported_schema_version"


async def test_create_version_rejects_a_manifest_app_id_mismatching_the_url(
    install_dal: Any,
) -> None:
    """The row would store the URL's `app_id` while `manifest_json` keeps the YAML's own.

    An admin scoped to app A must not be able to upload a manifest
    describing app B.
    """
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.forums.default",  # URL says forums...
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),  # ...manifest says music
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "app_id_mismatch"


async def test_create_version_rejects_an_oversize_manifest(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=b"x" * (1_048_576 + 1),
            source_bytes=b"x",
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 413
    assert exc.value.code == "PAYLOAD_TOO_LARGE"


async def test_create_version_rejects_oversize_source(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=b"x" * (16_777_216 + 1),
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 413


async def test_create_version_rejects_prebuilt_when_disallowed(install_dal: Any) -> None:
    prebuilt_manifest = {**_MANIFEST, "artifact": "prebuilt", "language": "other"}
    del prebuilt_manifest["stages"]["process"]["entry"]
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(prebuilt_manifest).encode(),
            source_bytes=None,
            component_bytes=b"x",
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.code == "prebuilt_not_allowed"


async def test_create_version_rejects_missing_source_part(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await create_version(
            install_dal,
            tenant_id=1,
            app_id="waddles.socials.music.default",
            requested_by=1,
            manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
            source_bytes=None,
            component_bytes=None,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "missing_part"


async def test_get_version_not_found_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await get_version(install_dal, app_id="waddles.x.y.default", version="1.0.0")
    assert exc.value.status_code == 404


async def test_list_versions_returns_all_versions_for_app_id(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="2.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    rows = await list_versions(install_dal, app_id="waddles.socials.music.default")
    assert {r.version for r in rows} == {"1.0.0", "2.0.0"}


def test_valid_transition_uploaded_to_validating() -> None:
    assert valid_transition(STATUS_UPLOADED, STATUS_VALIDATING) is True


def test_valid_transition_rejects_skipping_states() -> None:
    assert valid_transition(STATUS_UPLOADED, STATUS_PUBLISHED) is False


def test_valid_transition_terminal_states_have_no_edges() -> None:
    assert valid_transition(STATUS_PUBLISHED, STATUS_VALIDATING) is False
    assert valid_transition(STATUS_REJECTED, STATUS_VALIDATING) is False


def test_valid_transition_addressing_direct_to_published() -> None:
    """Coordinator contract: the prebuilt-component path publishes directly from ADDRESSING."""
    assert valid_transition(STATUS_ADDRESSING, STATUS_PUBLISHED) is True
    assert valid_transition(STATUS_ADDRESSING, STATUS_PUBLISHING) is True  # still legal too


async def test_advance_state_moves_a_row_forward(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    row = await advance_state(
        install_dal,
        app_id="waddles.socials.music.default",
        version="1.0.0",
        target=STATUS_VALIDATING,
    )
    assert row.status == STATUS_VALIDATING


async def test_advance_state_refuses_an_illegal_transition(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await advance_state(
            install_dal,
            app_id="waddles.socials.music.default",
            version="1.0.0",
            target=STATUS_PUBLISHED,
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "invalid_state_transition"


async def test_advance_state_unknown_version_raises_404(install_dal: Any) -> None:
    with pytest.raises(ApiError) as exc:
        await advance_state(
            install_dal, app_id="waddles.x.y.default", version="9.9.9", target=STATUS_VALIDATING
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# STATUS_ABANDONED -- stalled-upload self-healing (fix/seeder-stalled-upload-recovery)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _FakeUploadRow:
    """Minimal stand-in for an `app_version_uploads` row.

    `is_lease_expired()` reads only `updated_at`/`created_at`, so a full DB round-trip
    is unnecessary here.
    """

    updated_at: datetime
    created_at: datetime


@pytest.mark.parametrize(
    "source",
    [
        STATUS_UPLOADED,
        STATUS_VALIDATING,
        svc.STATUS_SCANNING,
        STATUS_INSPECTING,
        svc.STATUS_COMPILING,
        STATUS_ADDRESSING,
        STATUS_PUBLISHING,
    ],
)
def test_valid_transition_every_nonterminal_state_can_be_abandoned(source: str) -> None:
    assert valid_transition(source, svc.STATUS_ABANDONED) is True


def test_valid_transition_abandoned_is_terminal() -> None:
    assert valid_transition(svc.STATUS_ABANDONED, STATUS_VALIDATING) is False


def test_is_terminal_status() -> None:
    assert svc.is_terminal_status(STATUS_PUBLISHED) is True
    assert svc.is_terminal_status(STATUS_REJECTED) is True
    assert svc.is_terminal_status(svc.STATUS_ABANDONED) is True
    assert svc.is_terminal_status(STATUS_INSPECTING) is False


def test_is_lease_expired_false_within_lease() -> None:
    now = datetime.now(UTC)
    row = _FakeUploadRow(updated_at=now, created_at=now)
    assert svc.is_lease_expired(row, lease_seconds=600) is False


def test_is_lease_expired_true_past_lease() -> None:
    stale = datetime.now(UTC) - timedelta(seconds=700)
    row = _FakeUploadRow(updated_at=stale, created_at=stale)
    assert svc.is_lease_expired(row, lease_seconds=600) is True


def test_is_lease_expired_handles_a_naive_datetime() -> None:
    """The sqlite test fixture's own `DateTime` column returns naive datetimes."""
    stale_naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=700)
    row = _FakeUploadRow(updated_at=stale_naive, created_at=stale_naive)
    assert svc.is_lease_expired(row, lease_seconds=600) is True


async def test_abandon_stalled_upload_moves_to_abandoned_and_writes_audit_row(
    install_dal: Any,
) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.core.example.ping",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="prebuilt",
        language="rust",
        status=STATUS_INSPECTING,
        created_at=now,
        updated_at=now,
    )
    row = await svc.abandon_stalled_upload(
        install_dal,
        app_id="waddles.core.example.ping",
        version="1.0.0",
        reason="lease_expired:INSPECTING",
        actor="system:core-seeder",
    )
    assert row.status == svc.STATUS_ABANDONED
    assert row.reject_reason == "lease_expired:INSPECTING"

    audit_rows = await install_dal(
        install_dal.audit_log.action == "app_version_upload_abandoned"
    ).select()
    assert len(audit_rows) == 1
    assert audit_rows.first().target_id == "waddles.core.example.ping@1.0.0"
    assert audit_rows.first().details["actor"] == "system:core-seeder"


async def test_abandon_stalled_upload_refuses_an_already_terminal_row(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.core.example.ping",
        version="1.0.0",
        tenant_id=1,
        artifact_kind="prebuilt",
        language="rust",
        status=STATUS_PUBLISHED,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(ApiError) as exc:
        await svc.abandon_stalled_upload(
            install_dal,
            app_id="waddles.core.example.ping",
            version="1.0.0",
            reason="lease_expired:PUBLISHED",
            actor="system:core-seeder",
        )
    assert exc.value.code == "invalid_state_transition"


async def test_create_version_allows_resubmission_after_a_rejected_row(install_dal: Any) -> None:
    """A REJECTED upload for the same (app_id, version) never blocks a fresh submission."""
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=STATUS_REJECTED,
        reject_reason="wit_conformance_failed",
        created_at=now,
        updated_at=now,
    )
    row = await create_version(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        requested_by=1,
        manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
        source_bytes=b"fresh-tarball",
        component_bytes=None,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert row.status == STATUS_UPLOADED


async def test_create_version_allows_resubmission_after_an_abandoned_row(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status=svc.STATUS_ABANDONED,
        reject_reason="lease_expired:INSPECTING",
        created_at=now,
        updated_at=now,
    )
    row = await create_version(
        install_dal,
        tenant_id=1,
        app_id="waddles.socials.music.default",
        requested_by=1,
        manifest_bytes=yaml.safe_dump(_MANIFEST).encode(),
        source_bytes=b"fresh-tarball",
        component_bytes=None,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert row.status == STATUS_UPLOADED


# ---------------------------------------------------------------------------
# process_prebuilt_component() -- the pre-built-component follow-on
# ---------------------------------------------------------------------------

_COMPONENT_APP_ID = "waddles.vendor.42.mybundle"
_COMPONENT_VERSION = "1.0.0"
_COMPONENT_BYTES = b"fake-wasm-component-bytes"


async def _seed_prebuilt_upload(install_dal: Any) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id=_COMPONENT_APP_ID,
        version=_COMPONENT_VERSION,
        tenant_id=1,
        artifact_kind="prebuilt",
        language="python",
        status=STATUS_UPLOADED,
        created_at=now,
        updated_at=now,
    )


async def test_process_prebuilt_component_happy_path(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_prebuilt_upload(install_dal)
    monkeypatch.setattr(
        svc,
        "validate_component",
        AsyncMock(return_value=ComponentValidationResult(ok=True)),
    )
    expected_digest = hashlib.sha256(_COMPONENT_BYTES).hexdigest()
    _prefix = f"bundles/{_COMPONENT_APP_ID}/{_COMPONENT_VERSION}/{expected_digest}"
    expected_component_key = f"{_prefix}.wasm"
    expected_sidecar_key = f"{_prefix}.json"
    monkeypatch.setattr(
        svc.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value=expected_component_key),
    )
    fake_client = AsyncMock()

    row = await process_prebuilt_component(
        install_dal,
        app_id=_COMPONENT_APP_ID,
        version=_COMPONENT_VERSION,
        component_bytes=_COMPONENT_BYTES,
        tenant_slug="acme",
        valkey_client=fake_client,
    )

    # Coordinator contract: a successfully-staged prebuilt component is
    # published immediately (ADDRESSING -> PUBLISHED), not left at
    # ADDRESSING -- the loader only serves ACTIVE + APPROVED + PUBLISHED.
    assert row.status == STATUS_PUBLISHED

    upload = await get_version(install_dal, app_id=_COMPONENT_APP_ID, version=_COMPONENT_VERSION)
    assert upload.staging_component_key == expected_component_key
    assert upload.app_version_id is not None

    published = (
        await install_dal(install_dal.app_versions.id == upload.app_version_id).select()
    ).first()
    assert published is not None
    assert published.component_key == expected_component_key
    assert published.sidecar_key == expected_sidecar_key
    assert published.artifact_digest == expected_digest
    assert published.artifact_kind == "prebuilt"
    assert published.language == "python"

    # Only the `action`-stage group is provisioned here -- the `process`
    # key is unused (svc-process reads granted source streams instead, see
    # app_source_binding_service.py's own module docstring); regression
    # guard for the removed key stays explicit as a negative assertion.
    assert fake_client.xgroup_create.await_count == 1
    called_streams = {call.args[0] for call in fake_client.xgroup_create.await_args_list}
    assert called_streams == {
        f"waddles:t:acme:c:_tenant:app:{_COMPONENT_APP_ID}:action",
    }
    assert f"waddles:t:acme:c:_tenant:app:{_COMPONENT_APP_ID}:process" not in called_streams
    for call in fake_client.xgroup_create.await_args_list:
        assert call.args[1] == _COMPONENT_APP_ID  # group == app_id
    # caller-supplied client is never closed by process_prebuilt_component itself
    fake_client.aclose.assert_not_called()


async def test_process_prebuilt_component_rejects_nonconformant(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_prebuilt_upload(install_dal)
    monkeypatch.setattr(
        svc,
        "validate_component",
        AsyncMock(
            return_value=ComponentValidationResult(ok=False, reason="disallowed_import:wasi:http/x")
        ),
    )
    upload_spy = AsyncMock()
    monkeypatch.setattr(svc.storage_service, "upload_bundle_component", upload_spy)
    fake_client = AsyncMock()

    row = await process_prebuilt_component(
        install_dal,
        app_id=_COMPONENT_APP_ID,
        version=_COMPONENT_VERSION,
        component_bytes=_COMPONENT_BYTES,
        tenant_slug="acme",
        valkey_client=fake_client,
    )

    assert row.status == STATUS_REJECTED
    assert row.reject_reason is not None and "disallowed_import" in row.reject_reason
    upload_spy.assert_not_called()
    fake_client.xgroup_create.assert_not_called()


async def test_process_prebuilt_component_validator_unavailable_is_503_not_rejected(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_prebuilt_upload(install_dal)
    monkeypatch.setattr(
        svc,
        "validate_component",
        AsyncMock(side_effect=ComponentValidatorUnavailableError("wasm-tools not found")),
    )

    with pytest.raises(ApiError) as exc:
        await process_prebuilt_component(
            install_dal,
            app_id=_COMPONENT_APP_ID,
            version=_COMPONENT_VERSION,
            component_bytes=_COMPONENT_BYTES,
            tenant_slug="acme",
            valkey_client=AsyncMock(),
        )
    assert exc.value.status_code == 503
    assert exc.value.code == "component_validator_unavailable"

    row = await get_version(install_dal, app_id=_COMPONENT_APP_ID, version=_COMPONENT_VERSION)
    assert row.status == STATUS_INSPECTING  # left mid-pipeline, not REJECTED, for a retry


async def test_process_prebuilt_component_is_idempotent_on_busygroup(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-onboarding the same app_id/version tolerates BUSYGROUP via ensure_group's own contract."""
    await _seed_prebuilt_upload(install_dal)
    monkeypatch.setattr(
        svc, "validate_component", AsyncMock(return_value=ComponentValidationResult(ok=True))
    )
    monkeypatch.setattr(
        svc.storage_service,
        "upload_bundle_component",
        AsyncMock(return_value="bundles/x/1.0.0/y.wasm"),
    )
    import redis.exceptions

    fake_client = AsyncMock()
    fake_client.xgroup_create.side_effect = redis.exceptions.ResponseError(
        "BUSYGROUP Consumer Group name already exists"
    )

    # First advance the row to INSPECTING out from under process_prebuilt_component
    # so a second call re-drives it from UPLOADED again is unnecessary -- this
    # asserts the BUSYGROUP tolerance in isolation, matching
    # test_valkey_admin_client.py::test_ensure_group_is_busygroup_tolerant.
    row = await process_prebuilt_component(
        install_dal,
        app_id=_COMPONENT_APP_ID,
        version=_COMPONENT_VERSION,
        component_bytes=_COMPONENT_BYTES,
        tenant_slug="acme",
        valkey_client=fake_client,
    )
    assert row.status == STATUS_PUBLISHED  # never raised despite BUSYGROUP on every call
