"""Host-native tests for the `remind` bundle's stateful `transform`/`dispatch` logic.

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
    _USAGE,
    _USAGE_REMOVE,
    _USAGE_SET,
    _counter_key,
    _items_key,
    _pseudonym,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_NO_REMINDERS = "You have no reminders. Set one with !remind set <duration> <text>."


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
    actor: str | None = "viewer-1",
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
        app_id="waddles.core.example.remind",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
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


@pytest.mark.parametrize("text", ["!remind", "!REMIND", "  !remind  "])
def test_bare_remind_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == "list"


def test_explicit_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind list")))
    assert result is not None
    assert result.payload["action"] == "list"


def test_list_with_trailing_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind list extra")))
    assert result is not None
    assert result.payload["action"] == "usage"


def test_set_with_duration_and_text_is_forwarded_verbatim(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind set 3d2h water the plants")))
    assert result is not None
    assert result.payload["action"] == "set"
    assert result.payload["arg"] == "3d2h water the plants"


def test_set_with_no_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind set")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE_SET


def test_remove_with_id_is_forwarded(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind remove 3")))
    assert result is not None
    assert result.payload["action"] == "remove"
    assert result.payload["arg"] == "3"


def test_remove_with_no_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind remove")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE_REMOVE


def test_remove_with_multi_token_args_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind remove 3 4")))
    assert result is not None
    assert result.payload["action"] == "usage"


def test_valid_grammar_verb_with_no_meaning_for_remind_returns_usage(
    fake_host: _FakeHost,
) -> None:
    """`reset` is a real grammar verb (AUTHORING.md), just not one `!remind` gives meaning to."""
    result = _run(transform(_sample_event("!remind reset")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_unknown_option_returns_usage_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!remind bogus")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert isinstance(result.payload["arg"], str)


@pytest.mark.parametrize("text", ["!reminds", "remind", "!reminded", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!remind"))) is None
    assert _run(transform(_sample_event("!remind set 1d text"))) is None


# ---------------------------------------------------------------------------
# set: valid/invalid duration, missing text, confirmation wording
# ---------------------------------------------------------------------------


def test_set_valid_duration_and_text_confirms_with_id_and_no_delivery_note(
    fake_host: _FakeHost,
) -> None:
    result = _run(dispatch(_envelope("set", arg="3d2h water the plants"), {}, http_client=None))
    assert result.detail == "set"
    text = _relay_text(fake_host)
    assert "#1" in text
    assert "water the plants" in text
    assert "3d 2h" in text
    assert "no auto-delivery yet" in text


def test_set_ids_increment_per_user(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1d first"), {}, http_client=None))
    result = _run(dispatch(_envelope("set", arg="1d second"), {}, http_client=None))
    assert result.detail == "set"
    assert "#2" in _relay_text(fake_host)


def test_set_missing_text_replies_with_usage_and_does_not_store(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="1d"), {}, http_client=None))
    assert result.detail == "set"
    assert "text is required" in _relay_text(fake_host)
    pseudonym = _pseudonym("viewer-1")
    assert _scoped(_items_key(pseudonym)) not in fake_host.store


def test_set_invalid_duration_replies_with_error_and_does_not_store(
    fake_host: _FakeHost,
) -> None:
    result = _run(dispatch(_envelope("set", arg="soonish text here"), {}, http_client=None))
    assert result.detail == "set"
    assert "not a valid positive duration" in _relay_text(fake_host)
    pseudonym = _pseudonym("viewer-1")
    assert _scoped(_items_key(pseudonym)) not in fake_host.store


def test_set_zero_duration_is_invalid(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="0s text"), {}, http_client=None))
    assert result.detail == "set"
    assert "not a valid positive duration" in _relay_text(fake_host)


def test_dispatch_set_missing_forwarded_arg_raises(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="missing its forwarded arg"):
        _run(dispatch(_envelope("set"), {}, http_client=None))


def test_set_leading_whitespace_yields_empty_duration_token_is_invalid(
    fake_host: _FakeHost,
) -> None:
    """A leading space in the forwarded arg yields an empty duration token -- still rejected."""
    result = _run(dispatch(_envelope("set", arg=" text here"), {}, http_client=None))
    assert result.detail == "set"
    assert "not a valid positive duration" in _relay_text(fake_host)
    pseudonym = _pseudonym("viewer-1")
    assert _scoped(_items_key(pseudonym)) not in fake_host.store


def test_set_sub_minute_duration_formats_as_seconds(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="45s quick task"), {}, http_client=None))
    assert result.detail == "set"
    assert "45s" in _relay_text(fake_host)


def test_set_sub_hour_duration_formats_as_minutes_only(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("set", arg="5m quick task"), {}, http_client=None))
    assert result.detail == "set"
    assert "5m" in _relay_text(fake_host)
    assert "0h" not in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# list: pure reads, never mutate, ordering, overdue wording
# ---------------------------------------------------------------------------


def test_list_before_any_set(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_REMINDERS
    assert not any(op in ("set", "increment") for op, *_ in fake_host.kv_calls)


def test_list_after_set_shows_due_in(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="2h plants"), {}, http_client=None))
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    text = _relay_text(fake_host)
    assert "#1" in text
    assert "plants" in text
    assert "due in" in text


def test_list_is_a_pure_read_no_new_writes(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="2h plants"), {}, http_client=None))
    calls_before = list(fake_host.kv_calls)
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert not any(op in ("set", "increment") for op, *_ in fake_host.kv_calls[len(calls_before) :])


def test_list_shows_overdue_as_not_delivered(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1h meeting"), {}, http_client=None))
    fake_host.advance(2 * 3600)  # 2h later -- 1h reminder is now 1h overdue
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    text = _relay_text(fake_host)
    assert "overdue by" in text
    assert "not delivered" in text


def test_list_orders_by_due_time_ascending(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="2d later"), {}, http_client=None))
    _run(dispatch(_envelope("set", arg="1h sooner"), {}, http_client=None))
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    text = _relay_text(fake_host)
    assert text.index("sooner") < text.index("later")


def test_different_users_never_share_reminders(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", actor="viewer-1", arg="1d mine"), {}, http_client=None))
    result = _run(dispatch(_envelope("list", actor="viewer-2"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_REMINDERS


def test_different_communities_never_share_reminders(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _envelope("set", community="comm-1", arg="1d mine"), {}, http_client=None
        )
    )
    result = _run(dispatch(_envelope("list", community="comm-2"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_REMINDERS


# ---------------------------------------------------------------------------
# remove: cancels the caller's own reminder by id
# ---------------------------------------------------------------------------


def test_remove_existing_id_succeeds_and_list_no_longer_shows_it(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1d plants"), {}, http_client=None))
    result = _run(dispatch(_envelope("remove", arg="1"), {}, http_client=None))
    assert result.detail == "remove"
    assert "Reminder #1 removed" in _relay_text(fake_host)

    list_result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert list_result.detail == "list"
    assert _relay_text(fake_host) == _NO_REMINDERS


def test_remove_nonexistent_id_replies_not_found(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("remove", arg="99"), {}, http_client=None))
    assert result.detail == "remove"
    assert "No reminder with id 99" in _relay_text(fake_host)


def test_remove_non_digit_id_replies_invalid(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("remove", arg="abc"), {}, http_client=None))
    assert result.detail == "remove"
    assert "not a valid reminder id" in _relay_text(fake_host)


def test_remove_only_removes_the_matching_id_not_others(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1d first"), {}, http_client=None))
    _run(dispatch(_envelope("set", arg="1d second"), {}, http_client=None))
    _run(dispatch(_envelope("remove", arg="1"), {}, http_client=None))
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    text = _relay_text(fake_host)
    assert "#2" in text
    assert "second" in text
    assert "#1" not in text
    assert result.detail == "list"


def test_dispatch_remove_missing_forwarded_arg_raises(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="missing its forwarded arg"):
        _run(dispatch(_envelope("remove"), {}, http_client=None))


# ---------------------------------------------------------------------------
# storage hygiene: ancient overdue reminders are pruned on the next write
# ---------------------------------------------------------------------------


def test_very_stale_reminder_is_pruned_on_next_set(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("set", arg="1h old"), {}, http_client=None))
    fake_host.advance(31 * 24 * 60 * 60)  # 31 days later -- past the 30-day stale cutoff

    _run(dispatch(_envelope("set", arg="1h new"), {}, http_client=None))
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    text = _relay_text(fake_host)
    assert "new" in text
    assert "old" not in text
    assert result.detail == "list"


def test_very_stale_reminder_still_visible_via_list_pure_read(fake_host: _FakeHost) -> None:
    """`list` never mutates/prunes -- an ancient reminder stays visible until a write path runs."""
    _run(dispatch(_envelope("set", arg="1h old"), {}, http_client=None))
    fake_host.advance(31 * 24 * 60 * 60)
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert "old" in _relay_text(fake_host)


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
        _run(dispatch(_envelope("list", channel_id=None), {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("list", community=None), {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_unrecognized_action(fake_host: _FakeHost) -> None:
    envelope = _envelope("list")
    envelope.event.payload["action"] = "self-destruct"
    with pytest.raises(ValueError, match="unrecognized remind action"):
        _run(dispatch(envelope, {}, http_client=None))


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


def test_kv_failure_on_set_get_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="remind kv get failed"):
        _run(dispatch(_envelope("set", arg="1d text"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert any(msg == "remind.kv_error" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_failure_on_set_increment_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "increment", _raise_err)
    with pytest.raises(RuntimeError, match="remind kv increment failed"):
        _run(dispatch(_envelope("set", arg="1d text"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_set_write_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="remind kv set failed"):
        _run(dispatch(_envelope("set", arg="1d text"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_list_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="remind kv get failed"):
        _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_remove_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(dispatch(_envelope("set", arg="1d text"), {}, http_client=None))
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="remind kv set failed"):
        _run(dispatch(_envelope("remove", arg="1"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# corrupt state: self-heals, never crashes
# ---------------------------------------------------------------------------


def test_corrupt_items_json_is_treated_as_empty(fake_host: _FakeHost) -> None:
    pseudonym = _pseudonym("viewer-1")
    fake_host.store[_scoped(_items_key(pseudonym))] = b"not json"
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_REMINDERS
    assert any(msg == "remind.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_items_not_a_list_of_dicts_is_treated_as_empty(fake_host: _FakeHost) -> None:
    pseudonym = _pseudonym("viewer-1")
    fake_host.store[_scoped(_items_key(pseudonym))] = json.dumps(["oops"]).encode("utf-8")
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert _relay_text(fake_host) == _NO_REMINDERS
    assert result.detail == "list"


# ---------------------------------------------------------------------------
# kv key charset -- regression: gh-631, colon-free always
# ---------------------------------------------------------------------------


def test_kv_key_builders_satisfy_host_guest_key_charset() -> None:
    pseudonym = _pseudonym("viewer-1")
    for key in (_items_key(pseudonym), _counter_key(pseudonym)):
        validate_key(key)
        assert ":" not in key
