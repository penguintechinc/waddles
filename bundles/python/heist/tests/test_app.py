"""Host-native tests for the `heist` bundle (fake `wit_world`, in-memory kv; see `slots`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import app
from app import (
    DEFAULT_WINDOW_SECONDS,
    MAX_CREW,
    MAX_SUCCESS_PERCENT,
    STARTING_BALANCE,
    Heist,
    _amount_bucket,
    _balance_key,
    _caller_role_signal,
    _resolve_command,
    _success_percent,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _pseud(actor: str) -> str:
    return hashlib.sha256(actor.encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    return _scoped_key(community, key)


class _FakeHost:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[Any, str, str]] = []
        self.now_ms = 1_700_000_000_000
        self.fail_op: str | None = None
        self.drop_set_after_write = False

    def texts(self) -> list[str]:
        return [json.loads(m)["text"] for _, m in self.relay_calls]


def _install(mp: pytest.MonkeyPatch, host: _FakeHost, *, flag: bool = True) -> None:
    def _maybe_fail(op: str) -> None:
        if host.fail_op == op:
            raise RuntimeError("boom")

    def kv_get(key: str) -> bytes | None:
        _maybe_fail("get")
        return host.store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        _maybe_fail("set")
        if host.drop_set_after_write and key.endswith("heist.state"):
            return  # simulate a concurrent writer clobbering the roster
        host.store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        _maybe_fail("delete")
        host.store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        _maybe_fail("increment")
        new = int(host.store.get(key, b"0").decode()) + delta
        host.store[key] = str(new).encode()
        return new

    ns = types.SimpleNamespace
    mod = types.ModuleType("wit_world")
    mod.imports = ns(  # type: ignore[attr-defined]
        flags=ns(enabled=lambda key, default_value: flag),
        kv=ns(get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment),
        relay=ns(push=lambda provider, msg: host.relay_calls.append((provider, msg))),
        log=ns(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fj: host.log_calls.append((lvl, msg, fj)),
        ),
        clock=ns(
            now_millis=lambda: host.now_ms,
            now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
            monotonic_nanos=lambda: 0,
        ),
    )
    mp.setitem(sys.modules, "wit_world", mod)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    h = _FakeHost()
    _install(monkeypatch, h)
    return h


def _event(text: str, **extra: Any) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": "12345", **extra}
    return PlatformEvent("twitch", "chat.message", "viewer-1", payload, "2026-10-05T00:00:00.000Z")


def _env(
    command: str,
    *,
    arg: str | None = None,
    actor: str = "viewer-1",
    community: str | None = "comm-1",
    **extra: Any,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345", **extra}
    if arg is not None:
        payload["arg"] = arg
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.heist",
        stage="action",
        event=PlatformEvent("twitch", "chat.message", actor, payload, "2026-10-05T00:00:00.000Z"),
        ts="2026-10-05T00:00:00.000Z",
    )


def _say(host: _FakeHost, command: str, **kw: Any) -> str:
    _run(dispatch(_env(command, **kw), {}, http_client=None))
    return host.texts()[-1]


def _bal(host: _FakeHost, actor: str) -> int:
    return int(host.store[_scoped(_balance_key(_pseud(actor)))].decode())


def _state(host: _FakeHost) -> Heist:
    return Heist.decode(host.store[_scoped("heist.state")])


@pytest.mark.parametrize(
    ("text", "command", "arg"),
    [
        ("!heist 50", "join", "50"),
        ("!HEIST 5", "join", "5"),
        ("!heist", "status", None),
        ("!heist set window 90", "config_set_window", "window 90"),
        ("!heist all", "usage", None),
        ("!heist -3", "usage", None),
        ("!heist enable ai", "usage", None),
    ],
)
def test_transform_routes(host: _FakeHost, text: str, command: str, arg: str | None) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == command
    assert out.payload.get("arg") == arg


@pytest.mark.parametrize("text", ["!heists 5", "heist 5", "", "hi"])
def test_transform_ignores_other_text(host: _FakeHost, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_transform_non_string_and_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeHost(), flag=False)
    assert _run(transform(_event("!heist 5"))) is None
    ev = PlatformEvent("twitch", "chat.message", "v", {"text": None}, "")
    assert _run(transform(ev)) is None


def test_transform_badges(host: _FakeHost) -> None:
    out = _run(transform(_event("!heist", is_mod=True, is_broadcaster=False)))
    assert out is not None
    assert out.payload["is_mod"] is True
    assert out.payload["is_broadcaster"] is False
    bare = _run(transform(_event("!heist")))
    assert bare is not None
    assert "is_mod" not in bare.payload


def test_helpers() -> None:
    assert _resolve_command(None) == "usage"
    assert _resolve_command(parse_command("!heist", CommandSpec("heist"))) == "status"
    assert _resolve_command(ParsedCommand("heist", None, "sub", None)) == "usage"
    assert _caller_role_signal({}) is None
    assert _caller_role_signal({"is_mod": True}) is True
    assert _caller_role_signal({"is_mod": False}) is False
    assert _success_percent(1) == 35
    assert _success_percent(3) == 45
    assert _success_percent(500) == MAX_SUCCESS_PERCENT
    assert [_amount_bucket(n) for n in (5, 50, 500, 5000)] == ["le10", "le100", "le1000", "gt1000"]
    assert app._roll(100) and not app._roll(0)


def test_open_heist_debits_and_persists(host: _FakeHost) -> None:
    text = _say(host, "join", arg="40")
    assert "started a heist" in text
    assert _bal(host, "viewer-1") == STARTING_BALANCE - 40
    st = _state(host)
    assert st.crew == {_pseud("viewer-1"): 40}
    assert st.window_s == DEFAULT_WINDOW_SECONDS


def test_second_player_joins_and_duplicate_rejected(host: _FakeHost) -> None:
    _say(host, "join", arg="10")
    assert "joined the heist" in _say(host, "join", arg="20", actor="bob")
    assert len(_state(host).crew) == 2
    assert "already in" in _say(host, "join", arg="5")
    assert _bal(host, "viewer-1") == STARTING_BALANCE - 10


def test_join_validation(host: _FakeHost) -> None:
    assert "at least 1" in _say(host, "join", arg="0")
    assert "Usage" in _say(host, "join", arg="x")
    assert "only have" in _say(host, "join", arg=str(STARTING_BALANCE + 1))
    assert "heist.state" not in " ".join(host.store)


def test_crew_cap(host: _FakeHost) -> None:
    _say(host, "join", arg="1")
    st = _state(host)
    for i in range(MAX_CREW):
        st.crew[f"p{i}"] = 1
    host.store[_scoped("heist.state")] = st.encode()
    assert "full" in _say(host, "join", arg="1", actor="late")


def test_status_open_and_none(host: _FakeHost) -> None:
    assert "No heist" in _say(host, "status")
    _say(host, "join", arg="25")
    text = _say(host, "status")
    assert "1 crew" in text
    assert "pot 25" in text


@pytest.mark.parametrize("via", ["status", "join"])
def test_success_pays_1_5x_exactly_once(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    monkeypatch.setattr(app, "_roll", lambda pct: True)
    _say(host, "join", arg="40")
    _say(host, "join", arg="20", actor="bob")
    host.now_ms += (DEFAULT_WINDOW_SECONDS + 1) * 1000
    text = _say(host, via, arg="10") if via == "join" else _say(host, via)
    assert "SUCCEEDED" in text
    assert _bal(host, "viewer-1") == STARTING_BALANCE - 40 + 60
    assert _bal(host, "bob") == STARTING_BALANCE - 20 + 30
    assert _scoped("heist.state") not in host.store
    # second resolver finds nothing to resolve; no double payout
    _say(host, "status")
    assert _bal(host, "viewer-1") == STARTING_BALANCE + 20


def test_bust_forfeits_stakes(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_roll", lambda pct: False)
    _say(host, "join", arg="40")
    host.now_ms += (DEFAULT_WINDOW_SECONDS + 1) * 1000
    assert "BUSTED" in _say(host, "status")
    assert _bal(host, "viewer-1") == STARTING_BALANCE - 40


def test_already_claimed_heist_is_not_paid_twice(
    host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "_roll", lambda pct: True)
    _say(host, "join", arg="40")
    st = _state(host)
    host.store[_scoped(f"heist.claim.{st.started_ms}")] = b"1"  # another worker claimed it
    host.now_ms += (DEFAULT_WINDOW_SECONDS + 1) * 1000
    assert "wrapped up" in _say(host, "status")
    assert _bal(host, "viewer-1") == STARTING_BALANCE - 40
    assert "try again" in _say(host, "join", arg="5")


def test_join_race_refunds(host: _FakeHost) -> None:
    host.drop_set_after_write = True
    assert "refunded" in _say(host, "join", arg="30")
    assert _bal(host, "viewer-1") == STARTING_BALANCE


def test_corrupt_state_discarded(host: _FakeHost) -> None:
    host.store[_scoped("heist.state")] = b"not json"
    assert "No heist" in _say(host, "status")
    assert _scoped("heist.state") not in host.store


def test_corrupt_balance_fails_loud(host: _FakeHost) -> None:
    host.store[_scoped(_balance_key(_pseud("viewer-1")))] = b"zz"
    with pytest.raises(RuntimeError):
        _say(host, "join", arg="5")
    assert "temporarily unavailable" in host.texts()[-1]


def test_overdraw_floor(host: _FakeHost) -> None:
    ledger = app._Ledger("comm-1", "twitch", "1")
    host.store[_scoped(_balance_key("p"))] = b"10"
    assert _run(ledger.debit("p", 25)) == 0


def test_set_window(host: _FakeHost) -> None:
    assert "only moderators" in _say(host, "config_set_window", arg="window 60")
    assert "only moderators" in _say(host, "config_set_window", arg="window 60", is_mod=False)
    assert "set to 60s" in _say(host, "config_set_window", arg="window 60", is_mod=True)
    _say(host, "join", arg="5")
    assert _state(host).window_s == 60
    for arg, needle in [
        ("window 5", "between"),
        ("window 9999", "between"),
        ("window x", "isn't a whole"),
        ("window", "Usage"),
        (None, "Usage"),
        ("odds 5", "Usage"),
    ]:
        assert needle in _say(host, "config_set_window", arg=arg, is_broadcaster=True)


@pytest.mark.parametrize("raw", [b"junk", b"5"])
def test_bad_window_config_falls_back(host: _FakeHost, raw: bytes) -> None:
    host.store[_scoped("heist.config.window")] = raw
    _say(host, "join", arg="5")
    assert _state(host).window_s == DEFAULT_WINDOW_SECONDS


def test_usage_and_validation(host: _FakeHost) -> None:
    assert _run(dispatch(_env("usage"), {}, http_client=None)).detail == "usage"
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(_env("nope"), {}, http_client=None))
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_env("status", community=None), {}, http_client=None))
    env = _env("status")
    env.event.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(env, {}, http_client=None))


@pytest.mark.parametrize("op", ["get", "set", "increment"])
def test_kv_failures_fail_loud(host: _FakeHost, op: str) -> None:
    host.fail_op = op
    with pytest.raises(RuntimeError, match=f"kv {op} failed"):
        _run(dispatch(_env("join", arg="5"), {}, http_client=None))
    assert "temporarily unavailable" in host.texts()[-1]


def test_delete_failure_fails_loud(host: _FakeHost) -> None:
    host.store[_scoped("heist.state")] = b"not json"
    host.fail_op = "delete"
    with pytest.raises(RuntimeError, match="kv delete failed"):
        _run(dispatch(_env("status"), {}, http_client=None))


def test_logs_are_pii_free(host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "_roll", lambda pct: True)
    _run(transform(_event("!heist 25")))
    _say(host, "join", arg="25")
    host.now_ms += (DEFAULT_WINDOW_SECONDS + 1) * 1000
    _say(host, "status")
    blob = " ".join(f"{m} {fj}" for _, m, fj in host.log_calls)
    assert "heist.resolved" in blob
    assert "viewer-1" not in blob
    assert _pseud("viewer-1") not in blob
    assert "le100" in blob


def test_dispatch_result_shape(host: _FakeHost) -> None:
    result = _run(dispatch(_env("status"), {}, http_client=None))
    assert (result.transport, result.sub_type, result.http_status) == ("twitch", None, None)
