"""Host-native tests for the `coinflip` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (extended with a fake `kv`/`clock` import, same shape as
`bundles/python/fish`/`bundles/python/slots`'s own test harness -- `community_kv` is a thin
prefix wrapper over the same `kv` WIT import, so the same fake serves both).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _NOTHING_FLIPPED_YET,
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    SPEC,
    _caller_role_signal,
    _evaluate_flip,
    _flip_coin,
    _flips_key,
    _format_duration,
    _lastflip_key,
    _normalize_alias,
    _pseudonym,
    _resolve_command,
    _wins_key,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(actor: str | None) -> str:
    return hashlib.sha256((actor or "anonymous").encode()).hexdigest()


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
    kv_mod = types.SimpleNamespace(
        get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment
    )
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
    arg: str | None = None,
    call: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if arg is not None:
        payload["arg"] = arg
    if call is not None:
        payload["call"] = call
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.coinflip",
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


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!flip", "!FLIP", "  !flip  ", "!coinflip", "!CoinFlip"])
def test_bare_flip_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "flip"
    assert "call" not in result.payload


@pytest.mark.parametrize(
    ("text", "expected_call"),
    [
        ("!flip heads", "heads"),
        ("!flip tails", "tails"),
        ("!flip HEADS", "heads"),
        ("!coinflip tails", "tails"),
        ("!COINFLIP HEADS", "heads"),
    ],
)
def test_call_flip_matches_and_normalizes_the_call(
    text: str, expected_call: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "flip"
    assert result.payload["call"] == expected_call


def test_flip_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!flip list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_coinflip_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!coinflip list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_flip_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!flip list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_flip_set_cooldown_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!flip set cooldown 5")))
    assert result is not None
    assert result.payload["command"] == "config_set_cooldown"
    assert result.payload["arg"] == "cooldown 5"


@pytest.mark.parametrize(
    "text",
    [
        "!flip enable ai",
        "!flip bogus",
        "!flip add",
        "!flip reset",
        "!flip disable foo",
        "!flip heads list",
        "!flip tails set cooldown 5",
        "!flip heads enable",
        "!flip heads tails",
    ],
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!flipper", "flip", "!fli", "!coin", "hello", ""])
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
    assert _run(transform(_sample_event("!flip"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!flip", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!flip")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _normalize_alias() direct unit coverage -------------------------------------


def test_normalize_alias_bare_coinflip_becomes_bare_flip() -> None:
    assert _normalize_alias("!coinflip", "!coinflip") == "!flip"


def test_normalize_alias_preserves_tail() -> None:
    assert _normalize_alias("!coinflip  heads", "!coinflip") == "!flip heads"


def test_normalize_alias_bare_flip_is_unchanged() -> None:
    assert _normalize_alias("!flip", "!flip") == "!flip"


# -- _resolve_command() direct unit coverage -----------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_flip() -> None:
    parsed = parse_command("!flip", SPEC)
    assert _resolve_command(parsed) == "flip"


@pytest.mark.parametrize("call", ["heads", "tails"])
def test_resolve_command_call_alone_is_flip(call: str) -> None:
    parsed = parse_command(f"!flip {call}", SPEC)
    assert _resolve_command(parsed) == "flip"


def test_resolve_command_call_plus_option_is_usage() -> None:
    parsed = ParsedCommand(command="flip", sub_module="heads", option="list", args=None)
    assert _resolve_command(parsed) == "usage"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="flip", sub_module=None, option="sub", args=None)
    assert _resolve_command(parsed) == "usage"


def test_resolve_command_declares_heads_and_tails_as_sub_modules() -> None:
    spec = CommandSpec(name="flip")
    with pytest.raises(Exception):  # noqa: B017, PT011 -- CommandUsageError, undeclared sub_module
        parse_command("!flip heads", spec)


# -- flip / cooldown state machine ----------------------------------------------


def test_first_bare_flip_persists_state_and_replies(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "flip")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_lastflip_key(pseudonym)) in fake_host.store
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 1
    assert _scoped(_wins_key(pseudonym)) not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "flip #1" in text
    assert "viewer-1" in text  # username IS rendered in the visible chat reply, never logged


def test_second_flip_within_cooldown_is_rejected(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(1)
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in json.loads(message_json)["text"]


def test_flip_allowed_again_after_cooldown_elapses(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 2
    provider, message_json = fake_host.relay_calls[-1]
    assert "flip #2" in json.loads(message_json)["text"]


def test_cooldown_corrupt_state_is_treated_as_no_cooldown(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_lastflip_key(pseudonym))] = b"not-a-timestamp"
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    assert result.detail == "flip"
    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "coinflip.cooldown_state_corrupt"
    ]
    assert corrupt_logs


def test_different_communities_have_independent_cooldowns(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "flip", community="comm-1"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "flip", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "flip"
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" not in json.loads(message_json)["text"]


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "flip", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.coinflip",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "flip", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized coinflip command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- call-correct / call-incorrect outcomes --------------------------------------


def test_correct_call_increments_wins_and_replies_right(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._flip_coin", lambda: "heads")
    envelope = _sample_envelope("twitch", "flip", call="heads")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_wins_key(pseudonym))].decode()) == 1
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "You called it right!" in text
    assert "HEADS" in text


def test_incorrect_call_does_not_increment_wins_and_replies_wrong(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._flip_coin", lambda: "tails")
    envelope = _sample_envelope("twitch", "flip", call="heads")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_wins_key(pseudonym)) not in fake_host.store
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Not this time." in text
    assert "TAILS" in text


def test_bare_flip_reply_has_no_win_or_loss_language(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._flip_coin", lambda: "heads")
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    assert result.detail == "flip"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "called it right" not in text
    assert "Not this time" not in text
    assert "HEADS" in text


def test_dispatch_ignores_an_invalid_call_value(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: `dispatch` only trusts `call` when it is a known value."""
    monkeypatch.setattr("app._flip_coin", lambda: "heads")
    envelope = _sample_envelope("twitch", "flip", call="not-a-real-call")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "flip"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_wins_key(pseudonym)) not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "called it right" not in text
    assert "Not this time" not in text


