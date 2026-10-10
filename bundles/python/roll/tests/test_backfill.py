"""Backfill coverage for the `roll` bundle: dice math, bounds, flag fail-closed, PII-free logs.

Complements `test_app.py` (grammar/flag/relay basics) with the invariants it does not pin:
the exact dice arithmetic (via a deterministic `random.randint`), both inclusive bounds, case
insensitivity, the flag's fail-closed default, the "extra tokens" non-match, the PII-free-log
regression for user-typed spec text, and the static `_entry_wiring` re-export.
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
    DEFAULT_SIDES,
    FLAG_KEY,
    MAX_COUNT,
    MAX_SIDES,
    USAGE_TEXT,
    DispatchResult,
    dispatch,
    transform,
)
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: A distinctive token that must never appear in any log line, relay payload, or flag call.
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
        app_id="waddles.core.example.roll",
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


def test_dice_arithmetic_uses_inclusive_one_to_sides_and_sums_the_rolls(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`2d6` draws exactly two values from `randint(1, 6)` and reports their sum."""
    drawn: list[tuple[int, int]] = []
    values = iter([3, 5])

    def _fake_randint(low: int, high: int) -> int:
        drawn.append((low, high))
        return next(values)

    monkeypatch.setattr(app.random, "randint", _fake_randint)

    assert _reply_text(_run(transform(_event("!roll 2d6")))) == "\U0001f3b2 rolls [3, 5] (total 8)"
    assert drawn == [(1, 6), (1, 6)]


def test_bare_roll_defaults_to_one_d20(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[tuple[int, int]] = []
    monkeypatch.setattr(app.random, "randint", lambda lo, hi: drawn.append((lo, hi)) or 7)

    assert "rolls [7] (total 7)" in _reply_text(_run(transform(_event("!roll"))))
    assert drawn == [(1, DEFAULT_SIDES)]


@pytest.mark.parametrize(
    ("spec", "dice"),
    [("1d2", 1), (f"{MAX_COUNT}d{MAX_SIDES}", MAX_COUNT), ("1D20", 1), ("007d006", 7)],
)
def test_inclusive_bounds_and_case_insensitive_spec_are_accepted(
    spec: str, dice: int, host: _Host
) -> None:
    text = _reply_text(_run(transform(_event(f"!roll {spec}"))))
    assert text != USAGE_TEXT
    assert text.count(",") == dice - 1


@pytest.mark.parametrize("text", ["!ROLL", "!Roll 2d6", "  !roll  2d6  "])
def test_command_match_is_case_insensitive_and_whitespace_tolerant(text: str, host: _Host) -> None:
    assert _run(transform(_event(text))) is not None


@pytest.mark.parametrize("spec", ["1000d6", "2d10000", "-1d6", "2d-6", "d6", "2d", "2 d6"])
def test_overlong_signed_or_split_specs_reply_usage(spec: str, host: _Host) -> None:
    """Over-wide digit runs, signs, and split tokens never reach `randint`."""
    result = _run(transform(_event(f"!roll {spec}")))
    # `2 d6` is two tokens -> the command pattern itself does not match -> None.
    if spec == "2 d6":
        assert result is None
    else:
        assert _reply_text(result) == USAGE_TEXT


def test_extra_trailing_tokens_do_not_match_the_command_pattern(host: _Host) -> None:
    """Documented limitation: `!roll 2d6 extra` is not a recognized invocation (no reply)."""
    assert _run(transform(_event("!roll 2d6 extra"))) is None
    assert host.relay_calls == []


def test_flag_is_checked_with_a_fail_closed_default(host: _Host) -> None:
    _run(transform(_event("!roll")))
    assert host.flag_calls == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-roll"


def test_flag_server_outage_echoing_the_default_keeps_the_command_off(host: _Host) -> None:
    """A flag outage makes the host echo the supplied default -> `False` -> no reply."""
    host.flag_value = None
    assert _run(transform(_event("!roll"))) is None
    assert host.log_calls == []


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `wit_world` binding at all -> `feature_enabled` returns its default (`False`)."""
    monkeypatch.delitem(sys.modules, "wit_world", raising=False)
    monkeypatch.setitem(sys.modules, "wit_world", None)  # makes `import wit_world` raise
    assert _run(transform(_event("!roll"))) is None


def test_flag_off_consumes_no_randomness_and_logs_nothing(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.flag_value = False

    def _boom(*_a: Any) -> int:
        raise AssertionError("randint must not run while the flag is off")

    monkeypatch.setattr(app.random, "randint", _boom)
    assert _run(transform(_event("!roll 2d6"))) is None
    assert host.log_calls == []


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": 42}, {"text": ["!roll"]}])
def test_non_string_text_payloads_are_ignored(payload: dict[str, Any], host: _Host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=None,
        payload=payload,
        occurred_at="",
    )
    assert _run(transform(event)) is None
    assert host.flag_calls == []


def test_transform_preserves_origin_platform_and_channel(host: _Host) -> None:
    event = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="a",
        payload={"text": "!roll", "channel_id": "C9"},
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
    "text",
    [
        f"!roll 2d{CANARY}",  # malformed spec holding the canary
        f"!roll {CANARY}",
        "!roll 2d6",
        "!roll",
    ],
)
def test_logs_never_contain_user_typed_text_or_actor(text: str, host: _Host) -> None:
    """PII-free-log regression: only static strings + platform may reach the log sink."""
    _run(transform(_event(text)))
    _run(dispatch(_envelope("twitch", f"reply {CANARY}"), {}, http_client=None))

    assert host.log_calls, "expected at least one log line (denominator must be non-zero)"
    for _level, message, fields_json in host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()
        assert set(json.loads(fields_json)) <= {"platform"}


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
    """`__slots__` guards against attribute typos on the result shape."""
    result = DispatchResult(transport="twitch", detail="relayed")
    with pytest.raises(AttributeError):
        result.bogus = 1  # type: ignore[attr-defined]


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
