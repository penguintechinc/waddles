"""Host-native tests for the `quote` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/count/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors (fake `db`/`flags`/
`relay`/`log`/`clock`), extended with a tiny stateful in-memory `quotes`
table so inserts/reads/soft-deletes exercise `app.py`'s *real* SQL-building
and dispatch logic end-to-end, not canned responses.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest

from app import DispatchResult, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class Value_NullValue:  # noqa: N801 - mirrors componentize-py's generated case naming exactly
    """Fake WIT `db.value` case."""


class Value_BoolValue:
    """Fake WIT `db.value` case."""

    def __init__(self, value: bool) -> None:
        self.value = value


class Value_IntValue:
    """Fake WIT `db.value` case."""

    def __init__(self, value: int) -> None:
        self.value = value


class Value_FloatValue:
    """Fake WIT `db.value` case."""

    def __init__(self, value: float) -> None:
        self.value = value


class Value_TextValue:
    """Fake WIT `db.value` case."""

    def __init__(self, value: str) -> None:
        self.value = value


class Value_BytesValue:
    """Fake WIT `db.value` case."""

    def __init__(self, value: bytes) -> None:
        self.value = value


class _Rows:
    """Fake WIT `db.Rows` record."""

    def __init__(self, columns: list[str], rows: list[list[Any]], rows_affected: int) -> None:
        self.columns = columns
        self.rows = rows
        self.rows_affected = rows_affected


def _wrap(value: Any) -> Any:
    if value is None:
        return Value_NullValue()
    if isinstance(value, bool):
        return Value_BoolValue(value)
    if isinstance(value, int):
        return Value_IntValue(value)
    if isinstance(value, float):
        return Value_FloatValue(value)
    if isinstance(value, bytes | bytearray):
        return Value_BytesValue(bytes(value))
    return Value_TextValue(str(value))


def _unwrap(value: Any) -> Any:
    if isinstance(value, Value_NullValue):
        return None
    if isinstance(value, Value_BoolValue | Value_IntValue | Value_FloatValue | Value_TextValue):
        return value.value
    if isinstance(value, Value_BytesValue):
        return bytes(value.value)
    raise AssertionError(f"unrecognized fake Value case {type(value).__name__!r}")


def _wrap_rows(columns: list[str], dict_rows: list[dict[str, Any]], rows_affected: int) -> _Rows:
    wit_rows = [[_wrap(row[c]) for c in columns] for row in dict_rows]
    return _Rows(columns=columns, rows=wit_rows, rows_affected=rows_affected)


class FakeWitDb:
    """Stateful in-memory `quotes` table, answering the exact SQL shapes `app.py` generates."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._next_id = 1
        self.calls: list[tuple[str, list[Any]]] = []
        self.fail_on_substring: str | None = None

    def execute(self, statement: str, params: list[Any]) -> _Rows:
        py_params = [_unwrap(p) for p in params]
        self.calls.append((statement, py_params))
        if self.fail_on_substring and self.fail_on_substring in statement:
            raise RuntimeError(f"scripted failure for: {statement}")

        if statement.startswith("INSERT INTO quotes"):
            new_id = self._next_id
            self._next_id += 1
            row = {
                "id": new_id,
                "community_id": py_params[0],
                "quote_text": py_params[1],
                "platform": py_params[2],
                "is_approved": py_params[3],
                "quoted_username": None,
                "deleted_at": None,
                "created_at": f"2026-01-01T00:00:{new_id:02d}+00:00",
            }
            self.rows.append(row)
            return _wrap_rows(["id"], [{"id": new_id}], rows_affected=1)

        if statement.startswith("SELECT * FROM quotes WHERE"):
            quote_id, community_id = py_params[0], py_params[1]
            matches = [
                r
                for r in self.rows
                if r["id"] == quote_id
                and r["community_id"] == community_id
                and r["deleted_at"] is None
            ]
            cols = list(matches[0].keys()) if matches else []
            return _wrap_rows(cols, matches, rows_affected=len(matches))

        if "ORDER BY RANDOM()" in statement:
            community_id = py_params[0]
            matches = [
                r
                for r in self.rows
                if r["community_id"] == community_id
                and r["deleted_at"] is None
                and r["is_approved"]
            ]
            chosen = matches[:1]  # deterministic for tests
            cols = ["id", "quote_text", "quoted_username"]
            return _wrap_rows(cols, [{c: r[c] for c in cols} for r in chosen], len(chosen))

        if "ORDER BY created_at DESC LIMIT" in statement:
            community_id, limit = py_params[0], py_params[1]
            matches = [
                r for r in self.rows if r["community_id"] == community_id and r["deleted_at"] is None
            ]
            matches = sorted(matches, key=lambda r: r["created_at"], reverse=True)[:limit]
            cols = ["id", "quote_text", "quoted_username"]
            return _wrap_rows(cols, [{c: r[c] for c in cols} for r in matches], len(matches))

        if statement.startswith("UPDATE quotes SET deleted_at"):
            deleted_at, quote_id, community_id = py_params[0], py_params[1], py_params[2]
            updated = 0
            for r in self.rows:
                if (
                    r["id"] == quote_id
                    and r["community_id"] == community_id
                    and r["deleted_at"] is None
                ):
                    r["deleted_at"] = deleted_at
                    updated += 1
            return _wrap_rows([], [], rows_affected=updated)

        raise AssertionError(f"FakeWitDb: unrecognized statement: {statement!r}")


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """Fake WIT host: `flags.enabled` True by default, stateful `db`, recording `relay`/`log`."""
    fake_db = FakeWitDb()
    relay_calls: list[tuple[str, dict[str, Any]]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    db_mod = types.SimpleNamespace(
        execute=fake_db.execute,
        Value_NullValue=Value_NullValue,
        Value_BoolValue=Value_BoolValue,
        Value_IntValue=Value_IntValue,
        Value_FloatValue=Value_FloatValue,
        Value_TextValue=Value_TextValue,
        Value_BytesValue=Value_BytesValue,
        Rows=_Rows,
    )
    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_state["enabled"])
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: 0,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, db=db_mod, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        db=fake_db, relay_calls=relay_calls, log_calls=log_calls, flag_state=flag_state
    )


