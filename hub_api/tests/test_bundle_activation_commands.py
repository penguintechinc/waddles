"""Tests for the `app_commands` registry write path wired into bundle (de)activation.

`activate_*_with_warnings` registers a bundle's slash commands via `app_command_service.
sync_commands` in the SAME `engine.begin()` transaction as the approval/binding/activation
writes; `deactivate_*` clears them in the deactivation transaction. Warnings are returned (and
logged after commit) but never block activation.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from services import app_command_service
from services.app_command_service import (
    WARNING_COMMAND_CONFLICT,
    ActivationWarning,
    command_owner,
)
from services.bundle_approval_service import (
    ActivationResult,
    activate_for_community,
    activate_for_community_with_warnings,
    activate_tenant_wide,
    activate_tenant_wide_with_warnings,
    deactivate_for_community,
    deactivate_tenant_wide,
)
from tests.test_bundle_activation_service import _install_and_make_available

_A = "waddles.socials.alpha.default"
_B = "waddles.socials.beta.default"


def _manifest(
    app_id: str,
    *,
    prefixes: tuple[str, ...] = ("!ping", "!pong"),
    version: str = "1.0.0",
    commands: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Raw v2 manifest dict for `app_id` consuming twitch chat filtered by `prefixes`."""
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "app_id": app_id,
        "name": "Cmd Test",
        "version": version,
        "feature": app_id.rsplit(".", 1)[0],
        "module": "socials",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [
                    {
                        "platform": "twitch",
                        "event_types": ["chat.message"],
                        "filters": {"command_prefix": list(prefixes)},
                    }
                ],
            },
        },
    }
    if commands is not None:
        manifest["commands"] = commands
    return manifest


async def _community(install_dal: Any, name: str = "acme-community") -> int:
    return int(await install_dal.communities.async_insert(tenant_id=1, name=name))


async def _setup(install_dal: Any, app_id: str, **kwargs: Any) -> None:
    await _install_and_make_available(install_dal, manifest=_manifest(app_id, **kwargs))


async def _command_rows(install_dal: Any, *, app_id: str | None = None) -> list[Any]:
    table = install_dal.metadata.tables["app_commands"]
    stmt = select(table).order_by(table.c.command, table.c.id)
    if app_id is not None:
        stmt = stmt.where(table.c.app_id == app_id)
    async with install_dal.engine.connect() as conn:
        return list((await conn.execute(stmt)).all())


async def _activate(install_dal: Any, app_id: str, community_id: int) -> ActivationResult:
    return await activate_for_community_with_warnings(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=app_id,
        activated_by=7,
        valkey_client=AsyncMock(),
    )


