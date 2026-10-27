"""Host-native tests for the `memories` bundle's `transform`/`dispatch` logic.

No WASM -- a fake `wit_world` stands in for the WIT host imports (`kv`/`flags`/`relay`/`log`),
same approach as `bundles/python/uptime/tests/test_app.py` and `count/tests`.
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
    KEY_PREFIX,
    MAX_MEMORIES,
    MAX_REPLY_CHARS,
    MAX_TEXT_CHARS,
    DispatchResult,
    dispatch,
    transform,
)

# Mirrors `core/bundle_host_kv/src/scope.rs::is_allowed_key_byte` -- notably NOT `:`.
_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)

_TS = "2026-10-13T00:00:00.000Z"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _key(platform: str, identity: str) -> str:
    return KEY_PREFIX + hashlib.sha256(f"{platform}:{identity}".encode()).hexdigest()


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
        self.log_calls: list[tuple[Any, str, str]] = []
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
    platform: str = "discord",
    actor: str | None = "viewer-1",
    author_id: str | None = "111",
    occurred_at: str = _TS,
    channel_id: str | None = "12345",
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if author_id is not None:
        payload["author_id"] = author_id
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at=occurred_at,
    )


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.memories",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at=_TS,
        ),
        ts=_TS,
    )


def _say(text: Any, **kw: Any) -> str:
    result = _run(transform(_event(text, **kw)))
    assert result is not None
    return str(result.payload["text"])


def _stored(host: _Host, platform: str, identity: str) -> list[dict[str, Any]]:
    raw = host.kv.store[_key(platform, identity)]
    data = json.loads(raw.decode("utf-8"))
    assert isinstance(data, list)
    return data


def _seed(host: _Host, platform: str, identity: str, texts: list[str]) -> None:
    blob = [{"id": i, "text": t, "ts": _TS} for i, t in enumerate(texts, start=1)]
    host.kv.store[_key(platform, identity)] = json.dumps(blob).encode("utf-8")


def _logged(host: _Host) -> str:
    return " ".join(f"{msg} {fields}" for _, msg, fields in host.log_calls)


# --- routing / gating --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", [123, None, "", "   ", "hello", "!remembers x", "!other", "x !forget"]
)
def test_non_matching_payloads_return_none(host: _Host, text: Any) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []
    assert host.kv.calls == []


@pytest.mark.parametrize("head", ["!REMEMBER", "!Remember"])
def test_command_head_is_case_insensitive(host: _Host, head: str) -> None:
    assert _say(f"{head} hi").startswith("Remembered as #1")


def test_reply_event_carries_channel_and_context(host: _Host) -> None:
    result = _run(transform(_event("!memories", channel_id="chan-9")))
    assert result is not None
    assert result.payload["channel_id"] == "chan-9"
    assert result.platform == "discord"
    assert result.actor == "viewer-1"


# --- !remember ---------------------------------------------------------------------------


def test_remember_appends_and_persists_one_hashed_key(host: _Host) -> None:
    assert _say("!remember my cat is called Waffles") == "Remembered as #1 (1/20)."
    assert _say("!remember I like tea") == "Remembered as #2 (2/20)."
    assert list(host.kv.store) == [_key("discord", "111")]
    assert _stored(host, "discord", "111") == [
        {"id": 1, "text": "my cat is called Waffles", "ts": _TS},
        {"id": 2, "text": "I like tea", "ts": _TS},
    ]


def test_remember_key_is_hashed_colon_free_and_pii_free(host: _Host) -> None:
    _say("!remember hi")
    (key,) = host.kv.store
    assert key.startswith("memories.")
    assert ":" not in key
    assert "111" not in key
    assert "viewer-1" not in key


def test_remember_usage_when_empty(host: _Host) -> None:
    assert _say("!remember").startswith("Usage: !remember")
    assert host.kv.calls == []


def test_remember_exactly_max_chars_ok(host: _Host) -> None:
    assert _say("!remember " + "a" * MAX_TEXT_CHARS).startswith("Remembered as #1")


def test_remember_too_long_rejected(host: _Host) -> None:
    reply = _say("!remember " + "a" * (MAX_TEXT_CHARS + 1))
    assert "too long" in reply
    assert host.kv.store == {}
    assert host.kv.calls == []


@pytest.mark.parametrize(
    "text", ["!remember line one\nline two", "!remember a\rb", "!remember a\tb"]
)
def test_remember_multiline_or_control_chars_rejected(host: _Host, text: str) -> None:
    assert "single line" in _say(text)
    assert host.kv.store == {}


def test_remember_over_cap_rejected_with_explicit_reply(host: _Host) -> None:
    _seed(host, "discord", "111", [f"m{i}" for i in range(MAX_MEMORIES)])
    reply = _say("!remember one too many")
    assert "already have 20 memories" in reply
    assert "!forget" in reply
    assert len(_stored(host, "discord", "111")) == MAX_MEMORIES
    assert ("set", _key("discord", "111")) not in host.kv.calls


def test_remember_fills_to_exactly_cap_then_rejects(host: _Host) -> None:
    for i in range(MAX_MEMORIES):
        assert _say(f"!remember fact {i}").endswith(f"({i + 1}/20).")
    assert "already have 20" in _say("!remember overflow")


def test_remember_ids_are_monotonic_after_forget(host: _Host) -> None:
    _say("!remember a")
    _say("!remember b")
    _say("!remember c")
    _say("!forget 2")
    assert _say("!remember d") == "Remembered as #4 (3/20)."
    assert [m["id"] for m in _stored(host, "discord", "111")] == [1, 3, 4]


def test_remember_without_identity_replies_explicitly(host: _Host) -> None:
    reply = _say("!remember hi", actor=None, author_id=None)
    assert "couldn't identify you" in reply
    assert host.kv.store == {}


def test_remember_without_timestamp_replies_explicitly(host: _Host) -> None:
    assert "couldn't timestamp" in _say("!remember hi", occurred_at="")
    assert host.kv.store == {}


# --- !memories (own + other) -------------------------------------------------------------


def test_list_own_numbered(host: _Host) -> None:
    _seed(host, "discord", "111", ["alpha", "beta"])
    assert _say("!memories") == "1. alpha | 2. beta"


def test_list_own_empty(host: _Host) -> None:
    assert _say("!memories") == "You have no memories yet - try !remember <text>."


def test_list_other_via_discord_mention(host: _Host) -> None:
    _seed(host, "discord", "222", ["their fact_text hobby"])
    assert _say("!memories <@222>") == "1. their fact_text hobby"
    assert _say("!memories <@!222>") == "1. their fact_text hobby"


def test_list_other_empty_and_does_not_leak_own(host: _Host) -> None:
    _seed(host, "discord", "111", ["mine"])
    assert _say("!memories <@222>") == "They have no memories yet."


def test_list_other_via_handle_on_non_discord(host: _Host) -> None:
    # Twitch: caller identity is the case-folded actor handle, so `@name` resolves to it.
    _say("!remember streamer fact", platform="twitch", actor="CoolViewer", author_id=None)
    reply = _say("!memories @coolviewer", platform="twitch", actor="other", author_id=None)
    assert reply == "1. streamer fact"
    assert _say("!memories CoolViewer", platform="twitch", actor="other") == "1. streamer fact"


def test_platform_is_part_of_the_key(host: _Host) -> None:
    _seed(host, "discord", "222", ["discord memory"])
    assert (
        _say("!memories coolviewer", platform="twitch", actor="x") == "They have no memories yet."
    )


@pytest.mark.parametrize(
    ("platform", "target"),
    [
        ("discord", "somename"),  # bare name can't map to a Discord id
        ("discord", "@somename"),
        ("discord", "<@notanumber>"),
        ("twitch", "<@123>"),  # mention form is Discord-only
        ("twitch", "bad!name"),
        ("twitch", "@"),
        ("twitch", "a" * 40),  # handle too long
    ],
)
def test_list_unresolvable_target_replies_explicitly(
    host: _Host, platform: str, target: str
) -> None:
    reply = _say(f"!memories {target}", platform=platform)
    assert "couldn't identify that user" in reply
    assert host.kv.calls == []


def test_list_too_many_args_usage(host: _Host) -> None:
    assert _say("!memories <@2> extra") == "Usage: !memories [@user]"


def test_list_self_by_mention_matches_own(host: _Host) -> None:
    _seed(host, "discord", "111", ["mine"])
    assert _say("!memories <@111>") == "1. mine"


def test_list_truncates_with_count(host: _Host) -> None:
    _seed(host, "discord", "111", ["x" * MAX_TEXT_CHARS for _ in range(MAX_MEMORIES)])
    reply = _say("!memories")
    assert len(reply) <= MAX_REPLY_CHARS + len(" (+20 more)")
    assert reply.endswith(" more)")
    shown = reply.count(" | ") + 1
    assert f"(+{MAX_MEMORIES - shown} more)" in reply


def test_list_without_identity_replies_explicitly(host: _Host) -> None:
    assert "couldn't identify you" in _say("!memories", actor=None, author_id=None)


# --- !forget -----------------------------------------------------------------------------


def test_forget_by_id_removes_only_that_entry(host: _Host) -> None:
    _seed(host, "discord", "111", ["a", "b", "c"])
    assert _say("!forget 2") == "Forgot #2."
    assert [m["text"] for m in _stored(host, "discord", "111")] == ["a", "c"]


def test_forget_last_entry_deletes_the_key(host: _Host) -> None:
    _seed(host, "discord", "111", ["only"])
    assert _say("!forget 1") == "Forgot #1."
    assert host.kv.store == {}
    assert ("delete", _key("discord", "111")) in host.kv.calls


def test_forget_unknown_id(host: _Host) -> None:
    _seed(host, "discord", "111", ["a"])
    assert _say("!forget 9") == "You have no memory #9."
    assert len(_stored(host, "discord", "111")) == 1


def test_forget_with_empty_list(host: _Host) -> None:
    assert _say("!forget 1") == "You have no memories to forget."


def test_forget_all_clears_own_only(host: _Host) -> None:
    _seed(host, "discord", "111", ["a", "b"])
    _seed(host, "discord", "222", ["theirs"])
    assert _say("!forget all") == "Cleared all your memories."
    assert _key("discord", "111") not in host.kv.store
    assert _stored(host, "discord", "222")[0]["text"] == "theirs"
    assert _say("!forget ALL") == "Cleared all your memories."


def test_forget_cannot_touch_another_users_list(host: _Host) -> None:
    _seed(host, "discord", "222", ["theirs"])
    assert _say("!forget 1") == "You have no memories to forget."
    assert _stored(host, "discord", "222")[0]["text"] == "theirs"


@pytest.mark.parametrize("arg", ["", "abc", "-1", "1.5", "1 2", "all now", "9" * 12])
def test_forget_bad_args_usage(host: _Host, arg: str) -> None:
    assert _say(f"!forget {arg}".strip()) == "Usage: !forget <id> | !forget all"
    assert host.kv.calls == []


def test_forget_without_identity_replies_explicitly(host: _Host) -> None:
    assert "couldn't identify you" in _say("!forget 1", actor=None, author_id=None)


# --- corrupt stored state (handled explicitly, never silent) -----------------------------


@pytest.mark.parametrize(
    "blob",
    [
        b"not json at all",
        b"\xff\xfe\x00bad-utf8",
        b'{"id": 1}',  # not a list
        b"[1, 2]",  # entries not objects
        b'[{"id": "1", "text": "a", "ts": "t"}]',  # id wrong type
        b'[{"id": true, "text": "a", "ts": "t"}]',  # bool is not an id
        b'[{"id": 0, "text": "a", "ts": "t"}]',  # id must be positive
        b'[{"id": 1, "text": 5, "ts": "t"}]',  # text wrong type
        b'[{"id": 1, "text": "a"}]',  # ts missing
        b'[{"id": 1, "text": "a", "ts": "t"}, {"id": 1, "text": "b", "ts": "t"}]',  # dup ids
    ],
)
@pytest.mark.parametrize("command", ["!memories", "!remember hi", "!forget 1"])
def test_corrupt_blob_logs_error_replies_and_is_not_overwritten(
    host: _Host, blob: bytes, command: str
) -> None:
    host.kv.store[_key("discord", "111")] = blob
    reply = _say(command)
    assert "couldn't be read" in reply
    assert "!forget all" in reply
    assert host.kv.store[_key("discord", "111")] == blob  # untouched, never silently reset
    assert any(lvl == 0 and msg == "memories.state_corrupt" for lvl, msg, _ in host.log_calls)


def test_corrupt_blob_over_cap_is_corrupt(host: _Host) -> None:
    host.kv.store[_key("discord", "111")] = json.dumps(
        [{"id": i, "text": "x", "ts": "t"} for i in range(1, MAX_MEMORIES + 2)]
    ).encode()
    assert "couldn't be read" in _say("!memories")


def test_forget_all_recovers_from_corrupt_blob(host: _Host) -> None:
    host.kv.store[_key("discord", "111")] = b"garbage"
    assert _say("!forget all") == "Cleared all your memories."
    assert host.kv.store == {}
    assert _say("!remember fresh start").startswith("Remembered as #1")


# --- kv failure fails loud ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "command"),
    [
        ("get", "!remember hi"),
        ("get", "!memories"),
        ("get", "!memories <@222>"),
        ("get", "!forget 1"),
        ("set", "!remember hi"),
        ("delete", "!forget all"),
    ],
)
def test_kv_failure_logs_error_and_replies_explicitly(host: _Host, op: str, command: str) -> None:
    _seed(host, "discord", "111", ["a"])
    host.kv.fail_ops.add(op)
    reply = _say(command)
    assert reply == "Something went wrong with memory storage - please try again."
    errors = [(msg, fields) for lvl, msg, fields in host.log_calls if lvl == 0]
    assert [msg for msg, _ in errors] == ["memories.kv_failure"]
    assert json.loads(errors[0][1])["op"] == op


def test_kv_delete_failure_on_forget_last_entry(host: _Host) -> None:
    _seed(host, "discord", "111", ["only"])
    host.kv.fail_ops.add("delete")
    assert "went wrong" in _say("!forget 1")


# --- PII-free logs -----------------------------------------------------------------------


def test_logs_never_contain_memory_text_actor_or_target(host: _Host) -> None:
    fact_text = "my-very-fact_text-fact"
    _say(f"!remember {fact_text}", actor="private-handle")
    _say("!memories <@987654>", actor="private-handle")
    _say("!memories", actor="private-handle")
    _say("!forget 1", actor="private-handle")
    _say("!remember " + "z" * 500, actor="private-handle")
    _say("!memories bad-target!!", platform="twitch", actor="private-handle")
    host.kv.fail_ops.add("get")
    _say("!memories", actor="private-handle")
    logged = _logged(host)
    assert logged
    for needle in (fact_text, "private-handle", "987654", "bad-target", "111", "zzzz"):
        assert needle not in logged


# --- dispatch ----------------------------------------------------------------------------


def test_dispatch_relays_reply_text(host: _Host) -> None:
    result = _run(dispatch(_envelope({"channel_id": "c1", "text": "hello"}), {}, http_client=None))
    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("twitch", "relayed")
    assert result.sub_type is None
    assert result.http_status is None
    assert host.relay_calls == [("twitch", {"channel": "c1", "text": "hello"})]


def test_dispatch_relays_to_the_events_own_platform(host: _Host) -> None:
    _run(
        dispatch(
            _envelope({"channel_id": "c1", "text": "hi"}, platform="discord"), {}, http_client=None
        )
    )
    assert host.relay_calls[0][0] == "discord"


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hello"},
        {"channel_id": "", "text": "hello"},
        {"channel_id": "c1"},
        {"channel_id": "c1", "text": ""},
        {"channel_id": "c1", "text": 5},
    ],
)
def test_dispatch_rejects_incomplete_payload(host: _Host, payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="memories reply requires"):
        _run(dispatch(_envelope(payload), {}, http_client=None))
    assert host.relay_calls == []


def test_end_to_end_transform_output_is_dispatchable(host: _Host) -> None:
    out = _run(transform(_event("!remember hi")))
    assert out is not None
    result = _run(dispatch(_envelope(out.payload, platform=out.platform), {}, http_client=None))
    assert result.detail == "relayed"
    assert host.relay_calls[0][1]["text"].startswith("Remembered as #1")


# --- kv key hygiene ----------------------------------------------------------------------


def test_every_kv_call_uses_a_host_valid_key(host: _Host) -> None:
    for cmd in ("!remember x", "!memories", "!memories <@222>", "!forget 1", "!forget all"):
        _say(cmd)
    assert host.kv.calls
    for _, key in host.kv.calls:
        assert key.startswith("memories.")
        assert len(key) <= 256