def _event(text: str, *, community_id: Any = 1, is_mod: bool | None = None, platform: str = "twitch") -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    if community_id is not None:
        payload["community_id"] = community_id
    if is_mod is not None:
        payload["is_mod"] = is_mod
    return PlatformEvent(platform=platform, event_type="chat.message", actor="actor-1", payload=payload)


def _reply_text(result: PlatformEvent | None) -> str | None:
    if result is None:
        return None
    text = result.payload.get("text")
    assert isinstance(text, str)
    return text


# --- feature flag / cheap-skip -----------------------------------------------------------


def test_non_quote_message_returns_none(fake_host: types.SimpleNamespace) -> None:
    result = _run(transform(_event("hello there")))
    assert result is None
    assert fake_host.db.calls == []


def test_flag_disabled_returns_none(fake_host: types.SimpleNamespace) -> None:
    fake_host.flag_state["enabled"] = False
    result = _run(transform(_event("!quote add hello")))
    assert result is None
    assert fake_host.db.calls == []


def test_non_string_text_payload_returns_none(fake_host: types.SimpleNamespace) -> None:
    event = PlatformEvent(platform="twitch", event_type="chat.message", actor="a", payload={"text": 5})
    assert _run(transform(event)) is None


# --- bare / usage -------------------------------------------------------------------------