async def test_activate_registers_inferred_commands(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    community_id = await _community(install_dal)
    result = await _activate(install_dal, _A, community_id)

    assert result.warnings == ()
    rows = await _command_rows(install_dal, app_id=_A)
    assert [r.command for r in rows] == ["ping", "pong"]
    assert all(r.community_id == community_id for r in rows)
    assert all(list(r.platforms) == ["twitch"] for r in rows)
    assert all(r.command_types == 1 for r in rows)


async def test_tenant_wide_activation_registers_scope_zero(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    result = await activate_tenant_wide_with_warnings(
        install_dal, tenant_id=1, app_id=_A, activated_by=None, valkey_client=AsyncMock()
    )
    assert isinstance(result, ActivationResult)
    rows = await _command_rows(install_dal, app_id=_A)
    assert [r.command for r in rows] == ["ping", "pong"]
    assert all(r.community_id == 0 for r in rows)


async def test_second_app_same_command_returns_warning_naming_first(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    await _setup(install_dal, _B)
    community_id = await _community(install_dal)
    first = await _activate(install_dal, _A, community_id)
    second = await _activate(install_dal, _B, community_id)

    assert first.warnings == ()
    assert second.warnings
    warning = second.warnings[0]
    assert isinstance(warning, ActivationWarning)
    assert warning.code == WARNING_COMMAND_CONFLICT
    assert warning.owner_app_id == _A
    assert warning.app_id == _B
    assert (
        await command_owner(install_dal, tenant_id=1, community_id=community_id, command="ping")
        == _A
    )


async def test_legacy_activate_returns_approval_row_unchanged(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    community_id = await _community(install_dal)
    row = await activate_for_community(
        install_dal,
        tenant_id=1,
        community_id=community_id,
        app_id=_A,
        activated_by=7,
        valkey_client=AsyncMock(),
    )
    assert row.approved_by == 7
    assert row.community_id == community_id

    await _setup(install_dal, _B, prefixes=("!other",))
    wide = await activate_tenant_wide(
        install_dal, tenant_id=1, app_id=_B, activated_by=None, valkey_client=AsyncMock()
    )
    assert wide.community_id is None
    assert wide.approval_source == "system:core-seeder"


async def test_version_bump_reactivation_keeps_ownership(install_dal: Any) -> None:
    """Re-activating the already-active first owner keeps its queue position and ownership."""
    await _setup(install_dal, _A)
    await _setup(install_dal, _B)
    community_id = await _community(install_dal)
    await _activate(install_dal, _A, community_id)
    before = {r.command: r.registered_at for r in await _command_rows(install_dal, app_id=_A)}
    await _activate(install_dal, _B, community_id)

    again = await _activate(install_dal, _A, community_id)

    after = {r.command: r.registered_at for r in await _command_rows(install_dal, app_id=_A)}
    assert after == before
    assert again.warnings == ()
    assert (
        await command_owner(install_dal, tenant_id=1, community_id=community_id, command="ping")
        == _A
    )


async def test_deactivate_then_reactivate_requeues(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    await _setup(install_dal, _B)
    community_id = await _community(install_dal)
    await _activate(install_dal, _A, community_id)
    await _activate(install_dal, _B, community_id)

    await deactivate_for_community(
        install_dal, tenant_id=1, community_id=community_id, app_id=_A, deactivated_by=7
    )
    assert await _command_rows(install_dal, app_id=_A) == []

    result = await _activate(install_dal, _A, community_id)
    assert result.warnings
    assert result.warnings[0].owner_app_id == _B


async def test_deactivate_tenant_wide_clears_scope_zero_rows(install_dal: Any) -> None:
    await _setup(install_dal, _A)
    await activate_tenant_wide_with_warnings(
        install_dal, tenant_id=1, app_id=_A, activated_by=None, valkey_client=AsyncMock()
    )
    assert await _command_rows(install_dal, app_id=_A)

    await deactivate_tenant_wide(install_dal, tenant_id=1, app_id=_A, deactivated_by=None)
    assert await _command_rows(install_dal, app_id=_A) == []


async def test_activation_rolls_back_commands_when_a_later_write_fails(
    install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(install_dal, _A)
    community_id = await _community(install_dal)
    real_sync = app_command_service.sync_commands

    async def run_then_raise(*args: Any, **kwargs: Any) -> Any:
        await real_sync(*args, **kwargs)
        raise RuntimeError("boom after sync_commands")

    monkeypatch.setattr(app_command_service, "sync_commands", run_then_raise)
    with pytest.raises(RuntimeError, match="boom after sync_commands"):
        await _activate(install_dal, _A, community_id)

    assert await _command_rows(install_dal) == []
    active = await install_dal(install_dal.app_active_versions.app_id == _A).select()
    assert active.first() is None


async def test_explicit_commands_field_overrides_inferred_on_activation(
    install_dal: Any,
) -> None:
    await _setup(
        install_dal,
        _A,
        prefixes=("!ping",),
        commands=[{"name": "ping", "types": ["standard", "webhook"], "platforms": ["discord"]}],
    )
    community_id = await _community(install_dal)
    await _activate(install_dal, _A, community_id)

    rows = await _command_rows(install_dal, app_id=_A)
    assert [r.command for r in rows] == ["ping"]
    assert rows[0].command_types == 3
    assert list(rows[0].platforms) == ["discord"]


async def test_warnings_are_logged_after_commit_pii_free(
    install_dal: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await _setup(install_dal, _A)
    await _setup(install_dal, _B)
    community_id = await _community(install_dal)
    await _activate(install_dal, _A, community_id)
    with caplog.at_level(logging.WARNING, logger="services.bundle_approval_service"):
        await _activate(install_dal, _B, community_id)

    records = [r for r in caplog.records if r.getMessage() == "bundle activation: command warning"]
    assert records
    record = records[0]
    assert record.app_id == _B
    assert record.tenant_id == 1
    assert record.community_id == community_id
    assert record.code == WARNING_COMMAND_CONFLICT
    assert not hasattr(record, "command")
