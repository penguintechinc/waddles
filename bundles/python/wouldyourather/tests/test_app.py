"""Host-native tests for the `wouldyourather` bundle -- fake `wit_world` stands in for the WIT imports."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest
from app import PROMPTS, USAGE, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: Crude/adult/violent terms that must never appear in a canned list.
BANNED = ("sex", "drunk", "kill", "naked", "drug", "porn", "nude", "die ")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self) -> None:
        self.flag = True
        self.flag_keys: list[str] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, Any]] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host()

    def _enabled(key: str, default_value: bool) -> bool:
        h.flag_keys.append(key)
        return h.flag

    flags_mod = types.SimpleNamespace(enabled=_enabled)
    relay_mod = types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, m)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: h.log_calls.append((lvl, msg, fields)),
    )
    fake = types.ModuleType("wit_world")
    fake.imports = types.SimpleNamespace(flags=flags_mod, relay=relay_mod, log=log_mod)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake)
    return h


def _event(
    text: str, *, actor: str = "{user:actor-token}", channel_id: str | None = "chan-1"
) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _say(text: str) -> str:
    result = _run(transform(_event(text)))
    assert result is not None
    return str(result.payload["text"])


def _envelope(event: PlatformEvent) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.wouldyourather",
        stage="action",
        event=event,
        ts="2026-10-21T00:00:00.000Z",
    )


def _log_text(h: _Host) -> str:
    return " ".join(f"{m} {f}" for _, m, f in h.log_calls).lower()


def test_flag_off_drops_after_match_and_uses_default_off_key(host: _Host) -> None:
    host.flag = False
    assert _run(transform(_event("!wyr"))) is None
    assert host.flag_keys == ["waddles.command-wouldyourather"]


def test_non_matching_never_touches_flag(host: _Host) -> None:
    for text in ("hello", "", "!wyrx", "!wyr2"):
        assert _run(transform(_event(text))) is None
    notext = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="a",
        payload={},
        occurred_at="t",
    )
    assert _run(transform(notext)) is None
    assert host.flag_keys == []


def test_reply_keeps_platform_and_channel(host: _Host) -> None:
    result = _run(transform(_event("!wyr")))
    assert result is not None
    assert result.platform == "twitch"
    assert result.payload["channel_id"] == "chan-1"


def test_wiring_exports() -> None:
    import _entry_wiring

    assert _entry_wiring.bundle_transform is transform
    assert _entry_wiring.bundle_dispatch is dispatch


def test_dispatch_relays_and_rejects_bad_payload(host: _Host) -> None:
    ev = _event("x")
    ev.payload["text"] = "hi there"
    res = _run(dispatch(_envelope(ev), {}, http_client=None))
    assert res.transport == "twitch"
    assert [(p, json.loads(m)) for p, m in host.relay_calls] == [
        ("twitch", {"channel": "chan-1", "text": "hi there"})
    ]
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(_event("x", channel_id=None)), {}, http_client=None))
    blank = _event("x")
    blank.payload["text"] = ""
    with pytest.raises(ValueError, match="text"):
        _run(dispatch(_envelope(blank), {}, http_client=None))


def test_logs_are_pii_free(host: _Host) -> None:
    _run(transform(_event("!wyr @secret-name", actor="rawhandle")))
    out = _log_text(host)
    assert "rawhandle" not in out
    assert "secret-name" not in out
    assert "actor-token" not in out


@pytest.mark.parametrize("cmd", ["!wyr", "!WYR", "!wouldyourather", "  !wyr  "])
def test_prompt_is_from_static_list(cmd: str, host: _Host) -> None:
    reply = _say(cmd)
    assert any(reply == f"Would you rather {a} or {b}?" for a, b in PROMPTS)


def test_all_prompts_are_wellformed_and_sfw() -> None:
    assert len(PROMPTS) >= 20 and len(set(PROMPTS)) == len(PROMPTS)
    for a, b in PROMPTS:
        assert a and b and a != b
        assert not any(w in f"{a} {b}".lower() for w in BANNED)


@pytest.mark.parametrize("text", ["!wyr foo", "!wyr add x", "!wouldyourather enable"])
def test_malformed_gets_usage_not_echo(text: str, host: _Host) -> None:
    assert _say(text) == USAGE
