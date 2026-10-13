"""Host-native tests for the `uptime` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`/
`clock`), same approach as `bundles/python/count/tests/test_app.py` and `time/tests`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    FLAG_KEY,
    START_KEY,
    DispatchResult,
    dispatch,
    humanize_elapsed,
    transform,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

_START_MS = 1_700_000_000_000
_MIN = 60_000
_HOUR = 60 * _MIN
_DAY = 24 * _HOUR


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class _FakeKv:
    """In-memory `kv` stand-in validating keys like the real host, with scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_ops: set[str] = set()

    def _check(self, op: str, key: str) -> None:
        if not key or len(key) > 256 or any(c not in _ALLOWED_KV_KEY_CHARS for c in key):
            raise _HostKvError(f"backend: invalid key {key!r}")
        self.calls.append((op, key))
        if op in self.fail_ops:
            raise _HostKvError(f"backend: scripted {op} failure")

    def get(self, key: str) -> bytes | None:
        self._check("get", key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        self._check("set", key)
        self.store[key] = bytes(value)


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.kv = _FakeKv()
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.flag_keys: list[str] = []
        self.now_ms = _START_MS


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flag on, in-memory kv, recording relay/log, settable clock."""
    h = _Host()

    def _flag(key: str, default_value: bool) -> bool:
        h.flag_keys.append(key)
        return h.flag_enabled

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_flag),
        kv=h.kv,
        relay=types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, json.loads(m)))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: h.log_calls.append((lvl, msg, fields)),
        ),
        clock=types.SimpleNamespace(
            now_millis=lambda: h.now_ms,
            now_rfc3339=lambda: "2023-11-14T22:13:20.000Z",
            monotonic_nanos=lambda: 0,
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return h


def _event(
    text: Any, *, channel_id: str | None = "12345", platform: str = "twitch"
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.uptime",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-13T00:00:00.000Z",
        ),
        ts="2026-10-13T00:00:00.000Z",
    )


def _reply(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "!time", "!uptimes", "uptime", "!count add x"])
def test_non_matching_text_returns_none_without_flag_or_kv(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 42, ["!uptime"]])
def test_non_string_text_returns_none(text: Any, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.kv.calls == []


def test_missing_text_key_returns_none(host: _Host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_flag_off_returns_none_and_touches_no_kv(host: _Host) -> None:
    host.flag_enabled = False
    assert _run(transform(_event("!uptime"))) is None
    assert host.flag_keys == [FLAG_KEY]
    assert FLAG_KEY == "waddles.command-uptime"
    assert host.kv.calls == []


def test_command_match_is_case_and_whitespace_insensitive(host: _Host) -> None:
    assert _reply(_run(transform(_event("  !UpTime  ")))) == "up 0s"


# --- first invocation / elapsed ---------------------------------------------------------


def test_first_invocation_records_start_and_reports_zero(host: _Host) -> None:
    result = _run(transform(_event("!uptime")))
    assert _reply(result) == "up 0s"
    assert host.kv.store == {START_KEY: str(_START_MS).encode()}
    assert host.kv.calls == [("get", START_KEY), ("set", START_KEY)]
    assert result is not None
    assert result.payload["channel_id"] == "12345"
    assert result.platform == "twitch"
    assert result.actor == "viewer-1"


def test_subsequent_invocation_computes_elapsed_without_rewriting(host: _Host) -> None:
    _run(transform(_event("!uptime")))
    host.kv.calls.clear()
    host.now_ms = _START_MS + 3 * _DAY + 4 * _HOUR + 12 * _MIN + 999
    assert _reply(_run(transform(_event("!uptime")))) == "up 3d 4h 12m"
    assert host.kv.calls == [("get", START_KEY)]  # never overwrites the recorded start
    assert host.kv.store[START_KEY] == str(_START_MS).encode()


def test_preexisting_start_is_used(host: _Host) -> None:
    host.kv.store[START_KEY] = str(_START_MS - 2 * _HOUR).encode()
    assert _reply(_run(transform(_event("!uptime")))) == "up 2h 0m"


def test_future_start_clamps_to_zero(host: _Host) -> None:
    host.kv.store[START_KEY] = str(_START_MS + _HOUR).encode()
    assert _reply(_run(transform(_event("!uptime")))) == "up 0s"


def test_start_key_satisfies_host_charset() -> None:
    assert START_KEY == "uptime.start"
    assert ":" not in START_KEY
    assert all(c in _ALLOWED_KV_KEY_CHARS for c in START_KEY)


# --- humanize ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("elapsed_ms", "expected"),
    [
        (-5_000, "up 0s"),
        (0, "up 0s"),
        (999, "up 0s"),
        (45_000, "up 45s"),
        (59_999, "up 59s"),
        (_MIN, "up 1m"),
        (59 * _MIN + 59_000, "up 59m"),
        (_HOUR, "up 1h 0m"),
        (_HOUR + 5 * _MIN, "up 1h 5m"),
        (_DAY, "up 1d 0h 0m"),
        (_DAY + 5 * _MIN, "up 1d 0h 5m"),
        (3 * _DAY + 4 * _HOUR + 12 * _MIN, "up 3d 4h 12m"),
        (400 * _DAY, "up 400d 0h 0m"),
    ],
)
def test_humanize_elapsed(elapsed_ms: int, expected: str) -> None:
    assert humanize_elapsed(elapsed_ms) == expected


# --- usage / grammar --------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!uptime reset", "!uptime set 5", "!uptime list"])
def test_option_replies_with_usage_and_does_not_touch_kv(text: str, host: _Host) -> None:
    assert _reply(_run(transform(_event(text)))) == "Usage: !uptime"
    assert host.kv.calls == []


def test_unknown_token_replies_with_parser_message(host: _Host) -> None:
    reply = _reply(_run(transform(_event("!uptime bogus"))))
    assert "unknown option" in reply
    assert "!uptime" in reply
    assert host.kv.calls == []


# --- kv failures fail loud --------------------------------------------------------------


@pytest.mark.parametrize("op", ["get", "set"])
def test_kv_failure_fails_loud_with_reply_and_error_log(op: str, host: _Host) -> None:
    host.kv.fail_ops.add(op)
    reply = _reply(_run(transform(_event("!uptime"))))
    assert "Something went wrong" in reply
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "uptime.kv_failure"
    assert "_HostKvError" in errors[0][2]
    assert host.kv.store == {}  # nothing half-written on a failed get


def test_kv_get_failure_on_existing_state_fails_loud(host: _Host) -> None:
    host.kv.store[START_KEY] = b"1"
    host.kv.fail_ops.add("get")
    assert "Something went wrong" in _reply(_run(transform(_event("!uptime"))))
    assert host.kv.store[START_KEY] == b"1"


@pytest.mark.parametrize("raw", [b"not-a-number", b"\xff\xfe", b""])
def test_corrupt_start_fails_loud_and_is_not_overwritten(raw: bytes, host: _Host) -> None:
    host.kv.store[START_KEY] = raw
    reply = _reply(_run(transform(_event("!uptime"))))
    assert "Something went wrong" in reply
    assert host.kv.store[START_KEY] == raw
    assert any(c[0] == 0 and c[1] == "uptime.kv_failure" for c in host.log_calls)


# --- PII-free logs ----------------------------------------------------------------------


def test_logs_never_contain_message_text_or_actor(host: _Host) -> None:
    typed_text = "!uptime SECRETARG-9f3"
    host.kv.store[START_KEY] = b"corrupt"
    _run(transform(_event(typed_text)))
    host.kv.store.clear()
    _run(transform(_event("!uptime")))
    _run(transform(_event("!uptime")))
    assert host.log_calls  # denominator: logging actually happened
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls)
    assert "SECRETARG" not in blob
    assert "viewer-1" not in blob


# --- eager imports ----------------------------------------------------------------------


def test_host_imports_are_eager_module_attributes() -> None:
    import app

    for name in ("kv", "log", "relay", "clock", "feature_enabled"):
        assert hasattr(app, name), f"{name} must be imported at module top (componentize-py)"


# --- dispatch ---------------------------------------------------------------------------


def test_dispatch_relays_text_to_channel(host: _Host) -> None:
    env = _envelope({"channel_id": "chan-9", "text": "up 3d 4h 12m"}, platform="discord")
    result = _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [("discord", {"channel": "chan-9", "text": "up 3d 4h 12m"})]
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_requires_channel_id(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"text": "up 1m"}), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, "", 5])
def test_dispatch_requires_text(text: Any, host: _Host) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope({"channel_id": "c", "text": text}), {}, http_client=None))
    assert host.relay_calls == []


def test_transform_then_dispatch_end_to_end(host: _Host) -> None:
    out = _run(transform(_event("!uptime", channel_id="chan-1")))
    assert out is not None
    env = _envelope(dict(out.payload))
    _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [("twitch", {"channel": "chan-1", "text": "up 0s"})]
