"""Host-native tests for the `countdown` bundle's stateful `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, and `bundles/python/first/tests/test_app.py` for the
shared `FakeKvHost` composition pattern this bundle's suite follows directly.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from waddle_sdk import community_kv
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import validate_key
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _TARGET_KEY,
    _USAGE,
    _USAGE_SET,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_NO_TARGET = "No countdown set -- use !countdown set <time> [label] to start one."


@dataclass
class _FakeHost:
    """Composes the shared `FakeKvHost` with hand-rolled `flags`/`relay`/`log`/`clock` stubs."""

    kv: FakeKvHost
    relay_calls: list[tuple[str, str]] = field(default_factory=list)
    log_calls: list[tuple[Any, str, str]] = field(default_factory=list)
    now_ms: int = 1_700_000_000_000  # 2023-11-14T22:13:20Z -- arbitrary, fixed epoch for tests
    flag_enabled: bool = True

    @property
    def store(self) -> dict[str, bytes]:
        """The shared kv fake's in-memory store."""
        return self.kv.store

    @property
    def kv_calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        """The shared kv fake's recorded host calls."""
        return self.kv.calls

    def advance(self, seconds: int) -> None:
        """Move the fake clock forward by whole seconds."""
        self.now_ms += seconds * 1000


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    """Install the shared `kv` fake, then extend the same fake `wit_world` with the rest."""
    kv_host = install_fake_kv_host(monkeypatch)
    state = _FakeHost(kv=kv_host)

    wit_world = sys.modules["wit_world"]
    wit_world.imports.flags = types.SimpleNamespace(
        enabled=lambda key, default_value: state.flag_enabled
    )
    wit_world.imports.relay = types.SimpleNamespace(
        push=lambda provider, msg: state.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: state.log_calls.append((lvl, msg, fields_json)),
    )
    wit_world.imports.clock = types.SimpleNamespace(
        now_millis=lambda: state.now_ms,
        now_rfc3339=lambda: "2026-10-07T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    return state


def _sample_event(
    text: str, *, channel_id: str | None = "12345", actor: str | None = "viewer-1"
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-07T00:00:00.000Z",
    )


def _envelope(
    action: str,
    *,
    community: str | None = "comm-1",
    channel_id: str | None = "12345",
    arg: str | None = None,
    platform: str = "twitch",
) -> StageEnvelope:
    payload: dict[str, Any] = {"action": action, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.countdown",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-10-07T00:00:00.000Z",
        ),
        ts="2026-10-07T00:00:00.000Z",
    )


def _relay_text(fake_host: _FakeHost, index: int = -1) -> str:
    _provider, message_json = fake_host.relay_calls[index]
    text = json.loads(message_json)["text"]
    assert isinstance(text, str)
    return text


def _scoped(key: str, *, community: str = "comm-1") -> str:
    """The real `c.<community>.<key>` store key -- mirrors `community_kv._scoped_key`."""
    scoped = community_kv._scoped_key(community, key)
    assert isinstance(scoped, str)
    return scoped


# ---------------------------------------------------------------------------
# transform() parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!countdown", "!COUNTDOWN", "  !countdown  "])
def test_bare_countdown_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == "report"


def test_set_with_duration_and_label_is_forwarded_verbatim(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!countdown set 3d2h New Year")))
    assert result is not None
    assert result.payload["action"] == "set"
    assert result.payload["arg"] == "3d2h New Year"


def test_set_with_no_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!countdown set")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE_SET


def test_reset_parses_to_clear_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!countdown reset")))
    assert result is not None
    assert result.payload["action"] == "clear"


def test_reset_with_trailing_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!countdown reset now")))
    assert result is not None
    assert result.payload["action"] == "usage"


def test_valid_grammar_verb_with_no_meaning_for_countdown_returns_usage(
    fake_host: _FakeHost,
) -> None:
    """`list` is a real grammar verb (AUTHORING.md), just not one `!countdown` gives meaning to."""
    result = _run(transform(_sample_event("!countdown list")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_unknown_option_returns_usage_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!countdown bogus")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert isinstance(result.payload["arg"], str)


@pytest.mark.parametrize("text", ["!countdowns", "countdown", "!countdowned", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!countdown"))) is None
    assert _run(transform(_sample_event("!countdown set 1d"))) is None


# ---------------------------------------------------------------------------
# set: relative duration / ISO-8601 / label handling
# ---------------------------------------------------------------------------


def test_set_relative_duration_reports_remaining_time(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="2d3h14m New Year"), {}, http_client=None))
    assert result.detail == "set"
    text = _relay_text(fake_host)
    assert "2d 3h 14m until New Year" in text


def test_set_without_label_uses_default_label_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="1h"), {}, http_client=None))
    assert result.detail == "set"
    assert "until the countdown" in _relay_text(fake_host)