# -- !flip list ----------------------------------------------------------------


def test_list_before_any_flip(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_FLIPPED_YET


def test_list_after_a_correct_call_shows_flips_and_wins(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._flip_coin", lambda: "heads")
    _run(dispatch(_sample_envelope("twitch", "flip", call="heads"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Flips: 1" in text
    assert "Correct calls: 1" in text


def test_list_after_a_bare_flip_shows_zero_wins(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._flip_coin", lambda: "heads")
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Flips: 1" in text
    assert "Correct calls: 0" in text


def test_list_corrupt_flips_falls_back_to_nothing_flipped(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_flips_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_FLIPPED_YET


def test_list_corrupt_wins_falls_back_to_zero(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_flips_key(pseudonym))] = b"3"
    fake_host.store[_scoped(_wins_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Correct calls: 0" in text
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "coinflip.wins_corrupt"]
    assert corrupt_logs


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
    assert fake_host.store[_scoped("coinflip.config.cooldown")] == b"45"
    provider, message_json = fake_host.relay_calls[-1]
    assert "45" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 45", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    assert _scoped("coinflip.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "config_set_cooldown", arg="cooldown 45")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "coinflip.config_denied"]
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
    assert _scoped("coinflip.config.cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert expected_fragment in json.loads(message_json)["text"]


@pytest.mark.parametrize(
    "seconds", [MIN_COOLDOWN_SECONDS, MAX_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS]
)
def test_set_cooldown_accepts_boundary_values(seconds: int, fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg=f"cooldown {seconds}", is_mod=True
    )
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "config_set_cooldown"
    assert fake_host.store[_scoped("coinflip.config.cooldown")] == str(seconds).encode()


def test_zero_cooldown_allows_back_to_back_flips(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 0", is_mod=True
    )
    _run(dispatch(envelope, {}, http_client=None))

    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 2
    assert result.detail == "flip"
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" not in json.loads(message_json)["text"]


def test_configured_cooldown_is_honored_on_next_flip(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 5", is_mod=True
    )
    _run(dispatch(envelope, {}, http_client=None))

    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(6)
    result = _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_flips_key(pseudonym))].decode()) == 2
    assert result.detail == "flip"


def test_cooldown_config_corrupt_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped("coinflip.config.cooldown")] = b"not-a-number"
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(1)
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "coinflip.cooldown_config_corrupt"
    ]
    assert corrupt_logs
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in json.loads(message_json)["text"]  # default cooldown still applied


# -- usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _USAGE


# -- kv failure: fail loud, never silent ---------------------------------------


class _ErrorBackend:
    """Stand-in for the generated WIT `Error_Backend` variant case class."""


class _KvError(Exception):
    """Stand-in for the generated `Err` wrapper -- `.value` holds the error union member."""

    def __init__(self) -> None:
        self.value = _ErrorBackend()


def test_flip_replies_and_raises_on_kv_set_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "coinflip.kv_error"]
    assert error_logs


