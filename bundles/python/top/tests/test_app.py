"""Host-native tests for `top`: fake `wit_world` (flags/economy/relay/log), no WASM."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from app import _USAGE, DEFAULT_LIMIT, MAX_LIMIT, dispatch, render, transform
from waddle_sdk import economy
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeEconomyHost

U1, U2, U3 = (f"00000000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))


def _text(h: Host) -> str:
    return str(json.loads(h.relay_calls[-1][1])["text"])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@dataclass
class Host:
    eco: FakeEconomyHost
    relay_calls: list[tuple[str, str]] = field(default_factory=list)
    log_calls: list[tuple[Any, str, str]] = field(default_factory=list)
    flag: bool = True


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Host:
    h = Host(
        eco=FakeEconomyHost(members={U1, U2, U3}, balances={U1: 50, U2: 300, U3: 120})
    )
    flags = types.SimpleNamespace(enabled=lambda key, default: h.flag)
    eco = types.SimpleNamespace(leaderboard=lambda limit: h.eco.leaderboard(limit))
    relay_mod = types.SimpleNamespace(push=lambda p, m: h.relay_calls.append((p, m)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fj: h.log_calls.append((lvl, msg, fj)),
    )
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags, economy=eco, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return h


def _event(text: str) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": "123"},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _envelope(payload: dict[str, Any], platform: str = "twitch") -> StageEnvelope:
    return StageEnvelope(
        tenant="t",
        community="c1",
        app_id="waddles.core.example.top",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ("!top", DEFAULT_LIMIT),
        ("!TOP", DEFAULT_LIMIT),
        ("!top 3", 3),
        ("!top5", 5),
        ("!top10", 10),
        ("!Top10", 10),
        ("!top 1", 1),
        ("  !top   7 ", 7),
    ],
)
def test_matches_and_parses_limit(host: Host, text: str, limit: int) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == "top"
    assert out.payload["limit"] == limit


@pytest.mark.parametrize(
    "text",
    [
        "!top 0",
        "!top 11",
        "!top0",
        "!top11",
        "!top 999999999999",
        "!top abc",
        "!top -1",
        "!top 5 6",
        "!top5 6",
        "!top set",
        "!top enable",
        "!top 1.5",
    ],
)
def test_invalid_replies_usage(host: Host, text: str) -> None:
    out = _run(transform(_event(text)))
    assert out is not None
    assert out.payload["command"] == "usage"
    assert "limit" not in out.payload


@pytest.mark.parametrize("text", ["!topic", "!toptime", "!tops", "hello", "!to", "top"])
def test_non_matching_ignored(host: Host, text: str) -> None:
    assert _run(transform(_event(text))) is None


def test_non_string_text_ignored(host: Host) -> None:
    ev = _event("!top")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_flag_off_drops_after_match(host: Host) -> None:
    host.flag = False
    assert _run(transform(_event("!top"))) is None


def test_dispatch_renders_ranked_tokens(host: Host) -> None:
    res = _run(
        dispatch(
            _envelope({"command": "top", "limit": 2, "channel_id": "123"}),
            {},
            http_client=None,
        )
    )
    assert res.detail == "relayed"
    provider, msg = host.relay_calls[-1]
    assert provider == "twitch"
    assert f"1. {{user:{U2}}} - 300" in msg
    assert f"2. {{user:{U3}}} - 120" in msg
    assert U1 not in msg


def test_dispatch_discord_and_all_members(host: Host) -> None:
    _run(
        dispatch(
            _envelope({"command": "top", "limit": 10, "channel_id": "9"}, "discord"),
            {},
            http_client=None,
        )
    )
    provider, msg = host.relay_calls[-1]
    assert provider == "discord"
    assert msg.count("{user:") == 3


def test_dispatch_empty_board(host: Host) -> None:
    host.eco.balances.clear()
    _run(
        dispatch(
            _envelope({"command": "top", "limit": 5, "channel_id": "1"}),
            {},
            http_client=None,
        )
    )
    assert _text(host) == "nobody has a balance yet."


def test_dispatch_usage(host: Host) -> None:
    _run(
        dispatch(
            _envelope({"command": "usage", "channel_id": "1"}), {}, http_client=None
        )
    )
    assert _text(host) == _USAGE


@pytest.mark.parametrize("limit", [0, 11, True, "5", None])
def test_dispatch_rejects_bad_limit(host: Host, limit: Any) -> None:
    with pytest.raises(ValueError):
        _run(
            dispatch(
                _envelope({"command": "top", "limit": limit, "channel_id": "1"}),
                {},
                http_client=None,
            )
        )
    assert host.relay_calls == []


def test_dispatch_requires_channel(host: Host) -> None:
    with pytest.raises(ValueError):
        _run(dispatch(_envelope({"command": "top", "limit": 5}), {}, http_client=None))


def test_denied_grant_fails_loud_with_generic_reply(host: Host) -> None:
    host.eco.granted = False
    with pytest.raises(economy.DeniedError):
        _run(
            dispatch(
                _envelope({"command": "top", "limit": 5, "channel_id": "1"}),
                {},
                http_client=None,
            )
        )
    assert "temporarily unavailable" in _text(host)
    assert not any(U1 in str(c) for c in host.log_calls)


def test_end_to_end_transform_then_dispatch(host: Host) -> None:
    out = _run(transform(_event("!top2")))
    assert out is not None
    _run(dispatch(_envelope(out.payload), {}, http_client=None))
    assert _text(host).startswith("Top balances: 1. {user:")


def test_logs_are_pii_free(host: Host) -> None:
    out = _run(transform(_event("!top3")))
    assert out is not None
    _run(dispatch(_envelope(out.payload), {}, http_client=None))
    blob = " ".join(f"{m} {fj}" for _, m, fj in host.log_calls)
    assert host.log_calls
    for secret in (U1, U2, U3, "viewer-1", "300", "120"):
        assert secret not in blob
    assert "count" in blob


def test_render_empty_and_limit_constants() -> None:
    assert render([]) == "nobody has a balance yet."
    assert DEFAULT_LIMIT <= MAX_LIMIT <= economy.MAX_LEADERBOARD_LIMIT
