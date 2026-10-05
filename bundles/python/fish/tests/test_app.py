"""Host-native tests for the `fish` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors (extended with a fake `kv`/`clock` import, same shape as
`bundles/python/lurk/tests/test_app.py`'s own harness -- `community_kv` is a thin prefix wrapper
over the same `kv` WIT import, so the same fake serves both).
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
    _NOTHING_CAUGHT_YET,
    _USAGE,
    DEFAULT_COOLDOWN_SECONDS,
    MAX_COOLDOWN_SECONDS,
    MIN_COOLDOWN_SECONDS,
    FishCatch,
    _biggest_key,
    _caller_role_signal,
    _count_key,
    _format_duration,
    _lastcast_key,
    _pseudonym,
    _resolve_command,
    _roll_catch,
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
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if arg is not None:
        payload["arg"] = arg
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.fish",
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


@pytest.mark.parametrize("text", ["!fish", "!FISH", "  !fish  "])
def test_bare_fish_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "cast"


def test_fish_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!fish list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_fish_list_with_trailing_args_is_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!fish list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_fish_set_cooldown_matches_with_args(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!fish set cooldown 30")))
    assert result is not None
    assert result.payload["command"] == "config_set_cooldown"
    assert result.payload["arg"] == "cooldown 30"


@pytest.mark.parametrize(
    "text", ["!fish enable ai", "!fish bogus", "!fish add", "!fish reset", "!fish disable foo"]
)
def test_unsupported_verbs_and_bad_grammar_reply_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!fishing", "fish", "!fisher", "hello", ""])
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
    assert _run(transform(_sample_event("!fish"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!fish", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!fish")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() direct unit coverage -----------------------------------


def test_resolve_command_none_is_usage() -> None:
    assert _resolve_command(None) == "usage"


def test_resolve_command_bare_is_cast() -> None:
    spec = CommandSpec(name="fish")
    parsed = parse_command("!fish", spec)
    assert _resolve_command(parsed) == "cast"


def test_resolve_command_unimplemented_verb_is_usage() -> None:
    parsed = ParsedCommand(command="fish", sub_module=None, option="sub", args=None)
    assert _resolve_command(parsed) == "usage"


# -- cast / cooldown state machine ---------------------------------------------


def test_first_cast_persists_state_and_replies(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "cast")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "cast"
    pseudonym = _expected_pseudonym("viewer-1")
    assert _scoped(_lastcast_key(pseudonym)) in fake_host.store
    assert int(fake_host.store[_scoped(_count_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Total catches: 1" in text
    assert "viewer-1" in text  # username IS rendered in the visible chat reply, never logged


def test_second_cast_within_cooldown_is_rejected(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    fake_host.advance(1)
    result = _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    assert result.detail == "cast"
    pseudonym = _expected_pseudonym("viewer-1")
    # not incremented again
    assert int(fake_host.store[_scoped(_count_key(pseudonym))].decode()) == 1
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" in json.loads(message_json)["text"]


def test_cast_allowed_again_after_cooldown_elapses(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    result = _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    assert result.detail == "cast"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_count_key(pseudonym))].decode()) == 2
    provider, message_json = fake_host.relay_calls[-1]
    assert "Total catches: 2" in json.loads(message_json)["text"]


def test_cooldown_corrupt_state_is_treated_as_no_cooldown(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_lastcast_key(pseudonym))] = b"not-a-timestamp"
    result = _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    assert result.detail == "cast"
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "fish.cooldown_state_corrupt"]
    assert corrupt_logs


def test_different_communities_have_independent_cooldowns(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "cast", community="comm-1"), {}, http_client=None))
    result = _run(
        dispatch(_sample_envelope("twitch", "cast", community="comm-2"), {}, http_client=None)
    )
    assert result.detail == "cast"
    provider, message_json = fake_host.relay_calls[-1]
    assert "slow down" not in json.loads(message_json)["text"]


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "cast", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.fish",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "cast", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized fish command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- biggest-catch tracking -----------------------------------------------------


def test_biggest_catch_is_recorded_and_only_updates_when_bigger(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = FishCatch("Minnow", "common", 0.2, "tiny")
    big = FishCatch("Kraken Spawn", "legendary", 60.0, "huge")

    monkeypatch.setattr("app._roll_catch", lambda: (small, 0.2))
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    pseudonym = _expected_pseudonym("viewer-1")
    first = json.loads(fake_host.store[_scoped(_biggest_key(pseudonym))].decode())
    assert first == {"name": "Minnow", "rarity": "common", "weight_lbs": 0.2}

    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    monkeypatch.setattr("app._roll_catch", lambda: (big, 60.0))
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    second = json.loads(fake_host.store[_scoped(_biggest_key(pseudonym))].decode())
    assert second == {"name": "Kraken Spawn", "rarity": "legendary", "weight_lbs": 60.0}

    fake_host.advance(DEFAULT_COOLDOWN_SECONDS + 1)
    monkeypatch.setattr("app._roll_catch", lambda: (small, 0.2))
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    unchanged = json.loads(fake_host.store[_scoped(_biggest_key(pseudonym))].decode())
    assert unchanged == {"name": "Kraken Spawn", "rarity": "legendary", "weight_lbs": 60.0}


def test_corrupt_biggest_record_is_overwritten_rather_than_crashing(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_biggest_key(pseudonym))] = b"not valid json"
    fish = FishCatch("Bass", "uncommon", 3.0, "fight")
    monkeypatch.setattr("app._roll_catch", lambda: (fish, 3.0))

    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "fish.biggest_corrupt"]
    assert corrupt_logs
    assert json.loads(fake_host.store[_scoped(_biggest_key(pseudonym))].decode())["name"] == "Bass"


# -- !fish list -------------------------------------------------------------


def test_list_before_any_catch(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_CAUGHT_YET


def test_list_after_a_catch_shows_total_and_biggest(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    text = json.loads(message_json)["text"]
    assert "Total catches: 1" in text
    assert "Biggest catch" in text


def test_list_corrupt_count_falls_back_to_nothing_caught(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_count_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert json.loads(message_json)["text"] == _NOTHING_CAUGHT_YET


def test_list_corrupt_biggest_degrades_to_nothing_yet(fake_host: _FakeHost) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_count_key(pseudonym))] = b"3"
    fake_host.store[_scoped(_biggest_key(pseudonym))] = b"{not json"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "nothing yet" in json.loads(message_json)["text"]
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
    assert fake_host.store[_scoped("fish:config:cooldown")] == b"45"
    provider, message_json = fake_host.relay_calls[-1]
    assert "45" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 45", is_mod=False, is_broadcaster=False
    )
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    assert _scoped("fish:config:cooldown") not in fake_host.store
    provider, message_json = fake_host.relay_calls[-1]
    assert "only moderators/broadcasters" in json.loads(message_json)["text"]


def test_set_cooldown_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    envelope = _sample_envelope("discord", "config_set_cooldown", arg="cooldown 45")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "config_set_cooldown:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "fish.config_denied"]
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
    assert _scoped("fish:config:cooldown") not in fake_host.store
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
    assert fake_host.store[_scoped("fish:config:cooldown")] == str(seconds).encode()


def test_configured_cooldown_is_honored_on_next_cast(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope(
        "twitch", "config_set_cooldown", arg="cooldown 5", is_mod=True
    )
    _run(dispatch(envelope, {}, http_client=None))

    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    fake_host.advance(6)
    result = _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_count_key(pseudonym))].decode()) == 2
    assert result.detail == "cast"


def test_cooldown_config_corrupt_falls_back_to_default(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped("fish:config:cooldown")] = b"not-a-number"
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    fake_host.advance(1)
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    corrupt_logs = [
        m for _lvl, m, _f in fake_host.log_calls if m == "fish.cooldown_config_corrupt"
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


def test_cast_replies_and_raises_on_kv_set_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_set_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "fish.kv_error"]
    assert error_logs


def test_cast_replies_and_raises_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_get_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


def test_cast_replies_and_raises_on_kv_increment_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host, kv_increment_raises=_KvError())

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))

    provider, message_json = host.relay_calls[-1]
    assert "unavailable" in json.loads(message_json)["text"]


# -- PII: never leak the raw actor into kv keys or logs -------------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_actor() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_dispatch_never_logs_the_raw_actor(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "cast"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "actor" not in json.loads(fields_json)


def test_pseudonym_differs_per_actor() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


# -- _roll_catch() / _format_duration() unit coverage ---------------------------


def test_roll_catch_uses_weighted_choice_and_variance(monkeypatch: pytest.MonkeyPatch) -> None:
    fish = FishCatch("Bass", "uncommon", 10.0, "fight")
    monkeypatch.setattr("random.choices", lambda population, weights, k: [fish])
    monkeypatch.setattr("random.uniform", lambda lo, hi: 1.0)

    result_fish, weight = _roll_catch()
    assert result_fish is fish
    assert weight == 10.0


def test_roll_catch_returns_a_table_entry_and_positive_weight() -> None:
    fish, weight = _roll_catch()
    assert isinstance(fish, FishCatch)
    assert weight > 0


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
