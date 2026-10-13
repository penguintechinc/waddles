"""Backfill coverage for the `slap` bundle: target-shape edges, flag fail-closed, PII-free logs.

Complements `test_app.py` with the invariants it does not pin: target length/charset boundaries,
case-insensitive matching, the flavor-bank selection contract, the flag's fail-closed default
(including a flag-server outage and a missing `wit_world`), the PII-free-log regression on the
valid / usage / solo paths, relay payload exactness, and the static `_entry_wiring` re-export.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import _entry_wiring
import app
import pytest
from app import (
    _SOLO_SLAPS,
    _TARGETED_SLAPS,
    _USAGE,
    FLAG_KEY,
    DispatchResult,
    _normalize_target,
    dispatch,
    transform,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: A distinctive token that must never appear in any log line or flag call.
CANARY = "CANARYuser9f3a"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(text: str, *, channel_id: str | None = "chan-1") -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=CANARY + "_actor",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(platform: str, text: str, *, channel_id: str | None = "chan-1") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.slap",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=CANARY + "_actor",
            payload={"text": text, "channel_id": channel_id},
            occurred_at="2026-10-09T00:00:00.000Z",
        ),
        ts="2026-10-09T00:00:00.000Z",
    )


class _Host:
    """Recorded WIT host calls for one test."""

    def __init__(self) -> None:
        self.flag_calls: list[tuple[str, bool]] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_value: bool | None = True  # `None` -> echo the supplied default (outage)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Install a fake `wit_world` exposing flags/relay/log and return its call records."""
    state = _Host()

    def _enabled(key: str, default_value: bool) -> bool:
        state.flag_calls.append((key, default_value))
        return default_value if state.flag_value is None else state.flag_value

    imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=_enabled),
        relay=types.SimpleNamespace(push=lambda p, m: state.relay_calls.append((p, m))),
        log=types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields: state.log_calls.append((lvl, msg, fields)),
        ),
    )
    world = types.ModuleType("wit_world")
    world.imports = imports  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return state


def _reply_text(result: PlatformEvent | None) -> str:
    assert result is not None
    return str(result.payload["text"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a", "a"),
        ("@a", "a"),
        ("_under", "_under"),
        ("dot.name-ok", "dot.name-ok"),
        ("A" * 32, "A" * 32),
        ("@" + "A" * 32, "A" * 32),
        ("", None),
        ("@", None),
        ("@@x", None),
        ("-lead", None),
        (".lead", None),
        ("A" * 33, None),
        ("bad name", None),
        ("emoji\U0001f41f", None),
    ],
)
def test_normalize_target_shape_boundaries(raw: str, expected: str | None) -> None:
    assert _normalize_target(raw) == expected


def test_valid_targets_render_in_every_targeted_template(host: _Host) -> None:
    rendered = {_reply_text(_run(transform(_event("!slap bob")))) for _ in range(200)}
    assert rendered <= {t.format(target="bob") for t in _TARGETED_SLAPS}
    assert len(rendered) > 1, "random.choice should reach more than one template over 200 draws"


def test_bare_slap_draws_only_from_the_solo_bank(host: _Host) -> None:
    drawn = {_reply_text(_run(transform(_event("!slap")))) for _ in range(200)}
    assert drawn <= set(_SOLO_SLAPS)
    assert len(drawn) > 1


