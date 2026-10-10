"""ORACLE proof (spec Sec18 R1, D7 G3): the real alias bundle, through this facade.

The unmodified, currently-merged `core/svc_process/builtin_handlers/
social_alias_process.py` -- migrated to `penguin_dal` natively under M1.5 and
already green in its own suite
(`core/svc_process/tests/test_builtin_social_alias_process.py`) -- driven
end to end through this SDK's `penguin_dal`-compatible facade and
`flask_core` compatibility shims, exactly the way a compiled component would
see it once `waddle-sdk` is installed in `flask_core`'s place at build time
(spec D7/Sec4.12).

**Updated for the structured `db` capability (user decision: bundles use
structured `insert`/`get`/`query`/`update`/`delete` ops, never a raw-SQL/
DAL-style query builder; design doc SS1 round-1 CRITICAL finding: "no
bundle-supplied SQL, ever").** `social_alias_process.py` itself is not
migrated to the structured ops in this change -- it still drives
`flask_core.get_bundle_dal()`'s pydal-style query builder, and
`flask_core.bundle_runtime.raw_sql_rows`/`raw_sql_write` for its own
permission-check join -- and neither has a structured-ops analog
(`waddle_sdk.db.AsyncDB` is now an inert placeholder, `raw_sql_rows`/
`raw_sql_write` are independently permanent `NotImplementedError` stubs,
D21: a live `dal.engine` connection cannot be lowered into the sandbox).
Full oracle coverage for this bundle's query-composition fidelity will be
re-established, against the structured API, once the bundle itself is
migrated in a future wave.

This file stays the facade's own oracle in the meantime by proving the
REAL, currently-shipping behavior for real: every command path below runs
the genuine `transform()`, through the genuine (non-mocked) `AsyncDB`
placeholder and `raw_sql_rows` stub -- no WIT fake is installed at all,
since neither code path reaches the WIT `db` import anymore. Per
`critical-rules.md` Fail-Loud Code Paths, an unimplemented capability
boundary must fail loudly, never silently return a default -- and that is
exactly what both `AsyncDB.__getattr__`/`__call__` and `raw_sql_rows`/
`raw_sql_write` do. `social_alias_process.py`'s own commands wrap their
DB-dependent work in `except Exception` ("a write must reply, never crash
the bot"), so the real, observable, end-to-end behavior through this SDK
today is a graceful "Failed to ..." reply naming the retirement -- proven
here against the bundle's real source, not a hand-picked reproduction. The
one path NOT wrapped that way, the moderator/admin permission gate, itself
independently catches every exception and fails closed (denies), which is
real pre-existing behavior unrelated to this change (`raw_sql_rows` was
already a `NotImplementedError` stub before the structured `db` rewrite).

Two dependency-boundary stubs, neither touching the facade under test:

- `builtin_handlers.bot_process` (a SIBLING bundle `_cmd_set_alias`'s `_known_commands()`
  hard-imports for its command vocabulary) is replaced with a minimal fake
  exposing `_BOT_COMMANDS`/`_FEATURE_MODULES` -- `bot_process`'s own runtime
  dependency chain (`services.command_alias_store` -> `config.Config` ->
  `flask_core.secrets.require_secret_key`) is unrelated to the DB facade
  this test validates.
- No WIT `flags` fake is installed: `waddle_sdk.flask_core.feature_flags
  .feature_enabled` falls back to its supplied `default` (`True`, this
  bundle's own call-site default) whenever `wit_world` isn't importable at
  all, which is exactly this host-side test's situation.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

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

#: Substring every retired-facade failure reply carries (`AsyncDB.__getattr__`'s
#: own message) -- the oracle proof that these replies are the real retirement
#: error, not some other, unrelated failure swallowed by the same `except`.
_RETIRED_MARKER = "is retired -- call waddle_sdk.db's structured"


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
    """Import the real `builtin_handlers.social_alias_process` with `flask_core` shimmed to this SDK."""
    if not SVC_PROCESS_ROOT.is_dir():
        pytest.skip(f"core/svc_process not found at {SVC_PROCESS_ROOT} -- requires a full checkout")

    monkeypatch.setitem(sys.modules, "flask_core", waddle_flask_core)
    monkeypatch.setitem(sys.modules, "flask_core.bundle_runtime", waddle_bundle_runtime)
    monkeypatch.setitem(sys.modules, "flask_core.feature_flags", waddle_feature_flags)
    monkeypatch.setitem(sys.modules, "flask_core.stream_pipeline", waddle_stream_pipeline)

    fake_bot_process = types.ModuleType("builtin_handlers.bot_process")
    fake_bot_process._BOT_COMMANDS = frozenset({"ping", "hello"})  # type: ignore[attr-defined]
    fake_bot_process._FEATURE_MODULES = {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "builtin_handlers.bot_process", fake_bot_process)

    monkeypatch.syspath_prepend(str(SVC_PROCESS_ROOT))
    for name in list(sys.modules):
        if (
            name == "builtin_handlers"
            or name.startswith("builtin_handlers.")
            or name == "services"
            or name.startswith("services.")
        ):
            if name != "builtin_handlers.bot_process":
                monkeypatch.delitem(sys.modules, name, raising=False)

    reset_bundle_dal_for_tests()
    set_bundle_dal(AsyncDB())

    import builtin_handlers.social_alias_process as social_alias_process

    yield social_alias_process

    reset_bundle_dal_for_tests()


def test_alias_list_fails_loud_through_the_retired_facade(alias_bundle) -> None:
    """`!alias list` surfaces the real `AsyncDB` retirement as a graceful reply, never a crash."""
    social_alias_process = alias_bundle
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias list")))
    assert result is not None
    text = result.payload["text"]
    assert text.startswith("Failed to list aliases: AsyncDB.command_aliases ")
    assert _RETIRED_MARKER in text


def test_alias_add_denies_without_permission(alias_bundle) -> None:
    """Without the permission-gate bypass, the write is denied.

    The moderator/admin check itself fails closed on any lookup error
    (`_caller_is_moderator_or_admin`'s own `except Exception: return False`)
    -- real, pre-existing behavior given `raw_sql_rows` is independently a
    permanent `NotImplementedError` stub, unrelated to the structured `db`
    rewrite this file otherwise proves.
    """
    social_alias_process = alias_bundle
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias add sr ping")))
    assert result.payload["text"] == "only moderators/admins can set aliases"


def test_unalias_denies_without_permission(alias_bundle) -> None:
    """`!unalias` is gated the same way `!alias add` is -- same fail-closed permission check."""
    social_alias_process = alias_bundle
    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!unalias sr")))
    assert result.payload["text"] == "only moderators/admins can set aliases"


def test_alias_add_fails_loud_when_permission_bypassed(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the permission gate bypassed, the write itself hits the real retired `AsyncDB`.

    This is the spike's own benchmark path (`!alias add`) -- proven here
    against the bundle's real source: the write no longer silently
    succeeds against a nonexistent backend, it fails loud and the bundle's
    own `except Exception` turns that into a user-facing reply, never a
    crash (`critical-rules.md` Fail-Loud Code Paths).
    """
    social_alias_process = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!alias add sr ping")))

    assert result is not None
    text = result.payload["text"]
    assert text.startswith("Failed to set alias: AsyncDB.command_aliases ")
    assert _RETIRED_MARKER in text


def test_unalias_fails_loud_when_permission_bypassed(
    alias_bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`!unalias` with the permission gate bypassed: same real retired-`AsyncDB` failure."""
    social_alias_process = alias_bundle

    async def _allow(event, community_id):
        return True

    monkeypatch.setattr(social_alias_process, "_caller_is_moderator_or_admin", _allow)

    with bundle_context(tenant="acme", community="1", app_id="waddles.social.alias.default"):
        result = _run(social_alias_process.transform(_event("!unalias sr")))

    assert result is not None
    text = result.payload["text"]
    assert text.startswith("Failed to remove alias: AsyncDB.command_aliases ")
    assert _RETIRED_MARKER in text
