"""Host-native tests for the `dailycounter` bundle's `transform`/`dispatch` logic.

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

D1 = "2026-10-05T10:00:00.000Z"
D2 = "2026-10-06T10:00:00.000Z"


def _m(text: str, ts: str = D1, **kw):
    return _event(text, is_mod=True, occurred_at=ts, **kw)


def _say(event) -> str:
    return _reply(_run(transform(event)))


def _make(fake_host, name: str = "deaths") -> None:
    assert "Created" in _say(_m(f"!dailycounter add {name}"))


def test_disabled_flag_suppresses_everything(fake_host) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_m("!dailycounter add deaths"))) is None
    assert fake_host.kv.calls == []


def test_unregistered_bang_costs_one_registry_get(fake_host) -> None:
    assert _run(transform(_event("!nothing"))) is None
    assert fake_host.kv.calls == [("get", (app.REGISTRY_KEY,))]


def test_crud_lifecycle(fake_host) -> None:
    assert "No daily counters" in _say(_event("!dailycounter"))
    assert "No daily counters" in _say(_event("!dailycounter list"))
    _make(fake_host)
    assert "already exists" in _say(_m("!dailycounter add !Deaths"))
    assert "Daily counters: deaths" in _say(_event("!dailycounter list"))

    assert _say(_event("!deaths", occurred_at=D1)) == "deaths today: 0"
    assert _say(_m("!deaths add")) == "deaths today: 1"
    assert _say(_m("!deaths add 4")) == "deaths today: 5"
    assert _say(_m("!deaths sub 2")) == "deaths today: 3"
    assert _say(_m("!deaths sub")) == "deaths today: 2"
    assert _say(_m("!deaths set 10")) == "deaths today: 10"
    assert _say(_event("!DEATHS", occurred_at=D1)) == "deaths today: 10"
    assert _say(_m("!deaths reset")) == "deaths today: 0"
    assert _say(_event("!deaths", occurred_at=D1)) == "deaths today: 0"

    assert "Removed" in _say(_m("!dailycounter remove deaths"))
    assert _run(transform(_event("!deaths"))) is None
    assert "doesn't exist" in _say(_m("!dailycounter remove deaths"))


def test_values_roll_over_per_utc_day(fake_host) -> None:
    _make(fake_host)
    _say(_m("!deaths add 3", ts=D1))
    assert _say(_event("!deaths", occurred_at=D2)) == "deaths today: 0"
    _say(_m("!deaths add", ts=D2))
    assert _say(_event("!deaths", occurred_at=D1)) == "deaths today: 3"
    assert _say(_event("!deaths", occurred_at=D2)) == "deaths today: 1"
    assert "dailycounter.value.deaths.2026-10-05" in fake_host.kv.store
    assert "dailycounter.value.deaths.2026-10-06" in fake_host.kv.store


def test_day_values_carry_ttl(fake_host) -> None:
    _make(fake_host)
    _say(_m("!deaths add"))
    _say(_m("!deaths set 4"))
    _say(_m("!deaths reset"))
    ttls = []
    for op, args in fake_host.kv.calls:
        if op == "increment":
            ttls.append(args[2])
        elif op == "set" and args[0].startswith("dailycounter.value."):
            ttls.append(args[2])
    assert ttls == [app.TTL_SECONDS] * 3


def test_missing_or_bad_timestamp_falls_back_to_host_clock(fake_host) -> None:
    _make(fake_host)
    for ts in ("", "garbage"):
        _say(_m("!deaths add", ts=ts))
    assert fake_host.kv.store["dailycounter.value.deaths.2026-10-09"] == b"2"


@pytest.mark.parametrize("text", ["!dailycounter add deaths", "!dailycounter remove deaths"])
def test_management_fails_closed_without_role_info(text: str, fake_host) -> None:
    assert "broadcaster or a moderator" in _say(_no_role_event(text))
    assert app.REGISTRY_KEY not in fake_host.kv.store


@pytest.mark.parametrize("op", ["add", "sub", "set 3", "reset"])
def test_counter_mutations_fail_closed_and_deny_non_mods(op: str, fake_host) -> None:
    _make(fake_host)
    assert "broadcaster or a moderator" in _say(_no_role_event(f"!deaths {op}"))
    assert "broadcaster or a moderator" in _say(_event(f"!deaths {op}"))
    assert not any(c[0] == "increment" for c in fake_host.kv.calls)
    assert not any(
        c[0] == "set" and c[1][0].startswith("dailycounter.value.") for c in fake_host.kv.calls
    )


def test_broadcaster_is_allowed(fake_host) -> None:
    event = _event("!dailycounter add deaths", is_mod=False, is_broadcaster=True)
    assert "Created" in _say(event)


@pytest.mark.parametrize(
    "text,needle",
    [
        ("!dailycounter add", "Usage"),
        ("!dailycounter remove", "Usage"),
        ("!dailycounter nope", "Unknown !dailycounter subcommand"),
        ("!dailycounter add dailycounter", "reserved"),
        ("!dailycounter add list", "reserved"),
        ("!dailycounter add BAD$", "may only contain"),
        ("!dailycounter add " + "a" * 40, "32 characters"),
        ("!dailycounter add !", "name is required"),
    ],
)
def test_management_validation(text: str, needle: str, fake_host) -> None:
    assert needle in _say(_m(text))
    assert app.REGISTRY_KEY not in fake_host.kv.store


def test_max_counters_enforced(fake_host) -> None:
    fake_host.kv.store[app.REGISTRY_KEY] = json.dumps(
        [f"c{i}" for i in range(app.MAX_COUNTERS)]
    ).encode()
    assert "maximum" in _say(_m("!dailycounter add extra"))


@pytest.mark.parametrize(
    "text,needle",
    [
        ("!deaths add abc", "whole number"),
        ("!deaths add " + str(2**63), "out of range"),
        ("!deaths set", "number is required"),
        ("!deaths set x", "whole number"),
        ("!deaths bogus", "Unknown operation"),
    ],
)
def test_counter_arg_validation(text: str, needle: str, fake_host) -> None:
    _make(fake_host)
    assert needle in _say(_m(text))


def test_remove_deletes_todays_key(fake_host) -> None:
    _make(fake_host)
    _say(_m("!deaths add"))
    _say(_m("!dailycounter remove deaths"))
    assert "dailycounter.value.deaths.2026-10-05" not in fake_host.kv.store


@pytest.mark.parametrize("stored", [b"\xff\xfe", b"nope", b"{}", b"[1]"])
def test_corrupt_registry_is_loud_and_untouched(stored: bytes, fake_host) -> None:
    fake_host.kv.store[app.REGISTRY_KEY] = stored
    assert "unavailable" in _say(_m("!dailycounter add deaths"))
    assert fake_host.kv.store[app.REGISTRY_KEY] == stored
    assert "dailycounter.kv_failure" in _logs_text(fake_host)


@pytest.mark.parametrize("stored", [b"\xff", b"abc"])
def test_corrupt_value_is_loud(stored: bytes, fake_host) -> None:
    _make(fake_host)
    fake_host.kv.store["dailycounter.value.deaths.2026-10-05"] = stored
    assert "unavailable" in _say(_event("!deaths", occurred_at=D1))
    assert "dailycounter.kv_failure" in _logs_text(fake_host)


@pytest.mark.parametrize("op", ["get", "set", "delete", "increment"])
def test_kv_failures_logged_and_answered(op: str, fake_host) -> None:
    _make(fake_host)
    fake_host.kv.fail_ops.add(op)
    texts = {
        "get": "!deaths",
        "set": "!deaths set 3",
        "delete": "!dailycounter remove deaths",
        "increment": "!deaths add",
    }
    assert "unavailable" in _say(_m(texts[op]))
    logs = _logs_text(fake_host)
    assert "dailycounter.kv_failure" in logs and "RuntimeError" in logs


def test_logs_are_pii_free(fake_host) -> None:
    _make(fake_host, "secretcounter")
    _say(_m("!secretcounter add 5 trailingpii"))
    _say(_m("!secretcounter bogus piiarg"))
    _say(_no_role_event("!dailycounter add x"))
    logs = _logs_text(fake_host)
    for needle in ("secretcounter", "trailingpii", "piiarg", "viewer-1"):
        assert needle not in logs


def test_kv_keys_satisfy_host_charset(fake_host) -> None:
    _make(fake_host)
    _say(_m("!deaths add"))
    assert fake_host.kv.store  # fake validates every key exactly like the real host
    assert ":" not in app.REGISTRY_KEY and ":" not in app.VALUE_KEY_PREFIX
