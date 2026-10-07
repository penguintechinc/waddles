"""Host-native tests for the `first` bundle's stateful `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors. Unlike `count`/`lurk` (which predate it and still
hand-roll their own in-memory `kv` fake), this suite uses the SHARED, charset-enforcing
`waddle_sdk.testing.install_fake_kv_host` fake for `kv` -- composed here with minimal
hand-rolled stubs for `flags`/`relay`/`log`/`clock` (no shared fake exists yet for those).
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
    _LEADERBOARD_REGISTRY_KEY,
    _PERMISSION_DENIED_MSG,
    _USAGE,
    _attempts_key,
    _display_handle,
    _pseudonym,
    _winner_key,
    _wins_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


_NO_WINNER_TODAY = "No one has claimed first today yet -- be the first with !first!"


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

    def advance_days(self, days: int) -> None:
        """Move the fake clock forward by whole days -- simulates the UTC date rolling over."""
        self.now_ms += days * 24 * 60 * 60 * 1000


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
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    return state


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
    actor: str | None = "viewer-1",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> PlatformEvent:
    payload: dict[str, Any] = {"text": text, "channel_id": channel_id}
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _envelope(
    action: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    channel_id: str | None = "12345",
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    platform: str = "twitch",
) -> StageEnvelope:
    payload: dict[str, Any] = {"action": action, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.first",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
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


@pytest.mark.parametrize("text", ["!first", "!FIRST", "  !first  "])
def test_bare_first_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == "claim"


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [
        ("!first list", "list"),
        ("!first LIST", "list"),
        ("!first leaderboard", "leaderboard"),
        ("!first reset", "reset"),
    ],
)
def test_transform_parses_known_verbs_and_sub_module(
    text: str, expected_action: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == expected_action


def test_valid_grammar_verb_with_no_meaning_for_first_returns_usage(fake_host: _FakeHost) -> None:
    """`set` is a real grammar verb (AUTHORING.md), just not one `!first` assigns any meaning to."""
    result = _run(transform(_sample_event("!first set x")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_unknown_subcommand_returns_usage_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!first bogus")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert isinstance(result.payload["arg"], str)


def test_leaderboard_with_trailing_verb_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!first leaderboard list")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!first reset", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!first reset")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


@pytest.mark.parametrize("text", ["!firstly", "first", "!firsted", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!first"))) is None
    assert _run(transform(_sample_event("!first list"))) is None


# ---------------------------------------------------------------------------
# claim: first-wins / subsequent-callers
# ---------------------------------------------------------------------------


def test_first_claim_wins_and_is_attempt_one(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    assert result.detail == "claim"
    text = _relay_text(fake_host)
    assert "viewer-1 claimed first today" in text
    assert "win #1" in text


def test_second_caller_gets_nth_reply_and_does_not_overwrite_winner(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    result = _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))

    assert result.detail == "claim"
    text = _relay_text(fake_host)
    assert "already claimed today" in text
    assert "viewer-2, you're #2 to try" in text
    # the winner pseudonym itself is never overwritten by a later, non-winning caller
    stored = fake_host.store[_scoped(_winner_key(_current_period(fake_host)))]
    assert stored.decode() == _pseudonym("viewer-1")


def test_third_caller_is_attempt_three(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))
    _run(dispatch(_envelope("claim", actor="viewer-3"), {}, http_client=None))
    assert "#3 to try" in _relay_text(fake_host)


def test_repeat_claim_by_the_same_actor_still_counts_as_a_try(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    assert "#2 to try" in _relay_text(fake_host)


def test_claim_pseudonymizes_the_actor_everywhere(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    for key in fake_host.store:
        assert "viewer-1" not in key
    for value in fake_host.store.values():
        assert b"viewer-1" not in value
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json


# ---------------------------------------------------------------------------
# list / leaderboard: pure reads, never mutate
# ---------------------------------------------------------------------------


def test_list_before_any_claim(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_WINNER_TODAY
    assert fake_host.kv_calls  # reads happened
    assert not any(op == "set" or op == "increment" for op, *_ in fake_host.kv_calls)


def test_list_after_claim_reports_winner_and_attempt_count_without_mutating(
    fake_host: _FakeHost,
) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))
    calls_before = list(fake_host.kv_calls)

    result = _run(dispatch(_envelope("list"), {}, http_client=None))

    assert result.detail == "list"
    handle = _display_handle(_pseudonym("viewer-1"))
    assert _relay_text(fake_host) == f"First today: {handle}. 2 tries so far."
    # list is a pure read: no new set/increment calls beyond what the two claims already made
    assert not any(op in ("set", "increment") for op, *_ in fake_host.kv_calls[len(calls_before) :])


def test_list_singular_try_wording(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_envelope("list"), {}, http_client=None))
    assert _relay_text(fake_host).endswith("1 try so far.")


def test_leaderboard_empty(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    assert _relay_text(fake_host) == "No one has claimed first yet -- be the first with !first!"


def test_leaderboard_ranks_by_wins_descending(fake_host: _FakeHost) -> None:
    # viewer-1 wins today...
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    # ...and again the next day (a fresh window -- viewer-1 claims first again)
    fake_host.advance_days(1)
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    # viewer-2 only ever manages one win, on a third day (viewer-1 already won days 1 and 2)
    fake_host.advance_days(1)
    _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))

    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    text = _relay_text(fake_host)
    handle1 = _display_handle(_pseudonym("viewer-1"))
    handle2 = _display_handle(_pseudonym("viewer-2"))
    assert text == f"First leaderboard: {handle1} (2), {handle2} (1)"


# ---------------------------------------------------------------------------
# per-day window rollover -- no scheduler, just the date token in the key
# ---------------------------------------------------------------------------


def test_new_day_is_a_fresh_window_automatically(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    fake_host.advance_days(1)

    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert _relay_text(fake_host) == _NO_WINNER_TODAY
    assert result.detail == "list"

    claim_result = _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))
    assert claim_result.detail == "claim"
    assert "viewer-2 claimed first today" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# reset: mod/broadcaster only, current day only
# ---------------------------------------------------------------------------


def test_reset_clears_current_day_and_allows_a_fresh_claim(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    result = _run(dispatch(_envelope("reset", is_mod=True), {}, http_client=None))
    assert result.detail == "reset"
    assert "reset for today" in _relay_text(fake_host)

    claim_result = _run(dispatch(_envelope("claim", actor="viewer-2"), {}, http_client=None))
    text = _relay_text(fake_host)
    assert claim_result.detail == "claim"
    assert "viewer-2 claimed first today" in text
    assert "win #1" in text  # attempts counter reset too, not just the winner


def test_reset_never_touches_all_time_wins_or_leaderboard(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    _run(dispatch(_envelope("reset", is_mod=True), {}, http_client=None))

    assert fake_host.store[_scoped(_wins_key(_pseudonym("viewer-1")))] == b"1"
    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    handle = _display_handle(_pseudonym("viewer-1"))
    assert _relay_text(fake_host) == f"First leaderboard: {handle} (1)"
    assert result.detail == "leaderboard"


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(False, False), (None, None)],
)
def test_reset_denied_without_mod_or_broadcaster_badge(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    result = _run(
        dispatch(
            _envelope("reset", is_mod=is_mod, is_broadcaster=is_broadcaster), {}, http_client=None
        )
    )
    assert result.detail == "reset:denied"
    assert _relay_text(fake_host) == _PERMISSION_DENIED_MSG
    # the winner from before the denied reset attempt is still intact
    list_result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "First today" in _relay_text(fake_host)
    assert list_result.detail == "list"


@pytest.mark.parametrize(("is_mod", "is_broadcaster"), [(True, None), (None, True), (True, True)])
def test_reset_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _envelope("reset", is_mod=is_mod, is_broadcaster=is_broadcaster), {}, http_client=None
        )
    )
    assert result.detail == "reset"


def test_reset_denied_on_discord_where_no_badge_fields_exist_at_all(fake_host: _FakeHost) -> None:
    """No is_mod/is_broadcaster at all must deny, never implicitly allow."""
    result = _run(dispatch(_envelope("reset", platform="discord"), {}, http_client=None))
    assert result.detail == "reset:denied"


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
        _run(dispatch(_envelope("claim", channel_id=None), {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("claim", community=None), {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_unrecognized_action(fake_host: _FakeHost) -> None:
    envelope = _envelope("claim")
    envelope.event.payload["action"] = "self-destruct"
    with pytest.raises(ValueError, match="unrecognized first action"):
        _run(dispatch(envelope, {}, http_client=None))


def test_different_communities_never_share_first_state(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", community="comm-1", actor="viewer-1"), {}, http_client=None))
    result = _run(
        dispatch(_envelope("claim", community="comm-2", actor="viewer-2"), {}, http_client=None)
    )
    text = _relay_text(fake_host)
    assert "viewer-2 claimed first today" in text
    assert result.detail == "claim"


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


def test_kv_failure_on_claim_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "increment", _raise_err)
    with pytest.raises(RuntimeError, match="first kv increment failed"):
        _run(dispatch(_envelope("claim"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert any(msg == "first.kv_error" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_failure_on_claim_set_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="first kv set failed"):
        _run(dispatch(_envelope("claim"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_list_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="first kv get failed"):
        _run(dispatch(_envelope("list"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_reset_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "delete", _raise_err)
    with pytest.raises(RuntimeError, match="first kv delete failed"):
        _run(dispatch(_envelope("reset", is_mod=True), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# corrupt state: self-heals, never crashes
# ---------------------------------------------------------------------------


def test_corrupt_attempts_counter_reads_as_zero(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("claim", actor="viewer-1"), {}, http_client=None))
    period = _current_period(fake_host)
    fake_host.store[_scoped(_attempts_key(period))] = b"not-a-number"

    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert "0 tries so far" in _relay_text(fake_host)
    assert any(msg == "first.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_corrupt_winner_bytes_is_treated_as_no_winner(fake_host: _FakeHost) -> None:
    period = _current_period(fake_host)
    fake_host.store[_scoped(_winner_key(period))] = b"\xff\xfe\x00bad"

    result = _run(dispatch(_envelope("list"), {}, http_client=None))
    assert result.detail == "list"
    assert _relay_text(fake_host) == _NO_WINNER_TODAY


def test_corrupt_leaderboard_registry_is_treated_as_empty(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_LEADERBOARD_REGISTRY_KEY)] = b"not json"

    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    assert _relay_text(fake_host) == "No one has claimed first yet -- be the first with !first!"
    assert any(msg == "first.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_leaderboard_registry_not_a_list_of_strings_is_treated_as_empty(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped(_LEADERBOARD_REGISTRY_KEY)] = json.dumps([1, 2, 3]).encode("utf-8")

    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    assert _relay_text(fake_host) == "No one has claimed first yet -- be the first with !first!"
    assert result.detail == "leaderboard"
    assert any(msg == "first.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_leaderboard_skips_registered_pseudonyms_with_zero_or_missing_wins(
    fake_host: _FakeHost,
) -> None:
    pseudonym = _pseudonym("ghost")
    fake_host.store[_scoped(_LEADERBOARD_REGISTRY_KEY)] = json.dumps([pseudonym]).encode("utf-8")
    # no wins key written at all for this pseudonym -- _parse_int defaults it to 0

    result = _run(dispatch(_envelope("leaderboard"), {}, http_client=None))
    assert _relay_text(fake_host) == "No one has claimed first yet -- be the first with !first!"
    assert result.detail == "leaderboard"


# ---------------------------------------------------------------------------
# kv key charset -- regression: gh-631, colon-free always
# ---------------------------------------------------------------------------


def test_kv_key_builders_satisfy_host_guest_key_charset() -> None:
    period = "20261005"
    pseudonym = _pseudonym("viewer-1")
    keys = (
        _winner_key(period),
        _attempts_key(period),
        _wins_key(pseudonym),
        _LEADERBOARD_REGISTRY_KEY,
    )
    for key in keys:
        validate_key(key)
        assert ":" not in key


def _current_period(fake_host: _FakeHost) -> str:
    """Recompute today's period token from the fake clock -- avoids hardcoding a date in tests."""
    from datetime import UTC, datetime

    now = datetime.fromtimestamp(fake_host.now_ms / 1000, tz=UTC)
    return now.strftime("%Y%m%d")
