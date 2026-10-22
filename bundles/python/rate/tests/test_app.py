"""Host-native tests for the `rate` bundle (fake `wit_world`; same pattern as `eightball`)."""

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
        tenant="tenant-1", community=None, app_id="waddles.core.example.rate", stage="action",
        event=_event(text, channel_id=channel_id, platform=platform),
        ts="2026-10-21T00:00:00.000Z",
    )


@pytest.mark.parametrize("text", ["!rateing", "rate", "hello", "", "!other"])
def test_non_matching_is_ignored_without_flag_lookup(host, text):
    assert _text(text) is None
    assert host.flag_keys == []


def test_flag_off_drops_after_match_default_false(host):
    host.flag = False
    assert _text("!rate") is None
    assert host.flag_keys == [("waddles.command-rate", False)]


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
    _run(transform(_event("!rate my secret pizza")))
    _run(dispatch(_envelope(), {}, http_client=None))
    assert host.logs
    for _lvl, message, fields in host.logs:
        blob = message + fields
        assert "viewer-1" not in blob
        assert "my secret pizza" not in blob


def test_score_deterministic_case_insensitive_and_in_range():
    assert app.score_for("Pizza") == app.score_for("  pizza ")
    assert all(0 <= app.score_for(str(i)) <= 10 for i in range(200))
    assert {app.score_for(str(i)) for i in range(500)} == set(range(11))


def test_known_vector_pins_hash_scheme():
    import hashlib
    assert app.score_for("pizza") == hashlib.sha256(b"pizza").digest()[0] % 11


def test_reply_format_and_stability(host):
    first = _text("!rate pizza")
    assert first == _text("!rate pizza")
    score = app.score_for("pizza")
    assert first == f"I rate pizza {score}/10 -- {app.FLAVOR[score]}"


def test_bare_rate_replies_usage(host):
    assert _text("!rate") == app.USAGE


@pytest.mark.parametrize("verb", ["list", "add", "set"])
def test_grammar_verb_word_is_rated_not_swallowed(host, verb):
    assert _text(f"!rate {verb}").startswith(f"I rate {verb} ")


def test_long_input_capped_and_mentions_defused(host):
    out = _text("!rate @everyone " + "x" * 500)
    assert "@everyone" not in out
    assert len(out) < app.MAX_THING_LEN + 60


def test_flavor_covers_every_score():
    assert len(app.FLAVOR) == 11
