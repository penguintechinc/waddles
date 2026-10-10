"""Host-native tests for the `lastseen` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (fake `kv`/`relay`/`flags`/`log`/`clock`/`http`).
The `kv` fake validates keys exactly like the real host (gh-631), so a `:` key fails loudly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import app
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


_ALLOWED_KV_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


def _validate_guest_key(key: str) -> None:
    """Reject exactly what the real `bundle_host_kv` capability rejects."""
    if not key or len(key) > 256:
        raise _HostKvError(f"too-large: key length {len(key)}")
    if any(ch not in _ALLOWED_KV_KEY_CHARS for ch in key):
        raise _HostKvError(f"backend: invalid key {key!r}")


class _FakeKv:
    """In-memory `kv` host stand-in -- validates keys like the real host, scripted failures."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.fail_ops: set[str] = set()

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_ops:
            raise RuntimeError(f"scripted {op} failure")

    def get(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("get", (key,)))
        self._maybe_fail("get")
        return self.store.get(key)

    def set(self, key: str, value, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("set", (key, bytes(value), ttl_seconds)))
        self._maybe_fail("set")
        self.store[key] = bytes(value)

    def delete(self, key: str):
        _validate_guest_key(key)
        self.calls.append(("delete", (key,)))
        self._maybe_fail("delete")
        self.store.pop(key, None)

    def increment(self, key: str, delta: int, ttl_seconds: int):
        _validate_guest_key(key)
        self.calls.append(("increment", (key, delta, ttl_seconds)))
        self._maybe_fail("increment")
        new_value = int(self.store.get(key, b"0")) + delta
        self.store[key] = str(new_value).encode()
        return new_value


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch):
    """Fake WIT host: flag ON by default, real in-memory `kv`, recording `relay`/`log`."""
    fake_kv = _FakeKv()
    relay_calls: list[tuple[str, dict]] = []
    log_calls: list[tuple[int, str, str]] = []
    flag_state = {"enabled": True}

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_state["enabled"])
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, json.loads(msg)))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_rfc3339=lambda: "2026-10-09T12:00:00.000Z",
        now_millis=lambda: 1_790_000_000_000,
        monotonic_nanos=lambda: 1,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=fake_kv, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)
    return types.SimpleNamespace(
        kv=fake_kv,
        relay_calls=relay_calls,
        log_calls=log_calls,
        flag_state=flag_state,
        wit=fake_wit_world,
    )


def _event(
    text: str,
    *,
    platform: str = "twitch",
    is_mod=False,
    is_broadcaster=False,
    channel_id="12345",
    extra: dict | None = None,
    occurred_at: str = "2026-10-05T00:00:00.000Z",
):
    payload: dict = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    if extra:
        payload.update(extra)
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload=payload,
        occurred_at=occurred_at,
    )


def _no_role_event(text: str, *, platform: str = "discord", channel_id="guild-1"):
    """A Discord-shaped event -- no `is_mod`/`is_broadcaster` keys (today's real gap)."""
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _reply(result: PlatformEvent | None) -> str:
    assert result is not None
    return result.payload["text"]


def _logs_text(fake_host) -> str:
    return " ".join(f"{msg} {fields}" for _lvl, msg, fields in fake_host.log_calls)


def _make_env(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.test",
        stage="action",
        event=event,
        ts="2026-10-09T00:00:00.000Z",
    )


def test_dispatch_relays_reply(fake_host) -> None:
    env = _make_env(_event("x", channel_id="c1"))
    env.event.payload["text"] = "hello"
    result = _run(dispatch(env, {}, http_client=None))
    assert result.detail == "relayed"
    assert fake_host.relay_calls == [("twitch", {"channel": "c1", "text": "hello"})]


def test_dispatch_requires_channel_and_text(fake_host) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_make_env(_event("x", channel_id="")), {}, http_client=None))
    env = _make_env(_event("", channel_id="c1"))
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(env, {}, http_client=None))


@pytest.mark.parametrize("text", ["hello", "", "   "])
def test_non_bang_messages_are_ignored(text: str, fake_host) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_is_ignored(fake_host) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None
    assert fake_host.kv.calls == []


# ---------------------------------------------------------------------------
# bundle-specific tests
# ---------------------------------------------------------------------------

import uuid

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://waddles.penguintech.io/identity")


def _uid(platform: str, pid: str) -> str:
    return str(uuid.uuid5(_NS, f"{platform}:{pid}"))


def _tw(text: str, uid: str = "555", ts: str = "2026-10-05T00:00:00.000Z", **kw):
    return _event(text, extra={"user_id": uid, "author_id": uid}, occurred_at=ts, **kw)


def _dc(text: str, uid: str = "777", ts: str = "2026-10-05T00:00:00.000Z"):
    return _event(text, platform="discord", extra={"author_id": uid}, occurred_at=ts)


def test_disabled_flag_does_nothing(fake_host) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_tw("!lastseen"))) is None
    assert fake_host.kv.calls == []


def test_chat_message_stamps_activity_silently(fake_host) -> None:
    assert _run(transform(_tw("just chatting", ts="2026-10-05T01:02:03.000Z"))) is None
    key = f"lastseen.user.{_uid('twitch', '555')}"
    assert fake_host.kv.store[key] == b"2026-10-05T01:02:03.000Z"
    assert fake_host.kv.calls[-1][1][2] == app.TTL_SECONDS


