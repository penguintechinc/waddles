"""Host-native tests for the `riddle` bundle (fake `wit_world`; same pattern as `eightball`)."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import app
import pytest
from app import dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def _run(coro):
    return asyncio.run(coro)


def _event(text, *, channel_id="12345", platform="twitch"):
    return PlatformEvent(
        platform=platform, event_type="chat.message", actor="viewer-1",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


@pytest.fixture
def host(monkeypatch):
    """Fake WIT host: flag togglable and recorded, relay/log recorded."""
    st = types.SimpleNamespace(flag=True, relay=[], logs=[], flag_keys=[])

    def enabled(key, default_value):
        st.flag_keys.append((key, default_value))
        return st.flag

    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=enabled),
        relay=types.SimpleNamespace(push=lambda p, m: st.relay.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: st.logs.append((lvl, msg, fields)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return st


def _text(text, **kw):
    res = _run(transform(_event(text, **kw)))
    return None if res is None else res.payload["text"]


def _envelope(platform="twitch", text="reply", channel_id="12345"):
    return StageEnvelope(
        tenant="tenant-1", community=None, app_id="waddles.core.example.riddle", stage="action",
        event=_event(text, channel_id=channel_id, platform=platform),
        ts="2026-10-21T00:00:00.000Z",
    )


@pytest.mark.parametrize("text", ["!riddleing", "riddle", "hello", "", "!other"])
def test_non_matching_is_ignored_without_flag_lookup(host, text):
    assert _text(text) is None
    assert host.flag_keys == []


def test_non_text_payload_ignored(host):
    ev = PlatformEvent(platform="twitch", event_type="x", actor=None, payload={}, occurred_at="")
    assert _run(transform(ev)) is None


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_dispatch_relays_to_origin_platform(host, platform):
    res = _run(dispatch(_envelope(platform, "hello there"), {}, http_client=None))
    assert res.transport == platform
    provider, msg = host.relay[0]
    assert provider == platform
    assert json.loads(msg) == {"channel": "12345", "text": "hello there"}


def test_dispatch_requires_channel_id(host):
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope(channel_id=None), {}, http_client=None))
    assert host.relay == []


def test_logs_are_pii_free(host):
    _run(transform(_event("!riddle zzzsecret")))
    _run(dispatch(_envelope(), {}, http_client=None))
    assert host.logs
    for _lvl, message, fields in host.logs:
        blob = message + fields
        assert "viewer-1" not in blob
        assert "zzzsecret" not in blob


def test_bare_riddle_posts_numbered_riddle_without_answer(host):
    out = _text("!riddle")
    assert out.startswith("Riddle #")
    n = int(out.split("#")[1].split(":")[0])
    riddle, answer = app.RIDDLES[n - 1]
    assert riddle in out and answer not in out


def test_seeded_rng_is_deterministic():
    import random
    a = app.build_reply(None, rng=random.Random(7))
    assert a == app.build_reply(None, rng=random.Random(7))


def test_all_numbers_round_trip_answer(host):
    for i, (_, answer) in enumerate(app.RIDDLES, start=1):
        assert _text(f"!riddle answer {i}") == f"Riddle #{i} answer: {answer}"
    assert _text("!riddle answer #1") == _text("!riddle answer 1")


@pytest.mark.parametrize("arg", ["answer", "answer 0", "answer 999", "answer abc", "answer -1",
                                 "answer \u0661"])
def test_bad_answer_arg_gets_usage(host, arg):
    assert _text(f"!riddle {arg}").startswith(app.USAGE)


@pytest.mark.parametrize("arg", ["nonsense", "list", "add foo"])
def test_unknown_subcommand_gets_usage(host, arg):
    assert _text(f"!riddle {arg}") == app.USAGE


def test_riddles_nonempty_and_unique():
    assert len(app.RIDDLES) == len({r for r, _ in app.RIDDLES}) >= 10


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(host) -> None:
    """Activation-gated only: an OFF `waddles.command-riddle` flag must not drop the command."""
    host.flag = False
    out = _run(transform(_event("!riddle")))
    assert out is not None
    assert host.flag_keys == []