def test_flip_replies_and_raises_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


def test_flip_replies_and_raises_on_kv_increment_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_increment_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


# -- PII: never leak the raw actor into kv keys or logs -------------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_dispatch_never_logs_the_raw_actor(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_pseudonym_differs_per_actor() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


# -- _flip_coin() / _evaluate_flip() / _format_duration() unit coverage ---------


def test_flip_coin_returns_heads_or_tails() -> None:
    for _ in range(50):
        assert _flip_coin() in ("heads", "tails")


def test_flip_coin_uses_random_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("random.choice", lambda population: "tails")
    assert _flip_coin() == "tails"


def test_evaluate_flip_no_call_is_none() -> None:
    assert _evaluate_flip(None, "heads") is None


def test_evaluate_flip_matching_call_is_true() -> None:
    assert _evaluate_flip("heads", "heads") is True


def test_evaluate_flip_mismatched_call_is_false() -> None:
    assert _evaluate_flip("heads", "tails") is False


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (1, "1s"), (65, "1m 5s"), (3661, "1h 1m")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert _format_duration(seconds) == expected


# -- _caller_role_signal() unit coverage -----------------------------------------


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


# -- kv charset + PII-free logs + corrupt-state ERROR logging + lifecycle ---------------------


# regression: gh-631 -- this suite's hand-rolled kv fake accepts ANY key (the exact blind spot
# that hid `count`/`lurk`'s colon keys), so assert the real host charset over EVERY key a full
# flip/call/list/set-cooldown flow touches.
def test_every_kv_key_touched_satisfies_the_host_charset(fake_host: _FakeHost) -> None:
    from waddle_sdk.kv import validate_key

    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(60)
    _run(dispatch(_sample_envelope("twitch", "flip", call="heads"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "config_set_cooldown", arg="cooldown 30", is_mod=True),
            {},
            http_client=None,
        )
    )
    touched = {call[1] for call in fake_host.kv_calls}
    assert len(touched) >= 4, f"flow touched too few keys: {sorted(touched)}"
    for key in touched:
        validate_key(key)  # raises if any byte falls outside the real host's allowed charset
        assert ":" not in key


_PII_ACTOR = "PIIACTOR_alice"
_PII_TEXT = "PIITEXT_secret_phrase"


def _roundtrip(text: str, **role: bool) -> Any:
    """Run `transform()` then `dispatch()` -- the real two-stage path -- with PII sentinels."""
    event = _sample_event(text, **role)
    event.actor = _PII_ACTOR
    out = _run(transform(event))
    assert out is not None
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.coinflip",
        stage="action",
        event=out,
        ts="2026-10-05T00:00:00.000Z",
    )
    return _run(dispatch(envelope, {}, http_client=None))


def _assert_logs_pii_free(host: _FakeHost, *sentinels: str) -> int:
    """Assert no sentinel appears in any recorded log call; return how many were examined."""
    assert host.log_calls, "no log calls recorded -- the PII check would pass vacuously"
    for _lvl, message, fields_json in host.log_calls:
        blob = f"{message} {fields_json}".lower()
        for sentinel in sentinels:
            assert sentinel.lower() not in blob
    return len(host.log_calls)


# regression: gh-674 -- `coinflip` used to log the raw typed cooldown value
# (`raw=parts[1], error=str(exc)`); no actor, pseudonym or typed text may reach any log call.
def test_logs_never_contain_actor_pseudonym_or_typed_text(fake_host: _FakeHost) -> None:
    _roundtrip("!flip")
    fake_host.advance(60)
    _roundtrip("!coinflip tails")
    _roundtrip("!flip list")
    _roundtrip(f"!flip set cooldown {_PII_TEXT}", is_mod=True)  # invalid seconds
    _roundtrip(f"!flip set cooldown 99999999 {_PII_TEXT}", is_mod=True)  # wrong arity
    _roundtrip("!flip set cooldown 30", is_mod=True)  # applied
    _roundtrip("!flip set cooldown 30")  # denied: no role signal
    _roundtrip("!flip set cooldown 30", is_mod=False, is_broadcaster=False)  # denied
    _roundtrip(f"!flip bogus {_PII_TEXT}")  # usage
    _roundtrip(f"!flip heads list {_PII_TEXT}")  # call combined with a verb -> usage
    pseudonym = _expected_pseudonym(_PII_ACTOR)
    assert _assert_logs_pii_free(fake_host, _PII_ACTOR, _PII_TEXT, pseudonym, pseudonym[:8]) >= 15


