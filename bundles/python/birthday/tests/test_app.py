"""Host-native tests for the `birthday` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`),
same approach as `bundles/python/uptime/tests/test_app.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandUsageError
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    FLAG_KEY,
    KEY_PREFIX,
    DispatchResult,
    dispatch,
    format_month_day,
    parse_month_day,
    transform,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_key(platform: str, user_id: str) -> str:
    digest = hashlib.sha256(f"{platform}:{user_id.lower()}".encode()).hexdigest()
    return f"birthday.{digest}"


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

    def delete(self, key: str) -> None:
        self._check("delete", key)
        self.store.pop(key, None)


class _Host:
    """Recorded fake-host state shared with the test body."""

    def __init__(self) -> None:
        self.kv = _FakeKv()
        self.relay_calls: list[tuple[str, dict[str, Any]]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.flag_keys: list[str] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Fake WIT host: flag on, in-memory kv, recording relay/log."""
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
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return h


def _event(
    text: Any,
    *,
    actor: str | None = "viewer-1",
    channel_id: str | None = "12345",
    platform: str = "twitch",
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.birthday",
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


def _say(host: _Host, text: Any, **kwargs: Any) -> str:
    result = _run(transform(_event(text, **kwargs)))
    assert result is not None
    return str(result.payload["text"])


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "", "!time", "!birthdays", "birthday", "!bday"])
def test_non_matching_text_returns_none_without_flag_or_kv(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 42, ["!birthday"]])
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
    assert _run(transform(_event("!birthday set 03-25"))) is None
    assert host.flag_keys == [FLAG_KEY]
    assert FLAG_KEY == "waddles.command-birthday"
    assert host.kv.calls == []


def test_command_match_is_case_and_whitespace_insensitive(host: _Host) -> None:
    assert "haven't set" in _say(host, "  !BirthDay  ")


def test_reply_preserves_channel_platform_actor(host: _Host) -> None:
    result = _run(transform(_event("!birthday", channel_id="chan-7", platform="discord")))
    assert result is not None
    assert result.payload["channel_id"] == "chan-7"
    assert result.platform == "discord"
    assert result.actor == "viewer-1"


# --- set own -----------------------------------------------------------------------------


def test_set_own_valid_stores_month_day_only_under_hashed_key(host: _Host) -> None:
    reply = _say(host, "!birthday set 03-25")
    assert reply == "Birthday saved: March 25 (month and day only)."
    key = _expected_key("twitch", "viewer-1")
    assert host.kv.store == {key: b"03-25"}
    assert host.kv.calls == [("set", key)]
    assert key.startswith(KEY_PREFIX)
    assert "viewer-1" not in key
    assert ":" not in key
    assert all(c in _ALLOWED_KV_KEY_CHARS for c in key)


def test_set_is_not_mod_gated(host: _Host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="plain-viewer",
        payload={"text": "!birthday set 12-01", "channel_id": "c", "is_mod": False},
        occurred_at="",
    )
    result = _run(transform(event))
    assert result is not None
    assert "Birthday saved: December 1" in str(result.payload["text"])


def test_set_overwrites_existing(host: _Host) -> None:
    _say(host, "!birthday set 03-25")
    _say(host, "!birthday set 07-04")
    assert list(host.kv.store.values()) == [b"07-04"]


def test_set_normalizes_to_zero_padded_value(host: _Host) -> None:
    _say(host, "!birthday SET 01-05")
    assert list(host.kv.store.values()) == [b"01-05"]


def test_leap_day_is_accepted(host: _Host) -> None:
    reply = _say(host, "!birthday set 02-29")
    assert reply == "Birthday saved: February 29 (month and day only)."
    assert list(host.kv.store.values()) == [b"02-29"]
    assert "February 29" in _say(host, "!birthday")


def test_set_without_actor_replies_explicitly_and_stores_nothing(host: _Host) -> None:
    reply = _say(host, "!birthday set 03-25", actor=None)
    assert "couldn't tell who you are" in reply
    assert host.kv.calls == []


# --- invalid dates (explicit reply, nothing stored) --------------------------------------


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("!birthday set 02-30", "not a real date"),
        ("!birthday set 04-31", "not a real date"),
        ("!birthday set 13-05", "invalid month"),
        ("!birthday set 00-10", "invalid month"),
        ("!birthday set 05-00", "not a real date"),
        ("!birthday set abc", "not a valid date"),
        ("!birthday set 3-5", "not a valid date"),
        ("!birthday set 1990-03-25", "not a valid date"),
        ("!birthday set 03/25", "not a valid date"),
        ("!birthday set ٠٣-٢٥", "not a valid date"),
    ],
)
def test_invalid_date_is_rejected_with_explicit_reply(
    text: str, fragment: str, host: _Host
) -> None:
    reply = _say(host, text)
    assert fragment in reply
    assert host.kv.calls == []
    assert host.kv.store == {}


