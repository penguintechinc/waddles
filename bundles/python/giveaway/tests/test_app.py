"""Host-native tests for the `giveaway` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`/
`clock`), same approach as `bundles/python/uptime/tests`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    ACTIVE_KEY,
    FLAG_KEY,
    MAX_DURATION_MS,
    MAX_ENTRIES,
    DispatchResult,
    DurationError,
    _pick_winner,
    _pseudonym,
    dispatch,
    humanize_duration,
    parse_duration_ms,
    transform,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

_T0 = 1_700_000_000_000
_MOD = {"is_mod": True, "is_broadcaster": False}
_NOBODY = {"is_mod": False, "is_broadcaster": False}


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
        self.now_ms = _T0

    def state(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.kv.store[ACTIVE_KEY])
        return loaded

    def seed(self, ends_at: int, entries: list[str]) -> None:
        self.kv.store[ACTIVE_KEY] = json.dumps({"ends_at": ends_at, "entries": entries}).encode()


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
    text: Any,
    *,
    actor: str | None = "viewer-1",
    badges: dict[str, Any] | None = None,
    channel_id: str | None = "12345",
    platform: str = "twitch",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    payload.update(badges or {})
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-13T00:00:00.000Z",
    )


def _say(text: str, **kwargs: Any) -> str:
    result = _run(transform(_event(text, **kwargs)))
    assert result is not None
    return str(result.payload["text"])


def _mod_say(text: str) -> str:
    return _say(text, actor="mod-1", badges=_MOD)


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="community-1",
        app_id="waddles.core.example.giveaway",
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


# --- routing / flag ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["hello", "", "!raffle", "!giveaways", "giveaway", "!entering", "!enterx a"]
)
def test_non_matching_text_returns_none_without_flag_or_kv(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("text", [None, 42, ["!giveaway"]])
def test_non_string_text_returns_none(text: Any, host: _Host) -> None:
    assert _run(transform(_event(text))) is None
    assert host.kv.calls == []


def test_flag_off_returns_none_and_touches_no_kv(host: _Host) -> None:
    host.flag_enabled = False
    assert _run(transform(_event("!enter"))) is None
    assert _run(transform(_event("!giveaway start 5m", badges=_MOD))) is None
    assert set(host.flag_keys) == {FLAG_KEY}
    assert FLAG_KEY == "waddles.command-giveaway"
    assert host.kv.calls == []


def test_command_match_is_case_and_whitespace_insensitive(host: _Host) -> None:
    assert "no giveaway" in _say("  !GiveAway   STATUS  ")
    assert "no giveaway" in _say("!ENTER")


def test_key_satisfies_host_charset() -> None:
    assert ACTIVE_KEY == "giveaway.active"
    assert ":" not in ACTIVE_KEY
    assert all(c in _ALLOWED_KV_KEY_CHARS for c in ACTIVE_KEY)


# --- duration parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [("30s", 30_000), ("5m", 300_000), ("1h", 3_600_000), ("1S", 1000), ("24h", MAX_DURATION_MS)],
)
def test_parse_duration_ok(token: str, expected: int) -> None:
    assert parse_duration_ms(token) == expected


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("5", "not a valid duration"),
        ("m5", "not a valid duration"),
        ("5d", "not a valid duration"),
        ("-5m", "not a valid duration"),
        ("1.5h", "not a valid duration"),
        ("abc", "not a valid duration"),
        ("9999999m", "not a valid duration"),
        ("0s", "at least 1s"),
        ("0h", "at least 1s"),
        ("25h", "at most 24h"),
        ("1441m", "at most 24h"),
        ("999999s", "at most 24h"),
    ],
)
def test_parse_duration_rejects(token: str, fragment: str) -> None:
    with pytest.raises(DurationError, match=fragment):
        parse_duration_ms(token)


@pytest.mark.parametrize("token", ["soon", "0m", "48h", "5"])
def test_start_with_bad_duration_replies_explicitly_and_stores_nothing(
    token: str, host: _Host
) -> None:
    reply = _mod_say(f"!giveaway start {token}")
    assert "duration" in reply
    assert ACTIVE_KEY not in host.kv.store


@pytest.mark.parametrize(
    ("ms", "expected"),
    [(-1, "0s"), (0, "0s"), (999, "0s"), (45_000, "45s"), (61_000, "1m 1s"),
     (3_600_000, "1h 0m 0s"), (3_725_000, "1h 2m 5s")],
)  # fmt: skip
def test_humanize_duration(ms: int, expected: str) -> None:
    assert humanize_duration(ms) == expected


# --- start ------------------------------------------------------------------------------


def test_start_by_mod_opens_giveaway(host: _Host) -> None:
    reply = _mod_say("!giveaway start 5m")
    assert "5m 0s" in reply
    assert "!enter" in reply
    assert host.state() == {"ends_at": _T0 + 300_000, "entries": []}


def test_start_by_broadcaster_opens_giveaway(host: _Host) -> None:
    reply = _say("!giveaway start 30s", badges={"is_mod": False, "is_broadcaster": True})
    assert "30s" in reply
    assert host.state()["ends_at"] == _T0 + 30_000


def test_start_while_active_is_rejected_and_state_unchanged(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    _say("!enter")
    before = host.kv.store[ACTIVE_KEY]
    host.now_ms = _T0 + 60_000
    reply = _mod_say("!giveaway start 1h")
    assert "already running" in reply
    assert "4m 0s left" in reply
    assert host.kv.store[ACTIVE_KEY] == before


def test_start_while_ended_awaiting_draw_is_rejected(host: _Host) -> None:
    host.seed(_T0 - 1, ["a"])
    reply = _mod_say("!giveaway start 5m")
    assert "has ended" in reply
    assert "draw" in reply
    assert host.state()["entries"] == ["a"]


@pytest.mark.parametrize("text", ["!giveaway start", "!giveaway start 5m 10m"])
def test_start_wrong_arity_replies_with_usage(text: str, host: _Host) -> None:
    assert "Usage:" in _mod_say(text)
    assert host.kv.calls == []


# --- enter ------------------------------------------------------------------------------


def test_enter_adds_hashed_entry_once(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    reply = _say("!enter", actor="viewer-1")
    assert "entered the giveaway" in reply
    assert "(1 entered)" in reply
    expected = hashlib.sha256("twitch\x1fviewer-1".encode()).hexdigest()
    assert host.state()["entries"] == [expected] == [_pseudonym("twitch", "viewer-1")]
    assert "viewer-1" not in host.kv.store[ACTIVE_KEY].decode()  # raw id never persisted


def test_enter_duplicate_is_rejected_without_growth(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    _say("!enter")
    reply = _say("!enter")
    assert "already entered" in reply
    assert len(host.state()["entries"]) == 1


def test_enter_alias_via_giveaway_enter(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    assert "entered the giveaway" in _say("!giveaway enter")
    assert len(host.state()["entries"]) == 1


def test_distinct_users_and_platforms_get_distinct_entries(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    _say("!enter", actor="u1")
    _say("!enter", actor="u2")
    _say("!enter", actor="u1", platform="discord")
    assert len(set(host.state()["entries"])) == 3


def test_enter_when_none_active(host: _Host) -> None:
    reply = _say("!enter")
    assert "no giveaway is running" in reply
    assert ACTIVE_KEY not in host.kv.store


def test_enter_after_it_ended_is_rejected(host: _Host) -> None:
    _mod_say("!giveaway start 1m")
    host.now_ms = _T0 + 60_000  # exactly at the deadline == ended
    assert "has ended" in _say("!enter")
    assert host.state()["entries"] == []


def test_enter_without_actor_is_rejected(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    assert "couldn't identify" in _say("!enter", actor=None)
    assert host.state()["entries"] == []


def test_enter_with_trailing_text_is_usage(host: _Host) -> None:
    assert "Usage:" in _say("!enter now please")
    assert host.kv.calls == []


def test_enter_cap_is_enforced(host: _Host) -> None:
    host.seed(_T0 + 60_000, [f"{i:064x}" for i in range(MAX_ENTRIES)])
    assert "full" in _say("!enter")
    assert len(host.state()["entries"]) == MAX_ENTRIES


# --- draw -------------------------------------------------------------------------------


def test_draw_picks_a_real_entrant_announces_and_closes(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    for user in ("u1", "u2", "u3"):
        _say("!enter", actor=user)
    entries = host.state()["entries"]
    reply = _mod_say("!giveaway draw")
    assert any(f"entrant {e[:8]}" in reply for e in entries)
    assert "out of 3 entries" in reply
    assert ACTIVE_KEY not in host.kv.store  # closed


def test_draw_single_entry_always_that_entrant(host: _Host) -> None:
    host.seed(_T0 + 60_000, ["ab" * 32])
    assert "entrant abababab" in _mod_say("!giveaway draw")


def test_draw_before_deadline_is_allowed(host: _Host) -> None:
    host.seed(_T0 + 3_600_000, ["cd" * 32])
    assert "entrant cdcdcdcd" in _mod_say("!giveaway draw")


def test_draw_after_deadline_is_allowed(host: _Host) -> None:
    host.seed(_T0 - 5_000, ["ef" * 32])
    assert "entrant efefefef" in _mod_say("!giveaway draw")


def test_draw_with_no_entries_replies_explicitly_and_keeps_giveaway(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    reply = _mod_say("!giveaway draw")
    assert "no entries" in reply
    assert ACTIVE_KEY in host.kv.store


def test_draw_when_none_active(host: _Host) -> None:
    assert "nothing to draw" in _mod_say("!giveaway draw")


def test_pick_winner_is_deterministic_for_same_seed_inputs_and_varies_with_clock() -> None:
    entries = [f"{i:064x}" for i in range(50)]
    assert _pick_winner(entries, _T0) == _pick_winner(entries, _T0)
    winners = {_pick_winner(entries, _T0 + i) for i in range(200)}
    assert len(winners) > 10  # seeded from the clock, so draws are not pinned to one entrant
    assert winners <= set(entries)


def test_pick_winner_depends_on_entries() -> None:
    a = [f"{i:064x}" for i in range(20)]
    b = [f"{i:064x}" for i in range(1, 21)]
    assert {_pick_winner(a, _T0 + i) for i in range(50)} != {
        _pick_winner(b, _T0 + i) for i in range(50)
    }


# --- cancel / status --------------------------------------------------------------------


def test_cancel_closes_without_drawing(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    _say("!enter")
    reply = _mod_say("!giveaway cancel")
    assert "cancelled" in reply
    assert ACTIVE_KEY not in host.kv.store
    assert "no giveaway is running" in _say("!enter")


def test_cancel_when_none_active(host: _Host) -> None:
    assert "nothing to cancel" in _mod_say("!giveaway cancel")


def test_cancel_recovers_a_corrupt_state(host: _Host) -> None:
    host.kv.store[ACTIVE_KEY] = b"{garbage"
    assert "cancelled" in _mod_say("!giveaway cancel")
    assert ACTIVE_KEY not in host.kv.store


def test_status_running_and_ended_and_none(host: _Host) -> None:
    assert _say("!giveaway status") == "no giveaway is running right now"
    _mod_say("!giveaway start 5m")
    _say("!enter", actor="u1")
    _say("!enter", actor="u2")
    host.now_ms = _T0 + 90_000
    assert _say("!giveaway status") == "giveaway running: 2 entries, 3m 30s left"
    host.now_ms = _T0 + 300_000
    ended = _say("!giveaway status")
    assert "ended with 2 entries" in ended
    assert "draw" in ended


def test_status_is_public_and_read_only(host: _Host) -> None:
    host.seed(_T0 + 1000, ["a"])
    before = dict(host.kv.store)
    assert "giveaway running" in _say("!giveaway status", badges=_NOBODY)
    assert host.kv.store == before
    assert all(op == "get" for op, _ in host.kv.calls)


# --- usage ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["!giveaway", "!giveaway bogus", "!giveaway status now", "!giveaway draw now",
     "!giveaway cancel x", "!giveaway enter x"],
)  # fmt: skip
def test_unknown_or_malformed_shapes_reply_with_usage(text: str, host: _Host) -> None:
    assert "Usage:" in _say(text, badges=_MOD)
    assert host.kv.calls == []


# --- mod gate: fail closed --------------------------------------------------------------

_HOSTILE_BADGES: list[dict[str, Any]] = [
    {},
    {"is_mod": False, "is_broadcaster": False},
    {"is_mod": "true", "is_broadcaster": False},
    {"is_mod": "false", "is_broadcaster": "false"},
    {"is_mod": 1, "is_broadcaster": 0},
    {"is_mod": None, "is_broadcaster": None},
    {"is_mod": "yes"},
]


@pytest.mark.parametrize("command", ["start 5m", "draw", "cancel"])
@pytest.mark.parametrize("badges", _HOSTILE_BADGES)
def test_mod_commands_fail_closed_for_non_mods(
    command: str, badges: dict[str, Any], host: _Host
) -> None:
    host.seed(_T0 + 60_000, ["a"])
    before = dict(host.kv.store)
    reply = _say(f"!giveaway {command}", badges=badges)
    assert reply == "only moderators/broadcasters can do that"
    assert host.kv.store == before
    assert host.kv.calls == []  # denied before any kv access


def test_mod_commands_admit_real_booleans_only(host: _Host) -> None:
    assert "open for" in _say("!giveaway start 5m", badges={"is_mod": True})
    assert "no entries" in _say("!giveaway draw", badges={"is_broadcaster": True})


# --- kv failures fail loud --------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "text", "badges"),
    [
        ("get", "!giveaway start 5m", _MOD),
        ("get", "!enter", None),
        ("get", "!giveaway status", None),
        ("get", "!giveaway draw", _MOD),
        ("get", "!giveaway cancel", _MOD),
    ],
)
def test_kv_get_failure_fails_loud(
    op: str, text: str, badges: dict[str, Any] | None, host: _Host
) -> None:
    host.seed(_T0 + 60_000, ["a"])
    host.kv.fail_ops.add(op)
    before = dict(host.kv.store)
    assert "Something went wrong" in _say(text, badges=badges)
    assert host.kv.store == before
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 1
    assert errors[0][1] == "giveaway.kv_failure"
    assert "_HostKvError" in errors[0][2]


def test_kv_set_failure_on_start_and_enter_fails_loud(host: _Host) -> None:
    host.kv.fail_ops.add("set")
    assert "Something went wrong" in _mod_say("!giveaway start 5m")
    assert ACTIVE_KEY not in host.kv.store
    host.kv.fail_ops.clear()
    _mod_say("!giveaway start 5m")
    host.kv.fail_ops.add("set")
    assert "Something went wrong" in _say("!enter")
    assert host.state()["entries"] == []


@pytest.mark.parametrize("text", ["!giveaway draw", "!giveaway cancel"])
def test_kv_delete_failure_fails_loud_and_does_not_announce(text: str, host: _Host) -> None:
    host.seed(_T0 + 60_000, ["ab" * 32])
    host.kv.fail_ops.add("delete")
    reply = _mod_say(text)
    assert "Something went wrong" in reply
    assert "winner" not in reply
    assert ACTIVE_KEY in host.kv.store


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (b"not json", "JSONDecodeError"),
        (b"\xff\xfe", "UnicodeDecodeError"),
        (b"[1]", "NotAnObject"),
        (b'{"entries": []}', "BadEndsAt"),
        (b'{"ends_at": "x", "entries": []}', "BadEndsAt"),
        (b'{"ends_at": true, "entries": []}', "BadEndsAt"),
        (b'{"ends_at": 1}', "BadEntries"),
        (b'{"ends_at": 1, "entries": [1]}', "BadEntries"),
        (b'{"ends_at": 1, "entries": "a"}', "BadEntries"),
    ],
)
def test_corrupt_state_fails_loud_and_is_not_overwritten(
    raw: bytes, kind: str, host: _Host
) -> None:
    host.kv.store[ACTIVE_KEY] = raw
    for text, badges in (
        ("!enter", None),
        ("!giveaway status", None),
        ("!giveaway draw", _MOD),
        ("!giveaway start 5m", _MOD),
    ):
        assert "Something went wrong" in _say(text, badges=badges)
    assert host.kv.store[ACTIVE_KEY] == raw
    errors = [c for c in host.log_calls if c[0] == 0]
    assert len(errors) == 4
    assert all(kind in c[2] for c in errors)


# --- PII-free logs ----------------------------------------------------------------------


def test_logs_never_contain_message_text_or_actor(host: _Host) -> None:
    _mod_say("!giveaway start 5m")
    _say("!enter", actor="viewer-SECRETNAME")
    _say("!enter", actor="viewer-SECRETNAME")
    _say("!giveaway start SECRETARG-9f3", badges=_MOD)
    _say("!giveaway bogus SECRETARG-9f3", badges=_MOD)
    _say("!giveaway draw", actor="viewer-SECRETNAME", badges=_NOBODY)
    _mod_say("!giveaway draw")
    host.kv.store[ACTIVE_KEY] = b"corrupt"
    _say("!enter", actor="viewer-SECRETNAME")
    assert host.log_calls  # denominator: logging actually happened
    blob = " ".join(f"{m} {f}" for _, m, f in host.log_calls)
    assert "SECRETARG" not in blob
    assert "SECRETNAME" not in blob
    assert "viewer-" not in blob
    assert "mod-1" not in blob


# --- eager imports ----------------------------------------------------------------------


def test_host_imports_are_eager_module_attributes() -> None:
    import app

    for name in ("kv", "log", "relay", "clock", "feature_enabled"):
        assert hasattr(app, name), f"{name} must be imported at module top (componentize-py)"


# --- dispatch ---------------------------------------------------------------------------


def test_dispatch_relays_text_to_channel(host: _Host) -> None:
    env = _envelope({"channel_id": "chan-9", "text": "hi"}, platform="discord")
    result = _run(dispatch(env, {}, http_client=None))
    assert host.relay_calls == [("discord", {"channel": "chan-9", "text": "hi"})]
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


def test_full_lifecycle_transform_then_dispatch(host: _Host) -> None:
    def roundtrip(text: str, **kwargs: Any) -> str:
        out = _run(transform(_event(text, channel_id="chan-1", **kwargs)))
        assert out is not None
        _run(dispatch(_envelope(dict(out.payload)), {}, http_client=None))
        return str(host.relay_calls[-1][1]["text"])

    assert "open for 1m 0s" in roundtrip("!giveaway start 1m", actor="mod-1", badges=_MOD)
    assert "(1 entered)" in roundtrip("!enter", actor="u1")
    assert "(2 entered)" in roundtrip("!enter", actor="u2")
    assert "already entered" in roundtrip("!enter", actor="u1")
    assert "2 entries" in roundtrip("!giveaway status")
    host.now_ms = _T0 + 61_000
    assert "has ended" in roundtrip("!enter", actor="u3")
    assert "out of 2 entries" in roundtrip("!giveaway draw", actor="mod-1", badges=_MOD)
    assert "no giveaway is running" in roundtrip("!giveaway status")
    assert {c[1]["channel"] for c in host.relay_calls} == {"chan-1"}
