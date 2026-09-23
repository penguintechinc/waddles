"""ORACLE proof (spec Sec18 R1, D7 G3): the real alias bundle, through this facade.

The unmodified, currently-merged `core/svc_process/bundles/
social_alias_process.py` -- migrated to `penguin_dal` natively under M1.5 and
already green in its own suite
(`core/svc_process/tests/test_bundles_social_alias_process.py`) -- driven
end to end through this SDK's `penguin_dal`-compatible facade and
`flask_core` compatibility shims, exactly the way a compiled component would
see it once `waddle-sdk` is installed in `flask_core`'s place at build time
(spec D7/Sec4.12).

**Scope note, stated once here.** The bundle's own current pytest suite
seeds its fixtures via `dal.engine.connect()`/`dal.engine.begin()` -- a live
SQLAlchemy async engine -- which is exactly the construct `AsyncDB.engine`
(see `waddle_sdk/db.py`) explicitly cannot lower (no live connection exists
inside the sandbox; the WIT `db` import *is* the connection). Running that
literal test file unmodified against this facade is therefore not possible
by design, not an oversight -- D21's own rule is that such a construct must
raise, never silently mis-execute, and it does (see `test_bundle_runtime.py`'s
`raw_sql_rows`/`raw_sql_write` tests for the same class of gap, hit by this
bundle's own permission-check helper below).

This module is the facade's own oracle test instead: it imports the real
bundle module fresh, with `flask_core` resolved to `waddle_sdk.flask_core`
in `sys.modules` (the same substitution a real build performs), and drives
`transform()` for the exact three commands the M1.5 suite covers business
logic for (`!alias list`, `!alias add`, `!unalias`) through a fake WIT `db`
import backed by `wit_fake_db.FakeWitDb` -- proving the query-composition
fidelity (R1) against the bundle's real, current source, not a hand-picked
reproduction.

Two dependency-boundary stubs, neither touching the facade under test:

- `bundles.bot_process` (a SIBLING bundle `_cmd_set_alias`'s `_known_commands()`
  hard-imports for its command vocabulary) is replaced with a minimal fake
  exposing `_BOT_COMMANDS`/`_FEATURE_MODULES` -- `bot_process`'s own runtime
  dependency chain (`services.command_alias_store` -> `config.Config` ->
  `flask_core.secrets.require_secret_key`) is unrelated to the DB facade
  this test validates.
- `_caller_is_moderator_or_admin` (the bundle's own permission gate) is
  monkeypatched to allow, for the write-path tests only -- it calls
  `flask_core.bundle_runtime.raw_sql_rows`, which is *separately* proven to
  raise `NotImplementedError` in `test_bundle_runtime.py` (D21: raw
  `dal.engine`-based joins cannot be lowered). Bypassing it here isolates
  the DB-facade write path (the `!alias add`/`!unalias` benchmark the spike
  itself ran) from that already-documented, unrelated gap.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest
import wit_fake_db

import waddle_sdk.flask_core as waddle_flask_core
import waddle_sdk.flask_core.bundle_runtime as waddle_bundle_runtime
import waddle_sdk.flask_core.feature_flags as waddle_feature_flags
import waddle_sdk.flask_core.stream_pipeline as waddle_stream_pipeline
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core.bundle_runtime import (
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

SVC_PROCESS_ROOT = Path(__file__).resolve().parents[3] / "core" / "svc_process"


def _run(coro):
    return asyncio.run(coro)


def _event(text: str, *, actor: str | None = "penguin") -> PlatformEvent:
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor=actor,
        payload={"text": text, "channel_id": "chan-123"},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def alias_bundle(monkeypatch: pytest.MonkeyPatch):
    """Import the real `bundles.social_alias_process` with `flask_core` shimmed to this SDK."""
    if not SVC_PROCESS_ROOT.is_dir():
        pytest.skip(
            f"core/svc_process not found at {SVC_PROCESS_ROOT} -- requires a full checkout"
        )

    monkeypatch.setitem(sys.modules, "flask_core", waddle_flask_core)
    monkeypatch.setitem(sys.modules, "flask_core.bundle_runtime", waddle_bundle_runtime)
    monkeypatch.setitem(sys.modules, "flask_core.feature_flags", waddle_feature_flags)
    monkeypatch.setitem(sys.modules, "flask_core.stream_pipeline", waddle_stream_pipeline)

    fake_bot_process = types.ModuleType("bundles.bot_process")
    fake_bot_process._BOT_COMMANDS = frozenset({"ping", "hello"})  # type: ignore[attr-defined]
    fake_bot_process._FEATURE_MODULES = {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bundles.bot_process", fake_bot_process)

    monkeypatch.syspath_prepend(str(SVC_PROCESS_ROOT))
    for name in list(sys.modules):
        if (
            name == "bundles"
            or name.startswith("bundles.")
            or name == "services"
            or name.startswith("services.")
        ):
            if name != "bundles.bot_process":
                monkeypatch.delitem(sys.modules, name, raising=False)

    fake_db = wit_fake_db.install(monkeypatch)
    fake_db_module = sys.modules["wit_world"]
    fake_db_module.imports.flags = types.SimpleNamespace(  # type: ignore[attr-defined]
        enabled=lambda key, default_value: default_value
    )

    reset_bundle_dal_for_tests()
    set_bundle_dal(AsyncDB())

    import bundles.social_alias_process as social_alias_process

    yield social_alias_process, fake_db

    reset_bundle_dal_for_tests()


def test_alias_list_reports_no_aliases_through_the_facade(alias_bundle) -> None:
    """`!alias list` with an empty table replies with the exact no-aliases message."""
    social_alias_process, _fake_db = alias_bundle
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias list")))
    assert result is not None
    assert result.payload["text"] == "no aliases set — try !alias xx somecommand"


def test_alias_list_reports_seeded_aliases_through_the_facade(alias_bundle) -> None:
    """`!alias list` reads real rows back through the facade's SELECT lowering."""
    social_alias_process, fake_db = alias_bundle
    fake_db.canned_rows["command_aliases"] = [
        {"alias": "gg", "target_command": "hello"},
        {"alias": "sr", "target_command": "ping"},
    ]
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias list")))
    assert "!gg → !hello" in result.payload["text"]
    assert "!sr → !ping" in result.payload["text"]