def test_self_lookup_reports_previous_stamp_then_stamps(fake_host) -> None:
    _run(transform(_tw("hi", ts="2026-10-01T00:00:00.000Z")))
    reply = _reply(_run(transform(_tw("!lastseen", ts="2026-10-09T00:00:00.000Z"))))
    assert reply == "You were last seen 2026-10-01T00:00:00.000Z."
    # the command itself was stamped afterwards
    reply2 = _reply(_run(transform(_tw("!LASTSEEN", ts="2026-10-09T00:05:00.000Z"))))
    assert "2026-10-09T00:00:00.000Z" in reply2


def test_self_lookup_without_record(fake_host) -> None:
    assert "No activity recorded for you" in _reply(_run(transform(_tw("!lastseen"))))


def test_self_lookup_without_identity_is_explicit(fake_host) -> None:
    reply = _reply(_run(transform(_event("!lastseen"))))
    assert "couldn't identify you" in reply
    assert fake_host.kv.calls == []  # nothing stamped or read for an unidentifiable user


def test_unidentifiable_chatter_is_not_stamped(fake_host) -> None:
    assert _run(transform(_event("hello"))) is None
    assert fake_host.kv.calls == []


@pytest.mark.parametrize("bad_id", ["", "has space", "a" * 80, "bad:colon", 123, None])
def test_invalid_platform_ids_are_not_used(bad_id, fake_host) -> None:
    event = _event("hello", extra={"user_id": bad_id})
    assert _run(transform(event)) is None
    assert fake_host.kv.calls == []


def test_author_id_fallback_when_no_user_id(fake_host) -> None:
    _run(transform(_dc("hi", uid="42", ts="2026-10-02T00:00:00.000Z")))
    assert f"lastseen.user.{_uid('discord', '42')}" in fake_host.kv.store


def test_discord_mention_lookup(fake_host) -> None:
    _run(transform(_dc("hi", uid="42", ts="2026-10-02T00:00:00.000Z")))
    for mention in ("<@42>", "<@!42>"):
        reply = _reply(_run(transform(_dc(f"!lastseen {mention}", uid="9"))))
        assert reply == f"User {_uid('discord', '42')[:8]} was last seen 2026-10-02T00:00:00.000Z."


def test_mention_with_no_record(fake_host) -> None:
    reply = _reply(_run(transform(_dc("!lastseen <@123>"))))
    assert "No activity recorded for user" in reply


def test_literal_uuid_lookup(fake_host) -> None:
    _run(transform(_tw("hi", uid="55", ts="2026-10-03T00:00:00.000Z")))
    target = _uid("twitch", "55")
    reply = _reply(_run(transform(_tw(f"!lastseen {target}", uid="56"))))
    assert "2026-10-03T00:00:00.000Z" in reply


@pytest.mark.parametrize("target", ["@somename", "somename", "@Some_Name"])
def test_free_text_names_are_refused_not_guessed(target: str, fake_host) -> None:
    reply = _reply(_run(transform(_tw(f"!lastseen {target}"))))
    assert "can't look users up by name" in reply and "#429" in reply
    # never read a hashed-name key
    assert not any(c[0] == "get" for c in fake_host.kv.calls)


def test_discord_mention_syntax_is_ignored_on_twitch(fake_host) -> None:
    reply = _reply(_run(transform(_tw("!lastseen <@42>"))))
    assert "can't look users up by name" in reply


def test_too_many_args_gives_usage(fake_host) -> None:
    assert "Usage" in _reply(_run(transform(_tw("!lastseen a b"))))


def test_clock_fallback_when_event_has_no_timestamp(fake_host) -> None:
    _run(transform(_tw("hi", ts="")))
    key = f"lastseen.user.{_uid('twitch', '555')}"
    assert fake_host.kv.store[key] == b"2026-10-09T12:00:00.000Z"


def test_corrupt_stamp_is_loud(fake_host) -> None:
    fake_host.kv.store[f"lastseen.user.{_uid('twitch', '555')}"] = b"\xff\xfe"
    reply = _reply(_run(transform(_tw("!lastseen"))))
    assert "unavailable" in reply
    assert "lastseen.kv_failure" in _logs_text(fake_host)


def test_kv_read_failure_answered_and_logged(fake_host) -> None:
    fake_host.kv.fail_ops.add("get")
    reply = _reply(_run(transform(_tw("!lastseen"))))
    assert "unavailable" in reply
    logs = _logs_text(fake_host)
    assert "lastseen.kv_failure" in logs and "RuntimeError" in logs


def test_kv_write_failure_on_command_still_replies(fake_host) -> None:
    fake_host.kv.fail_ops.add("set")
    reply = _reply(_run(transform(_tw("!lastseen"))))
    assert "No activity recorded" in reply
    assert "lastseen.kv_failure" in _logs_text(fake_host)


def test_kv_write_failure_on_passive_message_is_logged_not_silent(fake_host) -> None:
    fake_host.kv.fail_ops.add("set")
    assert _run(transform(_tw("hello"))) is None
    logs = _logs_text(fake_host)
    assert "lastseen.kv_failure" in logs and '"op": "record"' in logs


def test_logs_are_pii_free(fake_host) -> None:
    _run(transform(_tw("!lastseen @SecretName", uid="9999")))
    _run(transform(_dc("!lastseen <@31337>", uid="8888")))
    _run(transform(_event("!lastseen")))
    fake_host.kv.fail_ops.add("get")
    _run(transform(_tw("!lastseen", uid="9999")))
    logs = _logs_text(fake_host)
    for needle in ("SecretName", "secretname", "9999", "8888", "31337", "viewer-1"):
        assert needle not in logs


def test_kv_key_satisfies_host_charset() -> None:
    sample = f"{app.KEY_PREFIX}{_uid('twitch', '1')}"
    assert set(sample) <= _ALLOWED_KV_KEY_CHARS
