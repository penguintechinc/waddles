"""Host-native tests for the `commands` bundle's enumeration logic."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    CATALOG,
    MAX_REPLY_LEN,
    dispatch,
    enabled_triggers,
    flag_key,
    format_reply,
    transform,
)

_DIR = Path(__file__).resolve().parent.parent


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    def __init__(self) -> None:
        self.off: set[str] = set()
        self.asked: list[tuple[str, bool]] = []
        self.relay: list[tuple[str, str]] = []
        self.logs: list[Any] = []


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    h = _Host()

    def enabled(key: str, default: bool) -> bool:
        h.asked.append((key, default))
        return False if key in h.off else default

    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=types.SimpleNamespace(enabled=enabled),
        relay=types.SimpleNamespace(push=lambda p, m: h.relay.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, f: h.logs.append((lvl, msg, f)),
        ),
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return h


def _ev(text: Any) -> PlatformEvent:
    return PlatformEvent(
        "twitch", "chat.message", "viewer-1", {"text": text, "channel_id": "1"}, "t"
    )


def _full(host: _Host, text: str) -> str | None:
    out = _run(transform(_ev(text)))
    if out is None:
        return None
    env = StageEnvelope(tenant="t", community="c", app_id="x", stage="action", event=out, ts="t")
    _run(dispatch(env, {}, http_client=None))
    return str(json.loads(host.relay[-1][1])["text"])


def test_catalog_is_unique_and_well_formed() -> None:
    suffixes = [s for s, _ in CATALOG]
    triggers = [t for _, t in CATALOG]
    assert len(suffixes) == len(set(suffixes)) and len(triggers) == len(set(triggers))
    assert all(t.startswith("!") for t in triggers)
    assert ("say", "!say") in CATALOG and ("commands", "!commands") in CATALOG


def test_unset_flags_count_as_enabled_and_every_flag_checked(host: _Host) -> None:
    got = _run(enabled_triggers())
    assert got == [t for _, t in CATALOG]
    assert len(host.asked) == len(CATALOG)  # denominator: every catalog entry was evaluated
    assert all(default is True for _, default in host.asked)
    assert (flag_key("fish"), True) in host.asked


def test_explicitly_off_flag_hides_command(host: _Host) -> None:
    host.off = {flag_key("fish"), flag_key("roast")}
    got = _run(enabled_triggers())
    assert "!fish" not in got and "!roast" not in got and "!echo" in got
    assert len(got) == len(CATALOG) - 2


def test_format_reply_basic_and_empty() -> None:
    assert format_reply(["!a", "!b"]) == "Commands: !a, !b"
    assert format_reply([]) == "No commands are currently enabled."


def test_format_reply_truncates_with_more_suffix() -> None:
    trig = [f"!cmd{i:03d}" for i in range(200)]
    reply = format_reply(trig)
    assert len(reply) <= MAX_REPLY_LEN
    shown = reply.count("!cmd")
    assert reply.endswith(f"(+{200 - shown} more)")


def test_full_catalog_reply_fits_in_chat_line(host: _Host) -> None:
    reply = _full(host, "!commands")
    assert (
        reply is not None and reply.startswith("Commands: !about") and len(reply) <= MAX_REPLY_LEN
    )


def test_dispatch_reflects_flag_state(host: _Host) -> None:
    host.off = {flag_key(s) for s, _ in CATALOG}
    assert _full(host, "!commands") == "No commands are currently enabled."


@pytest.mark.parametrize("text", ["!commands now", "!commands set x", "!commands enable"])
def test_args_give_usage(host: _Host, text: str) -> None:
    assert _full(host, text) == "Usage: !commands"


@pytest.mark.parametrize("text", ["!command", "hello", "!commandsx", ""])
def test_non_matching_dropped(host: _Host, text: str) -> None:
    assert _run(transform(_ev(text))) is None
    assert _run(transform(_ev(None))) is None


def test_bad_envelopes_raise(host: _Host) -> None:
    ev = PlatformEvent("twitch", "chat.message", "a", {"action": "list"}, "t")
    env = StageEnvelope(tenant="t", community="c", app_id="x", stage="action", event=ev, ts="t")
    with pytest.raises(ValueError):
        _run(dispatch(env, {}, http_client=None))
    ev2 = PlatformEvent("twitch", "chat.message", "a", {"channel_id": "1", "action": "x"}, "t")
    env2 = StageEnvelope(tenant="t", community="c", app_id="x", stage="action", event=ev2, ts="t")
    with pytest.raises(ValueError):
        _run(dispatch(env2, {}, http_client=None))


def test_logs_pii_free(host: _Host) -> None:
    _full(host, "!commands")
    blob = json.dumps(host.logs, default=str)
    assert "viewer-1" not in blob and host.logs


def test_manifests_perms_and_no_attribution() -> None:
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        body = (_DIR / name).read_text()
        for pid in ("flags.read", '"chat.send:discord"', '"chat.send:twitch"'):
            assert f"id: {pid}" in body
        assert "storage.kv" not in body and "Psychoboy" not in body
