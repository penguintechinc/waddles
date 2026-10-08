"""Tests for `cli/reconcile_signed_sidecars.py` -- the scheduled sidecar-repair CLI.

MEDIUM security-review finding on `feature/bundle-artifact-signing`:
`sign_approved_bundles.py` never revisits a row that IS signed in Postgres
but whose bucket sidecar upload failed after that DB transaction committed.
This CLI is the reconciler that catches that gap on a schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cli import reconcile_signed_sidecars as reconciler
from cli.reconcile_signed_sidecars import _select_signed_versions, reconcile_one

_APP_ID = "waddles.socials.music.default"
_VERSION = "3.0.1"
_DIGEST = "sha256:" + "a" * 64


async def _seed_signed_version(install_dal: Any) -> Any:
    """A fully-signed `app_versions` row -- the set this CLI examines."""
    version_id = await install_dal.app_versions.async_insert(
        app_id=_APP_ID,
        version=_VERSION,
        artifact_digest=_DIGEST,
        language="python",
        artifact_kind="prebuilt",
        scan_status="not_scanned",
        artifact_signature="c2lnbmF0dXJl",
        artifact_signature_key_id="test-key-1",
        artifact_signed_approval_id=1,
        artifact_signed_at=datetime.now(UTC),
    )
    row = (await install_dal(install_dal.app_versions.id == version_id).select()).first()
    return row


async def test_select_finds_a_signed_version(install_dal: Any) -> None:
    seeded = await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)
    assert len(rows) == 1
    assert rows[0].version_id == seeded.id


async def test_select_excludes_an_unsigned_version(install_dal: Any) -> None:
    await install_dal.app_versions.async_insert(
        app_id=_APP_ID,
        version=_VERSION,
        artifact_digest=_DIGEST,
        language="python",
        artifact_kind="prebuilt",
        scan_status="not_scanned",
    )
    rows = await _select_signed_versions(install_dal)
    assert rows == []


async def test_reconcile_one_is_a_noop_when_sidecar_already_has_a_signature(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    seeded = await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)

    read_mock = AsyncMock(return_value={"signature": "c2lnbmF0dXJl", "key_id": "test-key-1"})
    upload_mock = AsyncMock()
    monkeypatch.setattr(reconciler.storage_service, "read_bundle_sidecar", read_mock)
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", upload_mock)

    result = await reconcile_one(rows[0])

    assert result.outcome == "already_signed"
    assert result.app_id == _APP_ID
    upload_mock.assert_not_called()
    del seeded  # only used to seed the row; assertions are on the selected row


async def test_reconcile_one_reuploads_when_sidecar_is_missing(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)

    read_mock = AsyncMock(return_value=None)  # bucket 404 -- object never uploaded / deleted
    upload_mock = AsyncMock(return_value="bundles/mock/1/mock.json")
    monkeypatch.setattr(reconciler.storage_service, "read_bundle_sidecar", read_mock)
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", upload_mock)

    result = await reconcile_one(rows[0])

    assert result.outcome == "reuploaded"
    upload_mock.assert_awaited_once_with(
        app_id=_APP_ID,
        version=_VERSION,
        digest=_DIGEST,
        approval_id=1,
        key_id="test-key-1",
        signature="c2lnbmF0dXJl",
    )


async def test_reconcile_one_reuploads_when_sidecar_is_the_pre_signing_stub(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)

    read_mock = AsyncMock(return_value={})  # the pre-signing `{}` stub -- no `signature` key
    upload_mock = AsyncMock(return_value="bundles/mock/1/mock.json")
    monkeypatch.setattr(reconciler.storage_service, "read_bundle_sidecar", read_mock)
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", upload_mock)

    result = await reconcile_one(rows[0])

    assert result.outcome == "reuploaded"
    upload_mock.assert_awaited_once()


async def test_reconcile_one_retries_with_backoff_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)

    attempts = {"n": 0}

    async def _flaky_upload(**kwargs: Any) -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("bucket unreachable")
        return "bundles/mock/1/mock.json"

    monkeypatch.setattr(
        reconciler.storage_service, "read_bundle_sidecar", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", _flaky_upload)
    monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())  # no real delay in tests

    result = await reconcile_one(rows[0])

    assert result.outcome == "reuploaded"
    assert attempts["n"] == 2


async def test_reconcile_one_reports_failure_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)
    rows = await _select_signed_versions(install_dal)

    async def _always_fails(**kwargs: Any) -> str:
        raise RuntimeError("bucket permanently unreachable")

    monkeypatch.setattr(
        reconciler.storage_service, "read_bundle_sidecar", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", _always_fails)
    monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())

    result = await reconcile_one(rows[0])

    assert result.outcome == "reupload_failed"


async def test_run_reports_nonzero_exit_when_a_row_needs_reupload_and_fails(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    async def _always_fails(**kwargs: Any) -> str:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(reconciler, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(reconciler.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())
    monkeypatch.setattr(
        reconciler.storage_service, "read_bundle_sidecar", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(reconciler.bundle_signing_service, "upload_signed_sidecar", _always_fails)
    monkeypatch.setattr(reconciler.asyncio, "sleep", AsyncMock())

    exit_code = await reconciler._run()

    assert exit_code == 1


async def test_run_returns_zero_when_every_row_is_already_signed(
    monkeypatch: pytest.MonkeyPatch, install_dal: Any
) -> None:
    await _seed_signed_version(install_dal)

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(reconciler, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(reconciler.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())
    monkeypatch.setattr(
        reconciler.storage_service,
        "read_bundle_sidecar",
        AsyncMock(return_value={"signature": "c2lnbmF0dXJl"}),
    )

    exit_code = await reconciler._run()

    assert exit_code == 0


def test_main_returns_the_run_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "_run", AsyncMock(return_value=0))
    assert reconciler.main([]) == 0


def test_main_propagates_a_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciler, "_run", AsyncMock(return_value=1))
    assert reconciler.main([]) == 1