def test_bare_quote_returns_usage(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote"))))
    assert reply is not None
    assert "Usage" in reply


def test_unknown_option_is_fail_loud_usage_error(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote frobnicate"))))
    assert reply is not None
    assert "unknown option" in reply
    assert "Usage" in reply


def test_unsupported_verb_reply(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote set foo", is_mod=True))))
    assert reply == "Unknown quote command 'set'. " + (
        "Usage: !quote add <text> | !quote <id> | !quote random | !quote list | !quote remove <id>"
    )


# --- add ----------------------------------------------------------------------------------


def test_add_requires_privilege(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote add hello world", is_mod=False))))
    assert reply == "Only the broadcaster or a moderator can add quotes."
    assert fake_host.db.calls == []


def test_add_fails_closed_when_role_info_absent(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote add hello world"))))
    assert reply == "Only the broadcaster or a moderator can add quotes."


def test_add_empty_text_usage(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote add", is_mod=True))))
    assert reply == "Usage: !quote add <text>"


def test_add_too_long_rejected(fake_host: types.SimpleNamespace) -> None:
    long_text = "x" * 501
    reply = _reply_text(_run(transform(_event(f"!quote add {long_text}", is_mod=True))))
    assert reply == "Quotes must be 500 characters or fewer."
    assert fake_host.db.calls == []


def test_add_success_as_mod(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote add hello world", is_mod=True, community_id=7))))
    assert reply == "Saved as quote #1."
    assert len(fake_host.db.rows) == 1
    row = fake_host.db.rows[0]
    assert row["quote_text"] == "hello world"
    assert row["community_id"] == 7
    assert row["platform"] == "twitch"


def test_add_success_as_broadcaster(fake_host: types.SimpleNamespace) -> None:
    event = _event("!quote add hi", community_id=1)
    event.payload["is_broadcaster"] = True
    reply = _reply_text(_run(transform(event)))
    assert reply == "Saved as quote #1."


# --- get by id ------------------------------------------------------------------------------


def test_get_by_id_not_found(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote 42"))))
    assert reply == "Quote #42 not found."


def test_get_by_id_found(fake_host: types.SimpleNamespace) -> None:
    _run(transform(_event("!quote add hello world", is_mod=True, community_id=1)))
    reply = _reply_text(_run(transform(_event("!quote 1", community_id=1))))
    assert reply == '#1: "hello world" — unknown'


def test_get_by_id_scoped_to_community(fake_host: types.SimpleNamespace) -> None:
    _run(transform(_event("!quote add hello world", is_mod=True, community_id=1)))
    reply = _reply_text(_run(transform(_event("!quote 1", community_id=999))))
    assert reply == "Quote #1 not found."


# --- random ---------------------------------------------------------------------------------


def test_random_no_quotes(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote random"))))
    assert reply == "No quotes found."


def test_random_found(fake_host: types.SimpleNamespace) -> None:
    _run(transform(_event("!quote add hello world", is_mod=True, community_id=1)))
    reply = _reply_text(_run(transform(_event("!quote random", community_id=1))))
    assert reply == '#1: "hello world" — unknown'


# --- list -----------------------------------------------------------------------------------


def test_list_empty(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote list"))))
    assert reply == "No quotes have been saved yet."


def test_list_with_items(fake_host: types.SimpleNamespace) -> None:
    _run(transform(_event("!quote add one", is_mod=True, community_id=1)))
    _run(transform(_event("!quote add two", is_mod=True, community_id=1)))
    reply = _reply_text(_run(transform(_event("!quote list", community_id=1))))
    assert reply is not None
    assert "#1" in reply
    assert "#2" in reply


# --- remove ---------------------------------------------------------------------------------


def test_remove_requires_privilege(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote remove 1", is_mod=False))))
    assert reply == "Only the broadcaster or a moderator can remove quotes."


def test_remove_invalid_id_usage(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote remove abc", is_mod=True))))
    assert reply == "Usage: !quote remove <id>"


def test_remove_not_found(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote remove 99", is_mod=True))))
    assert reply == "Quote #99 not found."


def test_remove_success_then_soft_deleted(fake_host: types.SimpleNamespace) -> None:
    _run(transform(_event("!quote add hello world", is_mod=True, community_id=1)))
    reply = _reply_text(_run(transform(_event("!quote remove 1", is_mod=True, community_id=1))))
    assert reply == "Removed quote #1."
    follow_up = _reply_text(_run(transform(_event("!quote 1", community_id=1))))
    assert follow_up == "Quote #1 not found."


# --- community_id / db failure fail-loud -----------------------------------------------------


def test_missing_community_id_fails_loud(fake_host: types.SimpleNamespace) -> None:
    reply = _reply_text(_run(transform(_event("!quote random", community_id=None))))
    assert reply == "Quote storage is unavailable right now - please try again."
    assert any("quote.community_id_missing" in call[1] for call in fake_host.log_calls)


def test_db_insert_failure_is_fail_loud(fake_host: types.SimpleNamespace) -> None:
    fake_host.db.fail_on_substring = "INSERT INTO quotes"
    reply = _reply_text(_run(transform(_event("!quote add hello", is_mod=True))))
    assert reply == "Something went wrong accessing quote storage - please try again."
    assert any("quote.db_failure" in call[1] for call in fake_host.log_calls)


def test_db_random_failure_is_fail_loud(fake_host: types.SimpleNamespace) -> None:
    fake_host.db.fail_on_substring = "ORDER BY RANDOM()"
    reply = _reply_text(_run(transform(_event("!quote random"))))
    assert reply == "Something went wrong accessing quote storage - please try again."


# --- dispatch ---------------------------------------------------------------------------------


def test_dispatch_relays_text(fake_host: types.SimpleNamespace) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "chan-1", "text": "#1: \"hi\" — unknown"},
    )
    envelope = StageEnvelope(
        tenant="t1", community="c1", app_id="waddles.core.example.quote", stage="action",
        event=event, ts="2026-10-05T00:00:00Z",
    )
    result = _run(dispatch(envelope, {}, http_client=None))
    assert isinstance(result, DispatchResult)
    assert result.transport == "twitch"
    assert len(fake_host.relay_calls) == 1
    provider, message_json = fake_host.relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {"channel": "chan-1", "text": "#1: \"hi\" — unknown"}


def test_dispatch_missing_channel_id_raises(fake_host: types.SimpleNamespace) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="chat.message", actor="a", payload={"text": "hi"}
    )
    envelope = StageEnvelope(
        tenant="t1", community="c1", app_id="waddles.core.example.quote", stage="action",
        event=event, ts="2026-10-05T00:00:00Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_missing_text_raises(fake_host: types.SimpleNamespace) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="chat.message", actor="a", payload={"channel_id": "chan-1"}
    )
    envelope = StageEnvelope(
        tenant="t1", community="c1", app_id="waddles.core.example.quote", stage="action",
        event=event, ts="2026-10-05T00:00:00Z",
    )
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(envelope, {}, http_client=None))