def test_invalid_cooldown_log_carries_only_the_exception_class(fake_host: _FakeHost) -> None:
    _roundtrip(f"!flip set cooldown {_PII_TEXT}", is_mod=True)
    invalid = [f for _lvl, m, f in fake_host.log_calls if m == "coinflip.invalid_cooldown"]
    assert len(invalid) == 1
    assert json.loads(invalid[0]) == {"error_type": "ValueError"}


# regression: gh-674 -- backend-error logs carry the error class only, never typed text.
def test_kv_error_logs_never_contain_actor_or_typed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())
    with pytest.raises(RuntimeError, match="kv set failed"):
        _roundtrip("!flip")
    assert any(m == "coinflip.kv_error" for _lvl, m, _f in host.log_calls)
    _assert_logs_pii_free(host, _PII_ACTOR, _expected_pseudonym(_PII_ACTOR))


@pytest.mark.parametrize(
    ("corrupt_key", "command", "log_name"),
    [
        ("flips", "list", "coinflip.flips_corrupt"),
        ("wins", "list", "coinflip.wins_corrupt"),
        ("lastflip", "flip", "coinflip.cooldown_state_corrupt"),
        ("cooldown", "flip", "coinflip.cooldown_config_corrupt"),
    ],
)
def test_corrupt_state_self_heals_but_always_logs_at_error(
    corrupt_key: str, command: str, log_name: str, fake_host: _FakeHost
) -> None:
    pseudo = _expected_pseudonym("viewer-1")
    if corrupt_key == "wins":
        fake_host.store[_scoped(_flips_key(pseudo))] = b"2"  # `list` only reads wins when flips > 0
    key = {
        "flips": _flips_key(pseudo),
        "wins": _wins_key(pseudo),
        "lastflip": _lastflip_key(pseudo),
        "cooldown": "coinflip.config.cooldown",
    }[corrupt_key]
    fake_host.store[_scoped(key)] = b"\xff\xfe"
    _run(dispatch(_sample_envelope("twitch", command), {}, http_client=None))
    levels = [lvl for lvl, m, _f in fake_host.log_calls if m == log_name]
    assert levels, f"{log_name} was not logged"
    assert set(levels) == {0}  # the fake host's Level.ERROR


def test_called_flip_lifecycle_tracks_wins_only_for_correct_calls(
    monkeypatch: pytest.MonkeyPatch, fake_host: _FakeHost
) -> None:
    """flip -> call right -> call wrong -> list: flips count all three, wins count one."""
    import app

    pseudo = _expected_pseudonym("viewer-1")
    monkeypatch.setattr(app.random, "choice", lambda seq: "heads" if "heads" in seq else seq[0])
    _run(dispatch(_sample_envelope("twitch", "flip"), {}, http_client=None))
    fake_host.advance(60)
    _run(dispatch(_sample_envelope("twitch", "flip", call="heads"), {}, http_client=None))
    fake_host.advance(60)
    _run(dispatch(_sample_envelope("twitch", "flip", call="tails"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert fake_host.store[_scoped(_flips_key(pseudo))] == b"3"
    assert fake_host.store[_scoped(_wins_key(pseudo))] == b"1"
    assert json.loads(fake_host.relay_calls[-1][1])["text"] == "Flips: 3. Correct calls: 1."


def test_coinflip_holds_only_its_own_community_scoped_keys_and_no_economy_state(
    fake_host: _FakeHost,
) -> None:
    """The documented no-betting-ties rule: only `coinflip.*` keys, community-scoped, no balance."""
    _run(dispatch(_sample_envelope("twitch", "flip", call="heads"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    _run(
        dispatch(
            _sample_envelope("twitch", "config_set_cooldown", arg="cooldown 30", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert fake_host.store, "flow wrote no state -- the key check would be vacuous"
    for key in fake_host.store:
        assert key.startswith("c.comm-1.coinflip."), key
        assert not any(word in key for word in ("points", "balance", "wallet", "wager", "stake"))
