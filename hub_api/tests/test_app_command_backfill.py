"""Tests for `services.app_command_backfill.backfill_active_commands`.

Drives the sweep against the real sqlite `install_dal`: rows are inserted straight into
`app_versions` / `app_version_uploads` / `app_active_versions` (activation WITHOUT the
registry sync, i.e. the pre-registry state) and the backfill must populate `app_commands`
in true activation order without ever reordering owners on a re-run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from services.app_command_backfill import BackfillResult, backfill_active_commands
from services.app_command_service import command_owner

_A = "waddles.core.alpha.default"
_B = "waddles.core.beta.default"
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_T9 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _raw_manifest(app_id: str, prefixes: tuple[str, ...]) -> dict[str, Any]:
    """Stored `manifest_json` shape `_reparse_trusted` accepts, consuming discord chat."""
    return {
        "schema_version": 2,
        "app_id": app_id,
        "name": "Test",
        "version": "1.0.0",
        "feature": "waddles.core.test",
        "module": "core",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "stages": {
            "process": {
                "consumes": [
                    {
                        "platform": "discord",
                        "event_types": ["chat.message"],
                        "filters": {"command_prefix": list(prefixes)},
                    }
                ]
            }
        },
    }


async def _activate(
    install_dal: Any,
    app_id: str,
    prefixes: tuple[str, ...],
    *,
    activated_at: datetime,
    community_id: int = 0,
    manifest_json: dict[str, Any] | None = None,
) -> None:
    """Insert version + upload + active rows directly, bypassing the activation sync."""
    version_id = int(
        await install_dal.app_versions.async_insert(
            app_id=app_id, version="1.0.0", language="python", artifact_kind="source"
        )
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=app_id,
        version="1.0.0",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest_json
        if manifest_json is not None
        else _raw_manifest(app_id, prefixes),
        app_version_id=version_id,
    )
    active = install_dal.metadata.tables["app_active_versions"]
    async with install_dal.engine.begin() as conn:  # no PK on this table -> Core insert
        await conn.execute(
            active.insert().values(
                app_id=app_id,
                tenant_id=1,
                community_id=community_id,
                version_id=version_id,
                activated_at=activated_at,
            )
        )


async def _rows(install_dal: Any) -> Any:
    return await install_dal(install_dal.app_commands.id > 0).select()


def _naive(value: datetime) -> datetime:
    """Sqlite DateTime round-trips naive; compare on the wall-clock value."""
    return value.replace(tzinfo=None)


async def test_backfill_registers_commands_for_already_active_apps(install_dal: Any) -> None:
    await _activate(install_dal, _A, ("!ping",), activated_at=_T0)
    await _activate(install_dal, _B, ("!pong",), activated_at=_T1, community_id=7)

    result = await backfill_active_commands(install_dal)

    assert result == BackfillResult(examined=2, synced=2, failed=())
    rows = await _rows(install_dal)
    assert {(r.app_id, r.command, r.community_id) for r in rows} == {
        (_A, "ping", 0),
        (_B, "pong", 7),
    }


async def test_backfill_is_idempotent_and_never_reorders_owners(install_dal: Any) -> None:
    await _activate(install_dal, _A, ("!ping",), activated_at=_T0)
    await _activate(install_dal, _B, ("!ping",), activated_at=_T1)
    await backfill_active_commands(install_dal, now=_T2)
    before = {(r.app_id, r.command): _naive(r.registered_at) for r in await _rows(install_dal)}

    second = await backfill_active_commands(install_dal, now=_T9)

    assert second == BackfillResult(examined=2, synced=2, failed=())
    after = {(r.app_id, r.command): _naive(r.registered_at) for r in await _rows(install_dal)}
    assert after == before
    owner = await command_owner(
        install_dal, tenant_id=1, community_id=1, command="ping", platform="discord"
    )
    assert owner == _A


async def test_backfill_orders_by_activated_at_on_first_run(install_dal: Any) -> None:
    # B is inserted FIRST but activated LATER: A (earliest activation) must own `ping`.
    await _activate(install_dal, _B, ("!ping",), activated_at=_T1)
    await _activate(install_dal, _A, ("!ping",), activated_at=_T0)

    await backfill_active_commands(install_dal, now=_T9)

    by_app = {r.app_id: _naive(r.registered_at) for r in await _rows(install_dal)}
    assert by_app[_A] == _naive(_T0)
    assert by_app[_B] == _naive(_T1)
    assert by_app[_A] < by_app[_B]


async def test_backfill_records_failure_for_corrupt_manifest_and_continues(
    install_dal: Any, caplog: pytest.LogCaptureFixture
) -> None:
    bad = _raw_manifest(_A, ("!ping",))
    del bad["schema_version"]
    await _activate(install_dal, _A, (), activated_at=_T0, community_id=3, manifest_json=bad)
    await _activate(install_dal, _B, ("!pong",), activated_at=_T1)

    with caplog.at_level(logging.WARNING):
        result = await backfill_active_commands(install_dal)

    assert result == BackfillResult(examined=2, synced=1, failed=(f"{_A}@3: KeyError",))
    assert {r.app_id for r in await _rows(install_dal)} == {_B}
    assert "backfill failed" in caplog.text


async def test_backfill_with_no_active_rows_reports_zero(install_dal: Any) -> None:
    assert await backfill_active_commands(install_dal) == BackfillResult(0, 0, ())
