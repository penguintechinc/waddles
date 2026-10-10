"""Host-native tests for the `guess` bundle's stateful `transform`/`dispatch` logic.

Mirrors `bundles/python/hangman/tests/test_app.py` (shared fake-`wit_world` kv host).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from waddle_sdk import community_kv
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import validate_key
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import _ACTIVE_KEY, _NO_ACTIVE_GAME, _USAGE, RANGE_MAX, RANGE_MIN, dispatch, transform


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@dataclass
class _FakeHost:
    kv: FakeKvHost
    relay_calls: list[tuple[str, str]] = field(default_factory=list)
    log_calls: list[tuple[Any, str, str]] = field(default_factory=list)
    flag_enabled: bool = True

    @property
    def store(self) -> dict[str, bytes]:
        return self.kv.store


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    state = _FakeHost(kv=install_fake_kv_host(monkeypatch))
    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(
        enabled=lambda key, default_value: state.flag_enabled
    )
    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda provider, msg: state.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: state.log_calls.append((lvl, msg, fields_json)),
    )
    return state


def _event(text: str) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": "12345"},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(
    action: str, *, community: str | None = "comm-1", arg: str | None = None
) -> StageEnvelope:
    payload: dict[str, Any] = {"action": action, "channel_id": "12345"}
    if arg is not None:
        payload["arg"] = arg
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.guess",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-09T00:00:00.000Z",
        ),
        ts="2026-10-09T00:00:00.000Z",
    )


def _text(host: _FakeHost) -> str:
    text = json.loads(host.relay_calls[-1][1])["text"]
    assert isinstance(text, str)
    return text


def _scoped(key: str, community: str = "comm-1") -> str:
    scoped = community_kv._scoped_key(community, key)
    assert isinstance(scoped, str)
    return scoped


def _state(host: _FakeHost, community: str = "comm-1") -> dict[str, Any]:
    return json.loads(host.store[_scoped(_ACTIVE_KEY, community)].decode())


def _start(monkeypatch: pytest.MonkeyPatch, secret: int = 42) -> None:
    monkeypatch.setattr("app.random.randint", lambda a, b: secret)
    _run(dispatch(_envelope("start"), {}, http_client=None))


def _guess(n: str) -> StageEnvelope:
    return _envelope("guess", arg=n)


@pytest.mark.parametrize(
    ("text", "action", "arg"),
    [
        ("!guess", "start", None),
        ("!GUESS start", "start", None),
        ("!guess giveup", "giveup", None),
        ("!guess reset", "giveup", None),
        ("!guess 50", "guess", "50"),
        ("!guess abc", "guess", "abc"),
        ("!guess 1 2 3", "usage", _USAGE),
    ],
)
def test_transform_classification(
    text: str, action: str, arg: str | None, fake_host: _FakeHost
) -> None:
    result = _run(transform(_event(text)))
    assert result is not None
    assert result.payload["action"] == action
    assert result.payload.get("arg") == arg


@pytest.mark.parametrize("text", ["!guessing", "guess", "hello", ""])
def test_non_matching_text_ignored(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_event(text))) is None


def test_non_chat_payload_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_event("!guess 5"))) is None


def test_start_picks_secret_in_range(fake_host: _FakeHost) -> None:
    for _ in range(30):
        fake_host.store.clear()
        _run(dispatch(_envelope("start"), {}, http_client=None))
        st = _state(fake_host)
        assert RANGE_MIN <= st["secret"] <= RANGE_MAX
        assert (st["low"], st["high"], st["attempts"]) == (RANGE_MIN, RANGE_MAX, 0)


def test_start_again_does_not_overwrite(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(monkeypatch)
    before = fake_host.store[_scoped(_ACTIVE_KEY)]
    _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "already active" in _text(fake_host)
    assert fake_host.store[_scoped(_ACTIVE_KEY)] == before


def test_round_lifecycle_higher_lower_win_then_new_round(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(monkeypatch, 42)

    _run(dispatch(_guess("50"), {}, http_client=None))
    assert "lower" in _text(fake_host)
    assert "between 1 and 49" in _text(fake_host)

    _run(dispatch(_guess("10"), {}, http_client=None))
    assert "higher" in _text(fake_host)
    assert "between 11 and 49" in _text(fake_host)
    assert _state(fake_host)["attempts"] == 2

    _run(dispatch(_guess("42"), {}, http_client=None))
    assert "Correct" in _text(fake_host)
    assert "attempt 3" in _text(fake_host)
    assert _scoped(_ACTIVE_KEY) not in fake_host.store

    _run(dispatch(_guess("42"), {}, http_client=None))
    assert _text(fake_host) == _NO_ACTIVE_GAME

    _start(monkeypatch, 7)
    assert _state(fake_host)["secret"] == 7


@pytest.mark.parametrize("bad", ["abc", "0", "101", "-5", "", "3.5"])
def test_invalid_or_out_of_range_guess_is_noop(
    bad: str, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(monkeypatch)
    before = fake_host.store[_scoped(_ACTIVE_KEY)]
    _run(dispatch(_guess(bad), {}, http_client=None))
    assert _text(fake_host) == _USAGE
    assert fake_host.store[_scoped(_ACTIVE_KEY)] == before


def test_giveup_reveals_and_clears(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _start(monkeypatch, 33)
    _run(dispatch(_envelope("giveup"), {}, http_client=None))
    assert "33" in _text(fake_host)
    assert _scoped(_ACTIVE_KEY) not in fake_host.store


def test_giveup_and_guess_with_no_round(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("giveup"), {}, http_client=None))
    assert _text(fake_host) == _NO_ACTIVE_GAME
    _run(dispatch(_guess("5"), {}, http_client=None))
    assert _text(fake_host) == _NO_ACTIVE_GAME


def test_one_round_per_community(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _start(monkeypatch, 11)
    monkeypatch.setattr("app.random.randint", lambda a, b: 22)
    _run(dispatch(_envelope("start", community="comm-2"), {}, http_client=None))
    assert _state(fake_host, "comm-1")["secret"] == 11
    assert _state(fake_host, "comm-2")["secret"] == 22


def test_corrupt_state_self_heals(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = b'{"secret": "x"}'
    _run(dispatch(_guess("5"), {}, http_client=None))
    assert _text(fake_host) == _NO_ACTIVE_GAME


def test_kv_key_has_no_colon() -> None:
    assert ":" not in _ACTIVE_KEY
    validate_key(_scoped(_ACTIVE_KEY))


def test_missing_community_and_channel_and_action_raise(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("start", community=None), {}, http_client=None))
    bad = _envelope("start")
    bad.event.payload["channel_id"] = None
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(bad, {}, http_client=None))
    with pytest.raises(ValueError, match="unrecognized"):
        _run(dispatch(_envelope("bogus"), {}, http_client=None))


def test_usage_action_replies_usage(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert _text(fake_host) == _USAGE


def test_logs_never_contain_guess_values_or_actor(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(monkeypatch, 42)
    _run(dispatch(_guess("77"), {}, http_client=None))
    blob = json.dumps([c[1:] for c in fake_host.log_calls])
    assert "77" not in blob
    assert "viewer-1" not in blob


def test_kv_failure_replies_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("backend down")

    monkeypatch.setattr(community_kv, "get", boom)
    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "temporarily unavailable" in _text(fake_host)