def test_alias_add_full_write_path_through_the_facade(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spike's own benchmark: `!alias add` end to end, real bundle + this facade."""
    social_alias_process, fake_db = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias add sr ping")))

    assert result is not None
    assert result.payload["text"] == "alias set: !sr → !ping"
    insert_calls = [c for c in fake_db.calls if c[0].startswith("INSERT INTO command_aliases")]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert sql == (
        "INSERT INTO command_aliases (community_id, alias, target_command, created_by) "
        "VALUES ($1, $2, $3, $4) RETURNING id"
    )
    assert params == [1, "sr", "ping", "penguin"]


def test_alias_add_reuses_existing_row_via_update_when_alias_already_exists(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-adding an existing (even soft-deleted) alias name UPDATEs the row, never blind-INSERTs."""
    social_alias_process, fake_db = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)
    fake_db.canned_rows["SELECT * FROM command_aliases WHERE"] = [
        {
            "id": 42,
            "community_id": 1,
            "alias": "sr",
            "target_command": "old",
            "deleted_at": "2026-01-01T00:00:00Z",
        }
    ]

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias add sr ping")))

    assert result.payload["text"] == "alias set: !sr → !ping"
    update_calls = [c for c in fake_db.calls if c[0].startswith("UPDATE command_aliases")]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert "target_command = $1" in sql
    assert "deleted_at = $2" in sql
    assert "created_by = $3" in sql
    assert params[0] == "ping"
    assert params[1] is None
    assert params[-1] == 42  # WHERE command_aliases.id = $4


def test_alias_add_denies_without_permission(alias_bundle) -> None:
    """Without the permission-gate bypass, the write is denied (raw_sql_rows fails closed)."""
    social_alias_process, fake_db = alias_bundle
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias add sr ping")))
    assert result.payload["text"] == "only moderators/admins can set aliases"
    assert not any(c[0].startswith("INSERT INTO command_aliases") for c in fake_db.calls)


def test_unalias_full_write_path_through_the_facade(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`!unalias <name>` soft-deletes the row (UPDATE deleted_at) through the facade."""
    social_alias_process, fake_db = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)
    fake_db.canned_rows["SELECT * FROM command_aliases WHERE"] = [
        {"id": 7, "community_id": 1, "alias": "sr", "target_command": "ping", "deleted_at": None}
    ]

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!unalias sr")))

    assert result.payload["text"] == "alias removed: !sr"
    update_calls = [
        c for c in fake_db.calls if c[0].startswith("UPDATE command_aliases SET deleted_at")
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert sql == "UPDATE command_aliases SET deleted_at = $1 WHERE command_aliases.id = $2"
    assert params[1] == 7


def test_unalias_reports_no_alias_when_none_found(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`!unalias` on a name with no active row reports "no alias named", never crashes or writes."""
    social_alias_process, fake_db = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!unalias ghost")))

    assert result.payload["text"] == "no alias named !ghost"
    assert not any(c[0].startswith("UPDATE") for c in fake_db.calls)