def test_bank_selection_is_delegated_to_random_choice(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    def _first(seq: tuple[str, ...]) -> str:
        seen.append(seq)
        return seq[0]

    monkeypatch.setattr(app.random, "choice", _first)
    assert _reply_text(_run(transform(_event("!slap")))) == _SOLO_SLAPS[0]
    assert _reply_text(_run(transform(_event("!slap @x")))) == _TARGETED_SLAPS[0].format(target="x")
    assert seen == [_SOLO_SLAPS, _TARGETED_SLAPS]


@pytest.mark.parametrize("text", ["!SLAP", "!Slap bob", "   !slap   bob   "])
def test_command_match_is_case_insensitive_and_whitespace_tolerant(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is not None


@pytest.mark.parametrize("bad", ["bob!", "a" * 33, "-x", "@@bob", "x y z"])
def test_malformed_targets_reply_usage_and_never_pick_a_template(
    bad: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_seq: Any) -> str:
        raise AssertionError("no template draw on the usage path")

    monkeypatch.setattr(app.random, "choice", _boom)
    assert _reply_text(_run(transform(_event(f"!slap {bad}")))) == _USAGE


def test_flag_is_checked_with_a_fail_closed_default(host: _Host) -> None:
    _run(transform(_event("!slap")))
    assert host.flag_calls == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-slap"


def test_flag_server_outage_echoing_the_default_keeps_the_command_off(host: _Host) -> None:
    host.flag_value = None
    assert _run(transform(_event("!slap bob"))) is None
    assert host.log_calls == []


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)  # makes `import wit_world` raise
    assert _run(transform(_event("!slap"))) is None


def test_flag_off_does_not_log_or_draw(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.flag_value = False

    def _boom(_seq: Any) -> str:
        raise AssertionError("random.choice must not run while the flag is off")

    monkeypatch.setattr(app.random, "choice", _boom)
    assert _run(transform(_event("!slap bob"))) is None
    assert host.log_calls == []


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 7}, {"text": ["!slap"]}])
def test_non_string_text_is_ignored_before_the_flag_check(
    payload: dict[str, Any], host: _Host
) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="chat.message", actor=None, payload=payload, occurred_at=""
    )
    assert _run(transform(event)) is None
    assert host.flag_calls == []


def test_transform_preserves_platform_event_type_and_channel(host: _Host) -> None:
    event = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="a",
        payload={"text": "!slap", "channel_id": "C9"},
        occurred_at="t0",
    )
    result = _run(transform(event))
    assert result is not None
    assert (result.platform, result.event_type, result.occurred_at) == (
        "discord",
        "chat.message",
        "t0",
    )
    assert result.payload["channel_id"] == "C9"


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("!slap", "solo"),
        (f"!slap {CANARY}", "targeted"),
        (f"!slap @{CANARY}", "targeted"),
        (f"!slap {CANARY}!!", "usage"),
        (f"!slap {CANARY} {CANARY}", "usage"),
    ],
)
def test_logs_contain_only_platform_and_shape_never_user_text(
    text: str, shape: str, host: _Host
) -> None:
    """PII-free-log regression: the typed target / actor never reach the log sink."""
    _run(transform(_event(text)))
    _run(dispatch(_envelope("twitch", f"reply {CANARY}"), {}, http_client=None))

    assert host.log_calls, "expected at least one log line (denominator must be non-zero)"
    transform_fields = json.loads(host.log_calls[0][2])
    assert transform_fields == {"platform": "twitch", "shape": shape}
    for _level, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_dispatch_relays_exact_text_and_reports_the_origin_transport(host: _Host) -> None:
    result = _run(dispatch(_envelope("discord", "hello"), {"ignored": 1}, http_client=object()))

    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None
    provider, payload = host.relay_calls[0]
    assert provider == "discord"
    assert json.loads(payload) == {"channel": "chan-1", "text": "hello"}


def test_dispatch_without_text_relays_an_empty_string_not_a_crash(host: _Host) -> None:
    envelope = _envelope("twitch", "x")
    del envelope.event.payload["text"]
    _run(dispatch(envelope, {}, http_client=None))
    assert json.loads(host.relay_calls[0][1])["text"] == ""


@pytest.mark.parametrize("channel_id", [None, ""])
def test_dispatch_fails_loud_without_a_channel_and_never_relays(
    channel_id: str | None, host: _Host
) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("twitch", "x", channel_id=channel_id), {}, http_client=None))
    assert host.relay_calls == []


def test_dispatch_result_rejects_unknown_attributes() -> None:
    result = DispatchResult(transport="twitch", detail="relayed")
    with pytest.raises(AttributeError):
        result.bogus = 1  # type: ignore[attr-defined]


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