def test_set_missing_date_replies_usage(host: _Host) -> None:
    reply = _say(host, "!birthday set")
    assert reply == "Usage: !birthday set <MM-DD> (e.g. !birthday set 03-25)"
    assert host.kv.calls == []


def test_set_extra_tokens_replies_usage(host: _Host) -> None:
    reply = _say(host, "!birthday set 03-25 1990")
    assert reply.startswith("Usage: !birthday set <MM-DD>")
    assert host.kv.calls == []


# --- read own ----------------------------------------------------------------------------


def test_read_own_after_set(host: _Host) -> None:
    _say(host, "!birthday set 03-25")
    host.kv.calls.clear()
    assert _say(host, "!birthday") == "Your birthday is March 25."
    assert host.kv.calls == [("get", _expected_key("twitch", "viewer-1"))]


def test_read_own_when_unset(host: _Host) -> None:
    assert _say(host, "!birthday") == "You haven't set your birthday - use !birthday set MM-DD."


def test_users_are_isolated_and_platforms_do_not_collide(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="alice")
    _say(host, "!birthday set 09-09", actor="bob")
    _say(host, "!birthday set 11-11", actor="alice", platform="discord")
    assert _say(host, "!birthday", actor="alice") == "Your birthday is March 25."
    assert _say(host, "!birthday", actor="bob") == "Your birthday is September 9."
    assert _say(host, "!birthday", actor="alice", platform="discord") == (
        "Your birthday is November 11."
    )
    assert len(host.kv.store) == 3


# --- read other --------------------------------------------------------------------------


def test_read_other_by_at_name_resolves_to_same_key_as_their_own_set(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="Alice")
    host.kv.calls.clear()
    reply = _say(host, "!birthday @alice", actor="bob")
    assert reply == "@alice's birthday is March 25."
    assert host.kv.calls == [("get", _expected_key("twitch", "alice"))]


def test_read_other_is_case_insensitive_on_name(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="alice")
    assert "March 25" in _say(host, "!birthday @ALICE", actor="bob")


@pytest.mark.parametrize("mention", ["<@123456789>", "<@!123456789>"])
def test_read_other_by_discord_mention(mention: str, host: _Host) -> None:
    _say(host, "!birthday set 12-31", actor="123456789", platform="discord")
    reply = _say(host, f"!birthday {mention}", actor="999", platform="discord")
    assert reply == f"{mention}'s birthday is December 31."


def test_read_other_unset(host: _Host) -> None:
    assert _say(host, "!birthday @ghost") == "@ghost hasn't set a birthday."


def test_read_other_needs_no_caller_identity(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="alice")
    assert "March 25" in _say(host, "!birthday @alice", actor=None)


def test_read_self_via_at_name_matches_own(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="viewer-1")
    assert "March 25" in _say(host, "!birthday @viewer-1")


# --- clear -------------------------------------------------------------------------------


def test_clear_removes_own_birthday(host: _Host) -> None:
    _say(host, "!birthday set 03-25")
    assert _say(host, "!birthday clear") == "Your birthday has been cleared."
    assert host.kv.store == {}
    assert "haven't set" in _say(host, "!birthday")


def test_clear_when_unset_does_not_delete(host: _Host) -> None:
    assert _say(host, "!birthday clear") == "You don't have a birthday set."
    assert [op for op, _ in host.kv.calls] == ["get"]


def test_clear_only_removes_callers_own(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="alice")
    _say(host, "!birthday set 04-04", actor="bob")
    _say(host, "!birthday clear", actor="alice")
    assert list(host.kv.store.values()) == [b"04-04"]


def test_clear_without_actor_replies_explicitly(host: _Host) -> None:
    assert "couldn't tell who you are" in _say(host, "!birthday clear", actor=None)
    assert host.kv.calls == []


# --- usage / grammar ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "!birthday 03-25",
        "!birthday alice",
        "!birthday remove",
        "!birthday add 03-25",
        "!birthday list",
        "!birthday enable",
        "!birthday clear now",
        "!birthday @alice @bob",
        "!birthday @",
        "!birthday <@abc>",
    ],
)
def test_malformed_input_replies_usage_and_touches_no_kv(text: str, host: _Host) -> None:
    reply = _say(host, text)
    assert "Usage:" in reply or "sub-module" in reply
    assert host.kv.calls == []


