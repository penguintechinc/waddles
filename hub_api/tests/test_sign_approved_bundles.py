"""Tests for `cli/sign_approved_bundles.py` -- the one-off artifact-signing backfill CLI.

Reuses the `install_dal`/`bundle_install_db` fixtures every other bundle-install test uses
(`tests/conftest.py`); `bundle_signing_test_env`/`bundle_signing_sidecar_upload_mock` (also
`conftest.py`, autouse) give every test a valid test signing key and a stubbed sidecar upload
by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cli import sign_approved_bundles as backfill
from cli.sign_approved_bundles import _select_unsigned_approved_versions, sign_one

_APP_ID = "waddles.socials.music.default"
_VERSION = "3.0.1"
_DIGEST = "a" * 64


async def _seed_version(
    install_dal: Any,
    *,
    app_id: str = _APP_ID,
    version: str = _VERSION,
    digest: str | None = _DIGEST,
) -> int:
    return await install_dal.app_versions.async_insert(
        app_id=app_id,
        version=version,
        artifact_digest=digest,
        language="python",
        artifact_kind="prebuilt",
        scan_status="not_scanned",
    )


async def _seed_current_approval(
    install_dal: Any, *, app_id: str = _APP_ID, version: str = _VERSION
) -> int:
    now = datetime.now(UTC)
    return await install_dal.app_install_approvals.async_insert(
        tenant_id=1,
        community_id=None,
        app_id=app_id,
        version=version,
        permission_hash="sha256:" + "b" * 64,
        summary_json={},
        approved_by=1,
        approved_at=now,
        superseded_by=None,
    )


async def test_select_finds_an_unsigned_approved_row(install_dal: Any) -> None:
    version_id = await _seed_version(install_dal)
    approval_id = await _seed_current_approval(install_dal)

    rows = await _select_unsigned_approved_versions(install_dal)

    assert len(rows) == 1
    assert rows[0].version_id == version_id
    assert rows[0].app_id == _APP_ID
    assert rows[0].version == _VERSION
    assert rows[0].approval_id == approval_id


async def test_select_excludes_a_version_with_no_current_approval(install_dal: Any) -> None:
    await _seed_version(install_dal)
    # No approval row at all -- an unapproved version is never signed.
    rows = await _select_unsigned_approved_versions(install_dal)
    assert rows == []


async def test_select_excludes_a_version_with_no_digest_yet(install_dal: Any) -> None:
    await _seed_version(install_dal, digest=None)
    await _seed_current_approval(install_dal)
    rows = await _select_unsigned_approved_versions(install_dal)
    assert rows == []


async def test_select_excludes_an_already_signed_version(install_dal: Any) -> None:
    version_id = await _seed_version(install_dal)
    await _seed_current_approval(install_dal)
    versions_table = install_dal.metadata.tables["app_versions"]
    from sqlalchemy import update as sa_update

    async with install_dal.engine.begin() as conn:
        await conn.execute(
            sa_update(versions_table)
            .where(versions_table.c.id == version_id)
            .values(artifact_signature="already-signed")
        )
    rows = await _select_unsigned_approved_versions(install_dal)
    assert rows == []


async def test_select_excludes_a_superseded_approval(install_dal: Any) -> None:
    await _seed_version(install_dal)
    superseding_id = await _seed_current_approval(install_dal)
    # A SUPERSEDED (not current) approval for the same version -- must not
    # count as "approved" on its own.
    now = datetime.now(UTC)
    await install_dal.app_install_approvals.async_insert(
        tenant_id=1,
        community_id=None,
        app_id=_APP_ID,
        version=_VERSION,
        permission_hash="sha256:" + "c" * 64,
        summary_json={},
        approved_by=1,
        approved_at=now,
        superseded_by=superseding_id,
    )
    rows = await _select_unsigned_approved_versions(install_dal)
    # Exactly one row -- the still-current approval, not the superseded one.
    assert len(rows) == 1
    assert rows[0].approval_id == superseding_id


async def test_select_dedupes_a_version_with_multiple_current_approvals(
    install_dal: Any,
) -> None:
    """Regression: two CURRENT approvals for one version must yield exactly one row.

    An earlier revision of `_SELECT_UNSIGNED_APPROVED_VERSIONS_SQL` grouped
    by `a.id` in addition to `v.id`, which did NOT collapse duplicates --
    this version would have produced 2 rows, and `sign_one()` would have
    re-signed/re-uploaded the same version twice in one backfill run.
    """
    version_id = await _seed_version(install_dal)
    first_approval_id = await _seed_current_approval(install_dal)
    now = datetime.now(UTC)
    second_approval_id = await install_dal.app_install_approvals.async_insert(
        tenant_id=2,
        community_id=None,
        app_id=_APP_ID,
        version=_VERSION,
        permission_hash="sha256:" + "d" * 64,
        summary_json={},
        approved_by=1,
        approved_at=now,
        superseded_by=None,
    )

    rows = await _select_unsigned_approved_versions(install_dal)

    assert len(rows) == 1
    assert rows[0].version_id == version_id
    assert rows[0].approval_id == min(first_approval_id, second_approval_id)


async def test_sign_one_signs_and_uploads_the_sidecar(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id = await _seed_version(install_dal)
    approval_id = await _seed_current_approval(install_dal)
    rows = await _select_unsigned_approved_versions(install_dal)
    assert len(rows) == 1

    upload_mock = AsyncMock(return_value="bundles/mock/1/mock.json")
    monkeypatch.setattr(
        backfill.bundle_signing_service.storage_service, "write_bundle_sidecar", upload_mock
    )

    result = await sign_one(install_dal, rows[0])

    assert result.outcome == "signed"
    assert result.app_id == _APP_ID
    assert result.version == _VERSION
    upload_mock.assert_awaited_once()

    row = (await install_dal(install_dal.app_versions.id == version_id).select()).first()
    assert row.artifact_signature is not None
    assert row.artifact_signed_approval_id == approval_id


async def test_sign_one_reports_sidecar_upload_failure_without_raising(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_version(install_dal)
    await _seed_current_approval(install_dal)
    rows = await _select_unsigned_approved_versions(install_dal)

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(
        backfill.bundle_signing_service.storage_service, "write_bundle_sidecar", _boom
    )

    result = await sign_one(install_dal, rows[0])

    assert result.outcome == "signed_sidecar_upload_failed"
    # The DB side still committed -- a re-run would no longer select this
    # row (it now has a signature), only the CLI's own idempotent sidecar
    # re-upload path (re-running this same CLI, or a future dedicated
    # sidecar-repair tool) can retry the upload half.
    row = (
        await install_dal(
            (install_dal.app_versions.app_id == _APP_ID)
            & (install_dal.app_versions.version == _VERSION)
        ).select()
    ).first()
    assert row.artifact_signature is not None


async def test_run_signs_every_unsigned_approved_row_and_returns_zero(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_version(install_dal)
    await _seed_current_approval(install_dal)
    # A second, already-signed version must be left alone (no-op).
    other_version_id = await _seed_version(install_dal, app_id="waddles.other.app", version="1.0.0")
    await _seed_current_approval(install_dal, app_id="waddles.other.app", version="1.0.0")
    from sqlalchemy import update as sa_update

    versions_table = install_dal.metadata.tables["app_versions"]
    async with install_dal.engine.begin() as conn:
        await conn.execute(
            sa_update(versions_table)
            .where(versions_table.c.id == other_version_id)
            .values(artifact_signature="already-signed")
        )

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(backfill, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(backfill.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())

    exit_code = await backfill._run()

    assert exit_code == 0
    row = (
        await install_dal(
            (install_dal.app_versions.app_id == _APP_ID)
            & (install_dal.app_versions.version == _VERSION)
        ).select()
    ).first()
    assert row.artifact_signature is not None


async def test_run_reports_nonzero_exit_when_a_row_fails(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_version(install_dal)
    await _seed_current_approval(install_dal)

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(backfill, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(backfill.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())
    monkeypatch.delenv("BUNDLE_SIGNING_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("BUNDLE_SIGNING_KEY_ID", raising=False)

    exit_code = await backfill._run()

    assert exit_code == 1


async def test_run_reports_nonzero_exit_when_sidecar_upload_fails_without_raising(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-raising `"signed_sidecar_upload_failed"` outcome still counts as a failure.

    Exercises `_run()`'s own `outcome != "signed"` branch, distinct from
    `test_run_reports_nonzero_exit_when_a_row_fails`'s raised-exception path.
    """
    await _seed_version(install_dal)
    await _seed_current_approval(install_dal)

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(
        backfill.bundle_signing_service.storage_service, "write_bundle_sidecar", _boom
    )

    async def _fake_build_install_dal(database_url: str, pool_size: int) -> Any:
        return install_dal

    class _FakeConfig:
        database_url = "sqlite://"

    monkeypatch.setattr(backfill, "build_install_dal", _fake_build_install_dal)
    monkeypatch.setattr(backfill.HubAPIConfig, "from_env", staticmethod(lambda: _FakeConfig()))
    monkeypatch.setattr(install_dal, "close", AsyncMock())

    exit_code = await backfill._run()

    assert exit_code == 1


def test_main_returns_the_run_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run() -> int:
        return 0

    monkeypatch.setattr(backfill, "_run", _fake_run)
    assert backfill.main([]) == 0


def test_main_propagates_a_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run() -> int:
        return 1

    monkeypatch.setattr(backfill, "_run", _fake_run)
    assert backfill.main([]) == 1
