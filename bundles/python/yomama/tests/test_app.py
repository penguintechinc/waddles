"""Host-native tests for the `yomama` bundle (fake `flags`/`relay`/`log` host, no kv)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import app as bundle
import pytest
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self) -> None:
        self.relay_calls: list[tuple[str, Any]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_calls: list[str] = []
        self.flag = True
        self.kv_touched = False


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host()

    def _enabled(key: str, default_value: bool) -> bool:
        h.flag_calls.append(key)
        return h.flag

    def _no_kv(*_a: Any, **_k: Any) -> None:
        h.kv_touched = True

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=_enabled),
        relay=types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, f: h.log_calls.append((lvl, msg, f)),
        ),
        kv=types.SimpleNamespace(
            get=_no_kv, set=_no_kv, delete=_no_kv, increment=_no_kv
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return h


def _event(
    text: str,
    *,
    actor: str = "{user:11111111-1111-1111-1111-111111111111}",
    platform: str = "twitch",
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": "chan-1"},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _say(text: str, **kw: Any) -> str:
    out = _run(transform(_event(text, **kw)))
    assert out is not None
    return str(out.payload["text"])


def _logs(h: _Host) -> str:
    return " ".join(f"{m} {f}" for _, m, f in h.log_calls).lower()


def test_non_command_and_prefix_collision_ignored(host: _Host) -> None:
    assert _run(transform(_event("hello"))) is None
    assert _run(transform(_event("!yomamas"))) is None
    assert host.flag_calls == []  # flag is only read AFTER a match


def test_non_string_text_ignored(host: _Host) -> None:
    ev = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={"channel_id": "c"},
        occurred_at="t",
    )
    assert _run(transform(ev)) is None


def test_flag_off_returns_none_and_queries_expected_key(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!yomama"))) is None
    assert host.flag_calls == ["waddles.command-yomama"]


def test_bare_addresses_caller_token(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle, "_pick", lambda pool: pool[0])
    assert (
        _say("!yomama")
        == f"💥 {{user:11111111-1111-1111-1111-111111111111}}, {bundle._POOL[0]}"
    )


def test_bare_with_unparseable_actor_is_generic(host: _Host) -> None:
    assert _say("!yomama", actor="rawname").startswith("💥 Hey, ")


def test_mention_target_echoed_as_token(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle, "_pick", lambda pool: pool[1])
    tok = "{user:22222222-2222-2222-2222-222222222222}"
    assert _say(f"!yomama {tok}") == f"💥 {tok}, {bundle._POOL[1]}"


def test_raw_handle_target_rejected_never_echoed(host: _Host) -> None:
    reply = _say("!yomama @SecretName")
    assert reply == bundle._BAD_TARGET_MSG
    assert "secretname" not in reply.lower()
    assert "secretname" not in _logs(host)


def test_extra_args_get_usage(host: _Host) -> None:
    assert _say("!yomama a b") == bundle._USAGE
    assert _say("!yomama {user:abc} {user:def}") == bundle._USAGE


def test_matches_case_insensitively(host: _Host) -> None:
    assert _run(transform(_event("!YOMAMA"))) is not None


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    out = _run(transform(_event("!yomama")))
    assert out is not None
    env = StageEnvelope(
        tenant="t",
        community="c",
        app_id="waddles.core.example.yomama",
        stage="action",
        event=out,
        ts="2026-10-21T00:00:00.000Z",
    )
    assert _run(dispatch(env, {}, http_client=None)).detail == "relayed"
    assert host.relay_calls[0][0] == "twitch"
    assert json.loads(host.relay_calls[0][1])["channel"] == "chan-1"
    for payload in ({"channel_id": "c"}, {"text": "x"}):
        bad = PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="a",
            payload=payload,
            occurred_at="t",
        )
        bad_env = StageEnvelope(
            tenant="t", community="c", app_id="x", stage="action", event=bad, ts="t"
        )
        with pytest.raises(ValueError):
            _run(dispatch(bad_env, {}, http_client=None))


def test_stateless_no_kv_and_pool_clean(host: _Host) -> None:
    for _ in range(30):
        _say("!yomama")
    assert host.kv_touched is False
    assert len(bundle._POOL) >= 15
    assert len(set(bundle._POOL)) == len(bundle._POOL)
    banned = ("slur",)
    assert not any(b in p.lower() for p in bundle._POOL for b in banned)
