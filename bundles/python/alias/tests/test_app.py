"""Host-native tests for the `alias` bundle's `transform`/`dispatch` logic.

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


def _mod(text: str, **kw):
    return _event(text, is_mod=True, **kw)


def _registry(fake_host) -> dict:
    return json.loads(fake_host.kv.store[app.REGISTRY_KEY])


def test_disabled_flag_suppresses_everything(fake_host) -> None:
    fake_host.flag_state["enabled"] = False
    assert _run(transform(_mod("!alias set hi !hello"))) is None
    assert fake_host.kv.calls == []


def test_bang_message_without_alias_costs_one_kv_get(fake_host) -> None:
    assert _run(transform(_event("!whatever"))) is None
    assert fake_host.kv.calls == [("get", (app.REGISTRY_KEY,))]


def test_crud_lifecycle(fake_host) -> None:
    assert "No aliases" in _reply(_run(transform(_event("!alias"))))
    assert "No aliases" in _reply(_run(transform(_event("!alias list"))))

    assert "Created alias !hi -> !hello" in _reply(_run(transform(_mod("!alias set !HI hello"))))
    assert _registry(fake_host) == {"hi": "!hello"}
    assert "Updated alias" in _reply(_run(transform(_mod("!alias set hi !hello world"))))
    assert _registry(fake_host) == {"hi": "!hello world"}

    listing = _reply(_run(transform(_event("!alias list"))))
    assert "!hi -> !hello world" in listing
    assert "!hi" in _reply(_run(transform(_event("!alias"))))

    # invocation resolves, appending the caller's args
    assert _reply(_run(transform(_event("!hi there friend")))) == "!hello world there friend"
    assert _reply(_run(transform(_event("!HI")))) == "!hello world"

    assert "Removed alias" in _reply(_run(transform(_mod("!alias remove hi"))))
    assert _registry(fake_host) == {}
    assert _run(transform(_event("!hi"))) is None
    assert "doesn't exist" in _reply(_run(transform(_mod("!alias remove hi"))))


@pytest.mark.parametrize("text", ["!alias set hi !hello", "!alias remove hi"])
def test_mutations_fail_closed_without_role_info(text: str, fake_host) -> None:
    result = _run(transform(_no_role_event(text)))
    assert "broadcaster or a moderator" in _reply(result)
    assert not any(c[0] in ("set", "delete") for c in fake_host.kv.calls)


def test_non_mod_denied_and_broadcaster_allowed(fake_host) -> None:
    assert "Only the broadcaster" in _reply(_run(transform(_event("!alias set hi !hello"))))
    event = _event("!alias set hi !hello", is_mod=False, is_broadcaster=True)
    assert "Created" in _reply(_run(transform(event)))


@pytest.mark.parametrize(
    "text,needle",
    [
        ("!alias set", "Usage"),
        ("!alias set hi", "Usage"),
        ("!alias remove", "Usage"),
        ("!alias bogus", "Unknown !alias subcommand"),
        ("!alias set alias !hello", "reserved"),
        ("!alias set BAD$NAME !hello", "may only contain"),
        ("!alias set " + "a" * 40 + " !hello", "32 characters"),
        ("!alias set hi !alias", "no alias chains"),
        ("!alias set hi !hi", "point at itself"),
        ("!alias set hi !", "must start with a command"),
        ("!alias set hi !bad$cmd", "must start with a command"),
        ("!alias set hi !" + "a" * 250, "200 characters"),
        ("!alias set hi !a\x07b", "control characters"),
    ],
)
def test_validation_errors(text: str, needle: str, fake_host) -> None:
    assert needle in _reply(_run(transform(_mod(text))))
    assert app.REGISTRY_KEY not in fake_host.kv.store


def test_empty_name_after_normalization(fake_host) -> None:
    assert "alias name is required" in _reply(_run(transform(_mod("!alias set ! !hello"))))


def test_no_chaining_through_existing_alias(fake_host) -> None:
    _run(transform(_mod("!alias set hi !hello")))
    assert "no alias chains" in _reply(_run(transform(_mod("!alias set yo !hi"))))


def test_max_aliases_enforced_but_update_still_allowed(fake_host) -> None:
    fake_host.kv.store[app.REGISTRY_KEY] = json.dumps(
        {f"a{i}": "!x" for i in range(app.MAX_ALIASES)}
    ).encode()
    assert "maximum" in _reply(_run(transform(_mod("!alias set newone !y"))))
    assert "Updated" in _reply(_run(transform(_mod("!alias set a0 !z"))))


def test_resolved_text_is_length_capped(fake_host) -> None:
    _run(transform(_mod("!alias set hi !hello")))
    result = _run(transform(_event("!hi " + "x" * 2000)))
    assert len(_reply(result)) == app.MAX_RESOLVED_LEN


@pytest.mark.parametrize(
    "stored",
    [b"\xff\xfe", b"not json", b"[1, 2]", b'{"a": 1}'],
)
def test_corrupt_registry_is_loud_not_reset(stored: bytes, fake_host) -> None:
    fake_host.kv.store[app.REGISTRY_KEY] = stored
    reply = _reply(_run(transform(_mod("!alias set hi !hello"))))
    assert "unavailable" in reply
    assert fake_host.kv.store[app.REGISTRY_KEY] == stored  # never silently overwritten
    assert "alias.kv_failure" in _logs_text(fake_host)


@pytest.mark.parametrize("op", ["get", "set"])
def test_kv_failure_logged_and_answered_explicitly(op: str, fake_host) -> None:
    fake_host.kv.fail_ops.add(op)
    reply = _reply(_run(transform(_mod("!alias set hi !hello"))))
    assert "unavailable" in reply
    logs = _logs_text(fake_host)
    assert "alias.kv_failure" in logs
    assert "RuntimeError" in logs  # exception TYPE only


def test_logs_are_pii_free(fake_host) -> None:
    _run(transform(_mod("!alias set secretname !secrettarget")))
    _run(transform(_event("!secretname topsecretarg")))
    _run(transform(_no_role_event("!alias set x !y")))
    logs = _logs_text(fake_host)
    for needle in ("secretname", "secrettarget", "topsecretarg", "viewer-1"):
        assert needle not in logs


def test_kv_key_satisfies_host_charset() -> None:
    assert set(app.REGISTRY_KEY) <= _ALLOWED_KV_KEY_CHARS
    assert ":" not in app.REGISTRY_KEY