def test_set_iso_timestamp_in_the_future(fake_host: _FakeHost) -> None:
    # fake now = 1_700_000_000_000 ms = 2023-11-14T22:13:20Z; pick a later instant.
    result = _run(
        dispatch(_envelope("set", arg="2023-11-15T00:13:20Z Launch"), {}, http_client=None)
    )
    assert result.detail == "set"
    text = _relay_text(fake_host)
    assert "until Launch" in text
    assert "2h" in text


def test_set_iso_timestamp_without_timezone_is_treated_as_utc(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="2023-11-15T00:13:20"), {}, http_client=None))
    assert result.detail == "set"
    assert "2h" in _relay_text(fake_host)


def test_set_invalid_time_token_replies_with_error_and_does_not_raise(
    fake_host: _FakeHost,
) -> None:
    result = _run(dispatch(_envelope("set", arg="soonish Launch"), {}, http_client=None))
    assert result.detail == "set"
    text = _relay_text(fake_host)
    assert "not a valid" in text
    # invalid input never writes a target
    assert _scoped(_TARGET_KEY) not in fake_host.store


def test_set_past_time_does_not_crash_and_says_so(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_envelope("set", arg="2023-11-14T20:00:00Z Already gone"), {}, http_client=None)
    )
    assert result.detail == "set"
    assert "already happened" in _relay_text(fake_host)
    # the target IS still stored even though it's already in the past
    assert _scoped(_TARGET_KEY) in fake_host.store


def test_dispatch_set_missing_forwarded_arg_raises(fake_host: _FakeHost) -> None:
    envelope = _envelope("set")
    with pytest.raises(ValueError, match="missing its forwarded arg"):
        _run(dispatch(envelope, {}, http_client=None))


# ---------------------------------------------------------------------------
# report: pure reads, never mutate
# ---------------------------------------------------------------------------