def test_forgotten_set_is_not_treated_as_a_username(host: _Host) -> None:
    reply = _say(host, "!birthday 03-25")
    assert reply.startswith("Usage: !birthday")
    assert "month and day only" in reply


# --- pure helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("01-01", (1, 1)), ("02-29", (2, 29)), ("12-31", (12, 31)), ("04-30", (4, 30))],
)
def test_parse_month_day_accepts(raw: str, expected: tuple[int, int]) -> None:
    assert parse_month_day(raw) == expected


@pytest.mark.parametrize("raw", ["02-30", "13-01", "00-01", "01-32", "06-31", "x", "", "1-1"])
def test_parse_month_day_rejects(raw: str) -> None:
    with pytest.raises(CommandUsageError):
        parse_month_day(raw)


def test_format_month_day() -> None:
    assert format_month_day(1, 5) == "January 5"
    assert format_month_day(12, 31) == "December 31"


# --- kv failures fail loud ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "text", "seed"),
    [
        ("get", "!birthday", False),
        ("get", "!birthday @alice", False),
        ("get", "!birthday clear", False),
        ("set", "!birthday set 03-25", False),
        ("delete", "!birthday clear", True),
    ],
)
def test_kv_failure_fails_loud_with_reply_and_error_log(
    op: str, text: str, seed: bool, host: _Host
) -> None:
    if seed:
        host.kv.store[_expected_key("twitch", "viewer-1")] = b"03-25"
    before = dict(host.kv.store)
    host.kv.fail_ops.add(op)
    reply = _say(host, text)
    assert "Something went wrong" in reply
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "birthday.kv_failure"
    assert "_HostKvError" in errors[0][2]
    assert host.kv.store == before


@pytest.mark.parametrize("raw", [b"not-a-date", b"\xff\xfe", b"", b"02-30", b"1990-03-25"])
def test_corrupt_stored_value_fails_loud_and_is_not_overwritten(raw: bytes, host: _Host) -> None:
    key = _expected_key("twitch", "viewer-1")
    host.kv.store[key] = raw
    assert "Something went wrong" in _say(host, "!birthday")
    assert host.kv.store[key] == raw
    assert any(c[0] == 0 and c[1] == "birthday.kv_failure" for c in host.log_calls)


# --- PII-free logs -----------------------------------------------------------------------


def test_logs_never_contain_birthday_actor_or_target(host: _Host) -> None:
    _say(host, "!birthday set 03-25", actor="SecretActorXyz")
    _say(host, "!birthday @TargetPerson42", actor="SecretActorXyz")
    _say(host, "!birthday set 02-30 ", actor="SecretActorXyz")
    _say(host, "!birthday clear", actor="SecretActorXyz")
    host.kv.store[_expected_key("twitch", "SecretActorXyz")] = b"corrupt"
    _say(host, "!birthday", actor="SecretActorXyz")
    host.kv.fail_ops.add("get")
    _say(host, "!birthday", actor="SecretActorXyz")
    assert host.log_calls  # denominator: logging actually happened
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls).lower()
    for needle in ("secretactorxyz", "targetperson42", "03-25", "02-30", "march"):
        assert needle not in blob


# --- eager imports -----------------------------------------------------------------------


def test_host_imports_are_eager_module_attributes() -> None:
    import app

    for name in ("kv", "log", "relay", "feature_enabled"):
        assert hasattr(app, name), f"{name} must be imported at module top (componentize-py)"


# --- dispatch ----------------------------------------------------------------------------


def test_dispatch_relays_text_to_channel(host: _Host) -> None:
    env = _envelope({"channel_id": "chan-9", "text": "Your birthday is March 25."}, "discord")
    result = _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [
        ("discord", {"channel": "chan-9", "text": "Your birthday is March 25."})
    ]
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_requires_channel_id(host: _Host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"text": "hi"}), {}, http_client=None))
    assert host.relay_calls == []


@pytest.mark.parametrize("text", [None, "", 5])
def test_dispatch_requires_text(text: Any, host: _Host) -> None:
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope({"channel_id": "c", "text": text}), {}, http_client=None))
    assert host.relay_calls == []


def test_transform_then_dispatch_end_to_end(host: _Host) -> None:
    out = _run(transform(_event("!birthday set 03-25", channel_id="chan-1")))
    assert out is not None
    _run(dispatch(_envelope(dict(out.payload)), {}, http_client=None))
    assert host.relay_calls == [
        ("twitch", {"channel": "chan-1", "text": "Birthday saved: March 25 (month and day only)."})
    ]
