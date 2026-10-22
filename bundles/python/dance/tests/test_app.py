"""Host-native tests for the `dance` bundle's `transform`/`dispatch` logic.

A fake `wit_world` module stands in for the WIT host imports (`flags`, `log`, `relay`), same
pattern as `bundles/python/boop/tests/test_app.py`.
"""

from __future__ import annotations

import asyncio
import random
import sys
import types

import pytest
from app import _SOLO, _TARGETED, _USAGE, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent

ACTOR = "{user:actor-1}"
TARGET = "{user:tgt-9}"


def _run(coro):
    return asyncio.run(coro)


def _event(text, *, actor=ACTOR, channel_id="12345"):
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-21T00:00:00.000Z",
    )


def _install_host(monkeypatch, *, flag):
    relay_calls, log_calls, flag_keys = [], [], []

    def enabled(key, default_value):
        flag_keys.append(key)
        return flag

    flags_mod = types.SimpleNamespace(enabled=enabled)
    relay_mod = types.SimpleNamespace(push=lambda p, m: relay_calls.append((p, m)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields: log_calls.append((lvl, msg, fields)),
    )
    wit = types.ModuleType("wit_world")
    wit.imports = types.SimpleNamespace(flags=flags_mod, relay=relay_mod, log=log_mod)
    monkeypatch.setitem(sys.modules, "wit_world", wit)
    return types.SimpleNamespace(
        relay_calls=relay_calls, log_calls=log_calls, flag_keys=flag_keys
    )


@pytest.fixture
def host(monkeypatch):
    return _install_host(monkeypatch, flag=True)


def _expected(pool, **kw):
    return {t.format(**kw) for t in pool}


def test_no_target_is_a_solo_reply(host) -> None:
    out = _run(transform(_event("!dance")))
    assert out is not None
    assert out.payload["text"] in _expected(_SOLO, actor=ACTOR)


def test_target_mention_is_resolved_by_token(host) -> None:
    out = _run(transform(_event("!dance " + TARGET)))
    assert out is not None
    assert out.payload["text"] in _expected(_TARGETED, actor=ACTOR, target=TARGET)
    assert TARGET in out.payload["text"]


def test_seed_makes_variant_pick_deterministic(host) -> None:
    random.seed(7)
    first = _run(transform(_event("!dance " + TARGET))).payload["text"]
    random.seed(7)
    second = _run(transform(_event("!dance " + TARGET))).payload["text"]
    assert first == second


def test_all_variants_reachable(host) -> None:
    seen = set()
    random.seed(1)
    for _ in range(200):
        seen.add(_run(transform(_event("!dance " + TARGET))).payload["text"])
    assert seen == _expected(_TARGETED, actor=ACTOR, target=TARGET)


@pytest.mark.parametrize(
    "arg", ["plainname", "{user:a} {user:b}", "{user:a} extra", "@@"]
)
def test_non_single_mention_argument_gets_usage(host, arg) -> None:
    out = _run(transform(_event("!dance " + arg)))
    assert out is not None
    assert out.payload["text"] == _USAGE


def test_non_token_actor_uses_generic_fallback(host) -> None:
    out = _run(transform(_event("!dance", actor="rawname")))
    assert out is not None
    assert "rawname" not in out.payload["text"]
    assert out.payload["text"].split(" ", 1)[1].startswith("Someone")


def test_case_insensitive_command(host) -> None:
    assert _run(transform(_event("!DANCE"))) is not None


@pytest.mark.parametrize("text", ["hello", "!other", "!dancex", ""])
def test_non_matching_text_is_dropped(host, text) -> None:
    assert _run(transform(_event(text))) is None
    assert host.flag_keys == []


def test_non_string_text_is_dropped(host) -> None:
    ev = _event("x")
    ev.payload["text"] = 5
    assert _run(transform(ev)) is None


def test_logs_are_pii_free(host) -> None:
    _run(transform(_event("!dance " + TARGET)))
    blob = repr(host.log_calls)
    assert "actor-1" not in blob
    assert "tgt-9" not in blob


def test_dispatch_relays_to_origin_platform(host) -> None:
    out = _run(transform(_event("!dance")))
    env = _envelope(out)
    res = _run(dispatch(env, {}, http_client=None))
    assert res.transport == "twitch"
    assert host.relay_calls[0][0] == "twitch"


def test_dispatch_requires_channel_id(host) -> None:
    out = _run(transform(_event("!dance", channel_id=None)))
    with pytest.raises(ValueError):
        _run(dispatch(_envelope(out), {}, http_client=None))


def _envelope(ev):
    return types.SimpleNamespace(event=ev, community=1)


# hardening: activation-only
def test_runs_on_activation_alone_when_flag_backend_says_off(monkeypatch) -> None:
    """Activation-gated only: an OFF `waddles.command-dance` flag must not drop the command."""
    h = _install_host(monkeypatch, flag=False)
    out = _run(transform(_event("!dance")))
    assert out is not None
    assert h.flag_keys == []


@pytest.mark.parametrize(
    "head",
    [
        "!dance",
        "!DANCE",
        "!Dance",
        "!dances",
        "!dance!",
        "dance",
        "!",
        "!!dance",
        "!dance\u200b",
        "!ask",
        "",
    ],
)
def test_head_match_agrees_with_shared_parser(host, head: str) -> None:
    """The explicit head comparison must accept exactly what `parse_command` accepts.

    regression: the head used to be matched via `try: parse_command(head) / except: return None`
    (flagged as a silent fallback); the explicit comparison replaces it and this pins the two.
    """
    from app import _SPEC
    from waddle_sdk.command import CommandUsageError, parse_command

    try:
        parse_command(head, _SPEC)
        parser_accepts = True
    except CommandUsageError:
        parser_accepts = False
    out = _run(transform(_event(f"{head} hello".strip())))
    assert (out is not None) == parser_accepts
