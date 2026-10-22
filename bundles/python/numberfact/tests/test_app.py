"""Host-native tests for the `numberfact` bundle (fake `wit_world`; same pattern as `eightball`)."""

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
        tenant="tenant-1", community=None, app_id="waddles.core.example.numberfact", stage="action",
        event=_event(text, channel_id=channel_id, platform=platform),
        ts="2026-10-21T00:00:00.000Z",
    )


@pytest.mark.parametrize("text", ["!numberfacting", "numberfact", "hello", "", "!other"])
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
    _run(transform(_event("!numberfact 987654321")))
    _run(dispatch(_envelope(), {}, http_client=None))
    assert host.logs
    for _lvl, message, fields in host.logs:
        blob = message + fields
        assert "viewer-1" not in blob
        assert "987654321" not in blob


def test_curated_number_gets_curated_fact(host):
    assert _text("!numberfact 42") == app.FACTS[42]
    assert _text("!numberfact 6") == app.FACTS[6]


def test_given_number_is_deterministic(host):
    assert _text("!numberfact 1234567") == _text("!numberfact 1234567")


@pytest.mark.parametrize("n,needle", [
    (97, "prime"), (49, "perfect square (7 x 7)"), (15, "composite odd"),
    (18, "composite even"), (-5, "negative"),
])
def test_computed_property_facts(host, n, needle):
    assert needle in app.fact_for(n)
    assert _text(f"!numberfact {n}") == app.fact_for(n)


def test_bare_is_random_curated_and_seedable(host):
    import random
    assert _text("!numberfact") in set(app.FACTS.values())
    assert app.build_reply(None, rng=random.Random(3)) == app.build_reply(None, rng=random.Random(3))


@pytest.mark.parametrize("arg", ["abc", "1.5", "--3", "+", "\u0661", "list", "add"])
def test_bad_arg_gets_usage(host, arg):
    assert _text(f"!numberfact {arg}") == app.USAGE


def test_huge_number_rejected(host):
    assert _text(f"!numberfact {10**13}").startswith(app.USAGE)


def test_prime_helper_edges():
    assert [app._is_prime(i) for i in (0, 1, 2, 3, 4, 9, 11)] == [False, False, True, True, False, False, True]


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(host) -> None:
    """Activation-gated only: an OFF `waddles.command-numberfact` flag must not drop the command."""
    host.flag = False
    out = _run(transform(_event("!numberfact 7")))
    assert out is not None
    assert host.flag_keys == []
