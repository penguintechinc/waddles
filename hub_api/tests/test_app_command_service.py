"""Tests for the `app_commands` write path in `services.app_command_service`.

`sync_commands()` / `clear_commands()` run directly against `install_dal.engine.begin()` (the
transaction primitive the activation flow uses), mirroring `test_app_source_binding_service.py`.
Covers replace semantics, first-registered ownership, conflict warnings, and PII-free logging.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from services.app_command_service import (
    WARNING_COMMAND_CONFLICT,
    WARNING_NO_ROUTABLE_COMMAND,
    ActivationWarning,
    clear_commands,
    command_owner,
    sync_commands,
)
from services.bundle_manifest_v2 import BundleManifestV2, ConsumeRule, Limits

_A = "waddles.core.alpha.default"
_B = "waddles.core.beta.default"
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _manifest(
    app_id: str,
    prefixes_by_platform: dict[str, tuple[str, ...]],
    *,
    process: bool = False,
    supported: tuple[str, ...] | None = None,
) -> BundleManifestV2:
    """Build a manifest; an empty prefix tuple makes a bare (wildcard) rule for that platform."""
    rules = tuple(
        ConsumeRule(
            platform=platform,
            source_id=None,
            event_types=("chat.message",),
            filters={"command_prefix": list(prefixes)} if prefixes else {},
        )
        for platform, prefixes in prefixes_by_platform.items()
    )
    return BundleManifestV2(
        schema_version=2,
        app_id=app_id,
        name="Test",
        version="1.0.0",
        feature="waddles.core.test",
        module="core",
        provider="builtin",
        language="python",
        artifact="source",
        execution_model="native",
        is_default=False,
        stages={"process": {}} if process else {},
        egress=(),
        data_tables=(),
        limits=Limits(timeout_ms=2000, memory_mb=64, egress_rps=10),
        permissions=(),
        routes_to=(),
        consumes=rules,
        supported_platforms=supported,
    )


async def _sync(
    install_dal: Any,
    app_id: str,
    prefixes_by_platform: dict[str, tuple[str, ...]],
    *,
    now: datetime,
    tenant_id: int = 1,
    community_id: int | None = None,
    process: bool = False,
    supported: tuple[str, ...] | None = None,
) -> tuple[ActivationWarning, ...]:
    table = install_dal.metadata.tables["app_commands"]
    manifest = _manifest(app_id, prefixes_by_platform, process=process, supported=supported)
    async with install_dal.engine.begin() as conn:
        return await sync_commands(
            conn,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            manifest=manifest,
            commands_table=table,
            now=now,
        )


async def _clear(
    install_dal: Any, app_id: str, *, tenant_id: int = 1, community_id: int | None = None
) -> None:
    table = install_dal.metadata.tables["app_commands"]
    async with install_dal.engine.begin() as conn:
        await clear_commands(
            conn,
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            commands_table=table,
        )


async def _rows(install_dal: Any, app_id: str | None = None) -> Any:
    query = (
        install_dal(install_dal.app_commands.app_id == app_id)
        if app_id
        else install_dal(install_dal.app_commands.id > 0)
    )
    return await query.select()


def _naive(value: datetime) -> datetime:
    """Sqlite DateTime round-trips naive; compare on the wall-clock value."""
    return value.replace(tzinfo=None)


async def test_sync_inserts_rows_tenant_wide_stored_as_zero(install_dal: Any) -> None:
    warnings = await _sync(install_dal, _A, {"discord": ("!ping", "!pong")}, now=_T0)
    assert warnings == ()
    rows = await _rows(install_dal, _A)
    assert {r.command for r in rows} == {"ping", "pong"}
    assert all(r.community_id == 0 for r in rows)
    assert all(_naive(r.registered_at) == _naive(_T0) for r in rows)
    assert all(r.platforms == ["discord"] for r in rows)


async def test_resync_is_idempotent_and_preserves_registered_at(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0)
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T1)
    rows = await _rows(install_dal, _A)
    assert len(rows) == 1
    assert _naive(rows[0].registered_at) == _naive(_T0)


async def test_resync_drops_removed_and_keeps_kept_order(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping", "!pong")}, now=_T0)
    await _sync(install_dal, _A, {"discord": ("!ping",), "twitch": ("!ping",)}, now=_T1)
    rows = await _rows(install_dal, _A)
    assert [r.command for r in rows] == ["ping"]
    assert rows[0].platforms == ["discord", "twitch"]  # changed row updated in place
    assert _naive(rows[0].registered_at) == _naive(_T0)


async def test_first_registered_owns_second_gets_warning(install_dal: Any) -> None:
    assert await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=5) == ()
    warnings = await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T1, community_id=5)
    assert len(warnings) == 1
    w = warnings[0]
    assert w == ActivationWarning(
        code="command_conflict",
        app_id=_B,
        command="ping",
        owner_app_id=_A,
        message=w.message,
    )
    assert w.code == WARNING_COMMAND_CONFLICT
    assert _A in w.message and "ping" in w.message
    assert await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T2, community_id=5) == ()


async def test_wildcard_rows_never_warn(install_dal: Any) -> None:
    assert await _sync(install_dal, _A, {"discord": ()}, now=_T0) == ()
    assert await _sync(install_dal, _B, {"discord": ()}, now=_T1) == ()


async def test_disjoint_platforms_do_not_conflict(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0)
    assert await _sync(install_dal, _B, {"twitch": ("!ping",)}, now=_T1) == ()


async def test_other_tenant_and_other_community_never_conflict(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, tenant_id=1, community_id=5)
    other_tenant = await _sync(
        install_dal, _B, {"discord": ("!ping",)}, now=_T1, tenant_id=2, community_id=5
    )
    other_tenant_wide = await _sync(
        install_dal, _B, {"discord": ("!ping",)}, now=_T1, tenant_id=2, community_id=None
    )
    other_community = await _sync(
        install_dal, _B, {"discord": ("!ping",)}, now=_T1, tenant_id=1, community_id=6
    )
    assert other_tenant == () and other_tenant_wide == () and other_community == ()


async def test_tenant_wide_and_community_conflict_both_directions(install_dal: Any) -> None:
    # community first, tenant-wide later -> the tenant-wide app warns
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=5)
    later = await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T1, community_id=None)
    assert [(w.app_id, w.owner_app_id) for w in later] == [(_B, _A)]
    await _clear(install_dal, _A, community_id=5)
    await _clear(install_dal, _B, community_id=None)

    # tenant-wide first, community later -> the community app warns
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=None)
    later = await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T1, community_id=5)
    assert [(w.app_id, w.owner_app_id) for w in later] == [(_B, _A)]


async def test_equal_registered_at_is_broken_by_id(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0)
    await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T0)
    a_warn = await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0)
    b_warn = await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T0)
    a_row = (await _rows(install_dal, _A))[0]
    b_row = (await _rows(install_dal, _B))[0]
    assert a_row.id < b_row.id
    assert a_warn == ()
    assert [w.owner_app_id for w in b_warn] == [_A]
    assert await command_owner(install_dal, tenant_id=1, community_id=5, command="ping") == _A


async def test_clear_commands_removes_only_that_app_and_scope(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=None)
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=5)
    await _sync(install_dal, _B, {"twitch": ("!ping",)}, now=_T0, community_id=None)
    await _clear(install_dal, _A, community_id=None)
    remaining = {(r.app_id, r.community_id) for r in await _rows(install_dal)}
    assert remaining == {(_A, 5), (_B, 0)}


async def test_reactivation_requeues_behind_interim_app(install_dal: Any) -> None:
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0)
    await _sync(install_dal, _B, {"discord": ("!ping",)}, now=_T1)
    await _clear(install_dal, _A)
    warnings = await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T2)
    assert [w.owner_app_id for w in warnings] == [_B]
    assert await command_owner(install_dal, tenant_id=1, community_id=5, command="ping") == _B


async def test_no_routable_command_warning(install_dal: Any) -> None:
    warnings = await _sync(
        install_dal,
        _A,
        {"twitch": ("!ping",)},
        now=_T0,
        process=True,
        supported=("discord",),
    )
    assert warnings == (
        ActivationWarning(
            code=WARNING_NO_ROUTABLE_COMMAND,
            app_id=_A,
            command=None,
            owner_app_id=None,
            message=warnings[0].message,
        ),
    )
    assert not await _rows(install_dal, _A)


async def test_command_owner_picks_first_registered_and_filters_platform(
    install_dal: Any,
) -> None:
    assert await command_owner(install_dal, tenant_id=1, community_id=5, command="ping") is None
    await _sync(install_dal, _B, {"twitch": ("!ping",)}, now=_T1, community_id=5)
    await _sync(install_dal, _A, {"discord": ("!ping",)}, now=_T0, community_id=None)
    assert await command_owner(install_dal, tenant_id=1, community_id=5, command="ping") == _A
    assert (
        await command_owner(
            install_dal, tenant_id=1, community_id=5, command="ping", platform="twitch"
        )
        == _B
    )
    assert (
        await command_owner(
            install_dal, tenant_id=1, community_id=5, command="ping", platform="slack"
        )
        is None
    )
    assert await command_owner(install_dal, tenant_id=2, community_id=5, command="ping") is None


async def test_sync_logs_counts_without_command_text(
    install_dal: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await _sync(install_dal, _A, {"discord": ("!secretverb",)}, now=_T0)
    with caplog.at_level(logging.INFO):
        await _sync(install_dal, _B, {"discord": ("!secretverb", "!other")}, now=_T1)
    records = [r for r in caplog.records if r.message == "app commands: synced"]
    assert len(records) == 1
    rec = records[0]
    assert (rec.app_id, rec.tenant_id, rec.community_id) == (_B, 1, 0)
    assert (rec.rows, rec.warnings) == (2, 1)
    assert "secretverb" not in caplog.text and "other" not in caplog.text.replace(_B, "")