def test_report_before_any_set(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert result.detail == "report"
    assert _relay_text(fake_host) == _NO_TARGET
    assert not any(op in ("set", "increment") for op, *_ in fake_host.kv_calls)


def test_report_after_set_shows_remaining_time(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1d Birthday"), {}, http_client=None))
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert result.detail == "report"
    assert "until Birthday" in _relay_text(fake_host)
    assert "1d" in _relay_text(fake_host)


def test_report_after_target_passed_shows_elapsed_time(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1h Meeting"), {}, http_client=None))
    fake_host.advance(2 * 3600)  # 2 hours later -- target is 1 hour in the past now

    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert result.detail == "report"
    text = _relay_text(fake_host)
    assert "Meeting happened" in text
    assert "ago" in text


# ---------------------------------------------------------------------------
# reset/clear: deletes the target early
# ---------------------------------------------------------------------------


def test_clear_removes_target_and_report_reflects_it(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1d"), {}, http_client=None))
    result = _run(dispatch(_envelope("clear"), {}, http_client=None))
    assert result.detail == "clear"
    assert "cleared" in _relay_text(fake_host)

    report_result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert report_result.detail == "report"
    assert _relay_text(fake_host) == _NO_TARGET


def test_clear_when_nothing_set_is_a_no_op_reply(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("clear"), {}, http_client=None))
    assert result.detail == "clear"
    assert "cleared" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_usage_action_relays_the_forwarded_arg(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("usage", arg="custom usage text"), {}, http_client=None))
    assert result.detail == "usage"
    assert _relay_text(fake_host) == "custom usage text"


def test_usage_action_falls_back_to_default_usage_when_no_arg(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert _relay_text(fake_host) == _USAGE


# ---------------------------------------------------------------------------
# community / channel_id scoping
# ---------------------------------------------------------------------------


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("report", channel_id=None), {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("report", community=None), {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_unrecognized_action(fake_host: _FakeHost) -> None:
    envelope = _envelope("report")
    envelope.event.payload["action"] = "self-destruct"
    with pytest.raises(ValueError, match="unrecognized countdown action"):
        _run(dispatch(envelope, {}, http_client=None))


def test_different_communities_never_share_countdown_state(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", community="comm-1", arg="1d Alpha"), {}, http_client=None))
    result = _run(
        dispatch(_envelope("report", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "report"
    assert _relay_text(fake_host) == _NO_TARGET


# ---------------------------------------------------------------------------
# kv failure handling: fail loud, never silent
# ---------------------------------------------------------------------------


class _HostKvError(Exception):
    """Shaped like the generated WIT `Err` (`.value` holds the error union)."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


async def _raise_err(*_args: Any, **_kwargs: Any) -> Any:
    raise _HostKvError("backend")


def test_kv_failure_on_set_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="countdown kv set failed"):
        _run(dispatch(_envelope("set", arg="1d"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert any(msg == "countdown.kv_error" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_failure_on_report_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="countdown kv get failed"):
        _run(dispatch(_envelope("report"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_clear_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "delete", _raise_err)
    with pytest.raises(RuntimeError, match="countdown kv delete failed"):
        _run(dispatch(_envelope("clear"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# corrupt state: self-heals, never crashes
# ---------------------------------------------------------------------------


def test_corrupt_target_json_is_treated_as_no_target(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TARGET_KEY)] = b"not json"
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert result.detail == "report"
    assert _relay_text(fake_host) == _NO_TARGET
    assert any(msg == "countdown.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_target_missing_required_field_is_treated_as_no_target(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TARGET_KEY)] = json.dumps({"label": "oops"}).encode("utf-8")
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert _relay_text(fake_host) == _NO_TARGET
    assert result.detail == "report"


def test_target_with_non_integer_target_ms_is_treated_as_no_target(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TARGET_KEY)] = json.dumps(
        {"target_ms": "not-an-int", "label": "oops"}
    ).encode("utf-8")
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert _relay_text(fake_host) == _NO_TARGET
    assert result.detail == "report"
    assert any(msg == "countdown.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_target_with_non_string_label_falls_back_to_default_label(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_TARGET_KEY)] = json.dumps(
        {"target_ms": fake_host.now_ms + 60_000, "label": 42}
    ).encode("utf-8")
    result = _run(dispatch(_envelope("report"), {}, http_client=None))
    assert result.detail == "report"
    assert "until the countdown" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# kv key charset -- regression: gh-631, colon-free always
# ---------------------------------------------------------------------------


def test_kv_key_satisfies_host_guest_key_charset() -> None:
    validate_key(_TARGET_KEY)
    assert ":" not in _TARGET_KEY


# ---------------------------------------------------------------------------
# duration parsing / formatting edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arg", "expected_fragment"),
    [
        ("30s Soon", "until Soon"),
        ("5m Soon", "until Soon"),
        ("90m Soon", "1h 30m until Soon"),
    ],
)
def test_set_duration_unit_combinations(
    arg: str, expected_fragment: str, fake_host: _FakeHost
) -> None:
    result = _run(dispatch(_envelope("set", arg=arg), {}, http_client=None))
    assert result.detail == "set"
    assert expected_fragment in _relay_text(fake_host)


def test_set_empty_duration_token_is_invalid(fake_host: _FakeHost) -> None:
    """A token with no d/h/m/s suffix at all (empty match) is rejected, not a 0s countdown."""
    result = _run(dispatch(_envelope("set", arg="xyz Label"), {}, http_client=None))
    assert result.detail == "set"
    assert "not a valid" in _relay_text(fake_host)


def test_set_leading_whitespace_yields_empty_duration_token_is_invalid(
    fake_host: _FakeHost,
) -> None:
    """A leading space in the forwarded arg yields an empty time token -- still rejected."""
    result = _run(dispatch(_envelope("set", arg=" Label"), {}, http_client=None))
    assert result.detail == "set"
    assert "not a valid" in _relay_text(fake_host)
    assert _scoped(_TARGET_KEY) not in fake_host.store
