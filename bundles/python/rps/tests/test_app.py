"""Host-native tests for the `rps` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/duel/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import types
from typing import Any

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _NO_RECORD_YET,
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    _caller_role_signal,
    _format_duration,
    _lastplay_key,
    _losses_key,
    _normalize_choice,
    _pseudonym,
    _resolve_command,
    _resolve_outcome,
    _ties_key,
    _wins_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(identity: str | None) -> str:
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.store` is keyed by `community_kv`'s own `c:<community>:<key>` prefix."""
    return _scoped_key(community, key)


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
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
        actor="viewer-1",
        payload=payload,
        occurred_at="2026-10-05T00:00:00.000Z",
    )


class _FakeHost:
    """Fake WIT host: flags/kv/relay/log/clock, with a real in-memory kv store + TTL ignored."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.kv_calls: list[tuple[Any, ...]] = []
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.now_ms = 1_700_000_000_000

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    host = _FakeHost()
    _install(monkeypatch, host)
    return host


def _install(
    monkeypatch: pytest.MonkeyPatch,
    host: _FakeHost,
    *,
    kv_get_raises: Exception | None = None,
    kv_set_raises: Exception | None = None,
    kv_increment_raises: Exception | None = None,
    flag_enabled: bool = True,
) -> None:
    def kv_get(key: str) -> bytes | None:
        host.kv_calls.append(("get", key))
        if kv_get_raises is not None:
            raise kv_get_raises
        return host.store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        host.kv_calls.append(("set", key, bytes(value), ttl))
        if kv_set_raises is not None:
            raise kv_set_raises
        host.store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        host.kv_calls.append(("delete", key))
        host.store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        host.kv_calls.append(("increment", key, delta, ttl))
        if kv_increment_raises is not None:
            raise kv_increment_raises
        current = int(host.store.get(key, b"0").decode())
        new_value = current + delta
        host.store[key] = str(new_value).encode()
        return new_value

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_enabled)
    kv_mod = types.SimpleNamespace(get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment)
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: host.now_ms,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    choice: str | None = None,
    arg: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if choice is not None:
        payload["choice"] = choice
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.rps",
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


def _force_bot_choice(monkeypatch: pytest.MonkeyPatch, choice: str) -> None:
    """Seed the bot's move RNG deterministically -- see `app._handle_play`'s own `random.choice`."""
    monkeypatch.setattr("random.choice", lambda seq: choice)


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!rps", "!RPS", "  !rps  "])
def test_bare_rps_with_no_move_is_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize(
    "text", ["!rps rock", "!RPS rock", "!rps r", "!rps paper", "!rps p", "!rps scissors", "!rps s"]
)
def test_rps_with_move_matches_as_play(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "play"
    assert result.payload["choice"] == text.split()[1]


def test_rps_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rps list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_rps_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rps list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_rps_set_cooldown_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rps set cooldown 45")))
    assert result is not None
    assert result.payload["command"] == "config_set_cooldown"
    assert result.payload["arg"] == "cooldown 45"


@pytest.mark.parametrize("text", ["!rps enable ai", "!rps reset", "!rps add", "!rps rock solid"])
def test_unsupported_verbs_and_multiword_moves_reply_usage(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!rpsx", "rps", "!rpser", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, flag_enabled=False)
    assert _run(transform(_sample_event("!rps rock"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rps rock", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rps rock")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() / _normalize_choice() / _resolve_outcome() direct unit coverage ---------


def test_resolve_command_bare_is_usage() -> None:
    assert _resolve_command("!rps") == ("usage", None)


def test_resolve_command_single_token_is_play() -> None:
    assert _resolve_command("!rps rock") == ("play", "rock")


def test_resolve_command_list() -> None:
    assert _resolve_command("!rps list") == ("list", None)


def test_resolve_command_set_cooldown() -> None:
    assert _resolve_command("!rps set cooldown 10") == ("config_set_cooldown", "cooldown 10")


def test_resolve_command_two_token_non_verb_is_usage() -> None:
    assert _resolve_command("!rps rock solid") == ("usage", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("rock", "rock"),
        ("ROCK", "rock"),
        ("r", "rock"),
        ("R", "rock"),
        ("paper", "paper"),
        ("p", "paper"),
        ("scissors", "scissors"),
        ("s", "scissors"),
        (" rock ", "rock"),
    ],
)
def test_normalize_choice_accepts_known_moves_and_shorthand(raw: str, expected: str) -> None:
    assert _normalize_choice(raw) == expected


@pytest.mark.parametrize("raw", ["lizard", "spock", "", "rocks", "@", "123"])
def test_normalize_choice_rejects_unknown_input(raw: str) -> None:
    assert _normalize_choice(raw) is None


@pytest.mark.parametrize(
    ("player", "bot", "expected"),
    [
        ("rock", "scissors", "win"),
        ("paper", "rock", "win"),
        ("scissors", "paper", "win"),
        ("rock", "paper", "lose"),
        ("paper", "scissors", "lose"),
        ("scissors", "rock", "lose"),
        ("rock", "rock", "tie"),
        ("paper", "paper", "tie"),
        ("scissors", "scissors", "tie"),
    ],
)
def test_resolve_outcome_covers_every_combination(player: str, bot: str, expected: str) -> None:
    assert _resolve_outcome(player, bot) == expected


# -- play: invalid move -----------------------------------------------------------


def test_invalid_move_is_rejected_gracefully(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "play", choice="lizard")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "play:invalid_choice"
    assert fake_host.kv_calls == []
    provider, message_json = fake_host.relay_calls[-1]
    assert _USAGE in message_json


def test_dispatch_play_with_missing_choice_is_invalid(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "play", choice=None)
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "play:invalid_choice"
    assert fake_host.kv_calls == []


# -- play: cooldown / outcome / W-L-T tracking --------------------------------


def test_first_play_wins_persists_state_and_replies(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    envelope = _sample_envelope("twitch", "play", choice="rock")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "play:win"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_lastplay_key(pseudonym)) in fake_host.store
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "viewer-1 wins" in message_json


def test_play_loses_updates_losses(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_bot_choice(monkeypatch, "paper")
    envelope = _sample_envelope("twitch", "play", choice="rock")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "play:lose"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_losses_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "I win" in message_json


def test_play_ties_updates_ties(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_bot_choice(monkeypatch, "rock")
    envelope = _sample_envelope("twitch", "play", choice="rock")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "play:tie"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_ties_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "tie" in message_json


def test_shorthand_move_resolves_same_as_full_name(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    envelope = _sample_envelope("twitch", "play", choice="r")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "play:win"


def test_second_play_within_cooldown_is_rejected(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(1)
    envelope = _sample_envelope("twitch", "play", choice="paper")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "play:cooldown"
    pseudonym = _expected_pseudonym("viewer-1")
    # not incremented again
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 1
    assert _scoped(_wins_key(pseudonym)) in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in message_json


def test_play_allowed_again_after_cooldown_elapses(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    assert result.detail == "play:win"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 2


def test_invalid_move_does_not_consume_cooldown(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="lizard"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    assert result.detail == "play:win"


def test_cooldown_corrupt_state_is_treated_as_no_cooldown(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_lastplay_key(pseudonym))] = b"not-a-timestamp"
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    assert result.detail in ("play:win", "play:lose", "play:tie")
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.cooldown_state_corrupt"]
    assert corrupt_logs


def test_different_communities_have_independent_cooldowns(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(
        dispatch(
            _sample_envelope("twitch", "play", choice="rock", community="comm-1"),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(
            _sample_envelope("twitch", "play", choice="rock", community="comm-2"),
            {},
            http_client=None,
        )
    )
    assert result.detail == "play:win"


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "play", choice="rock", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.rps",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "play", "choice": "rock", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized rps command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- !rps list -------------------------------------------------------------


def test_list_before_any_play(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert _NO_RECORD_YET in message_json


def test_list_after_a_win_a_loss_and_a_tie(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    _force_bot_choice(monkeypatch, "paper")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    _force_bot_choice(monkeypatch, "rock")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert "1W - 1L - 1T" in message_json


def test_list_corrupt_wins_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_wins_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert _NO_RECORD_YET in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.wins_corrupt"]
    assert corrupt_logs
    assert result.detail == "list"


def test_list_corrupt_losses_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_wins_key(pseudonym))] = b"2"
    fake_host.store[_scoped(_losses_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "2W - 0L - 0T" in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.losses_corrupt"]
    assert corrupt_logs
    assert result.detail == "list"


def test_list_corrupt_ties_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_wins_key(pseudonym))] = b"1"
    fake_host.store[_scoped(_losses_key(pseudonym))] = b"1"
    fake_host.store[_scoped(_ties_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "1W - 1L - 0T" in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.ties_corrupt"]
    assert corrupt_logs
    assert result.detail == "list"


# -- admin config: cooldown -----------------------------------------------------


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(True, None), (None, True), (True, True)],
)
def test_set_cooldown_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope(
        "twitch",
        "config_set_cooldown",
        arg="cooldown 45",
        is_mod=is_mod,
        is_broadcaster=is_broadcaster,
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown"
    assert fake_host.store[_scoped("rps.config.cooldown")] == b"45"
    provider, message_json = fake_host.relay_calls[-1]
    assert "45" in message_json


def test_set_cooldown_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 45", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    assert _scoped("rps.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in message_json


def test_set_cooldown_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "config_set_cooldown", arg="cooldown 45")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.config_denied"]
    assert denial_logs


@pytest.mark.parametrize(
    ("arg", "expected_fragment"),
    [
        (None, "Usage"),
        ("", "Usage"),
        ("badkey 45", "Usage"),
        ("cooldown", "Usage"),
        ("cooldown notanumber", "whole number"),
        (f"cooldown {MIN_COOLDOWN_SECONDS - 1}", "must be between"),
        (f"cooldown {MAX_COOLDOWN_SECONDS + 1}", "must be between"),
    ],
)
def test_set_cooldown_rejects_bad_input(
    arg: str | None, expected_fragment: str, fake_host: _FakeHost
) -> None:
    envelope = _sample_envelope("twitch", "config_set_cooldown", arg=arg, is_mod=True)
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown"
    assert _scoped("rps.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert expected_fragment in message_json


@pytest.mark.parametrize(
    "seconds", [MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS]
)
def test_set_cooldown_accepts_boundary_values(seconds: int, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg=f"cooldown {seconds}", is_mod=True
    )
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "config_set_cooldown"
    assert fake_host.store[_scoped("rps.config.cooldown")] == str(seconds).encode()


def test_zero_cooldown_allows_back_to_back_plays(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _sample_envelope("twitch", "config_set_cooldown", arg="cooldown 0", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))

    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 2
    assert result.detail == "play:win"


def test_configured_cooldown_is_honored_on_next_play(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = _sample_envelope("twitch", "config_set_cooldown", arg="cooldown 5", is_mod=True)
    _run(dispatch(envelope, {}, http_client=None))

    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(6)
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 2
    assert result.detail == "play:win"


def test_cooldown_config_corrupt_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped("rps.config.cooldown")] = b"not-a-number"
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(1)
    result = _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rps.cooldown_config_corrupt"]
    assert corrupt_logs
    assert result.detail == "play:cooldown"  # default cooldown (10s) still applied


# -- usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert _USAGE in message_json


# -- kv failure: fail loud, never silent ---------------------------------------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_play_replies_and_raises_on_kv_set_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in message_json
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "rps.kv_error"]
    assert error_logs


def test_play_replies_and_raises_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in message_json


def test_play_replies_and_raises_on_kv_increment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_increment_raises=_KvError())
    _force_bot_choice(monkeypatch, "scissors")

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in message_json


# -- PII: never leak the raw actor into kv keys or logs ------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_identity() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_dispatch_never_logs_the_raw_actor(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_bot_choice(monkeypatch, "scissors")
    _run(dispatch(_sample_envelope("twitch", "play", choice="rock"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json


def test_pseudonym_differs_per_identity() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


# -- _format_duration() / _caller_role_signal() unit coverage -------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (1, "1s"), (65, "1m 5s"), (3661, "1h 1m")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert _format_duration(seconds) == expected


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
