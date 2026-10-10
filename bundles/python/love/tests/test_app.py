"""Host-native tests for the `love` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/fish/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. Unlike `duel`/
`fish`'s own test suites (which hand-roll a permissive `kv` fake that
accepted colon-containing keys and let that bug slip past review -- gh-631,
fixed in production only after `count`/`lurk` shipped a 1.0.4), this suite
uses the shared, charset-enforcing `waddle_sdk.testing.install_fake_kv_host`
for the `kv` capability: a colon (or any other host-rejected byte) in this
bundle's own keys would raise `InvalidKvKeyError` and fail the test
immediately. `flags`/`relay`/`log`/`clock` have no shared fake yet
(`waddle_sdk.testing` only covers `kv`) so they're hand-rolled here, same
shape as every other bundle's test suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import sys
import types
from typing import Any

import pytest
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _FLAVOR_TIERS,
    _NO_RECORD_YET,
    _USAGE,
    _compute_match,
    _handle_ship,
    _love_best_key,
    _love_ships_key,
    _normalize_target,
    _pseudonym,
    _resolve_love,
    _resolve_ship,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(identity: str | None) -> str:
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`community_kv`'s own `c.<community>.<key>` prefix -- `fake_host.store` is keyed by this."""
    return _scoped_key(community, key)


class _Host:
    """Single-object test harness -- validated `kv` (shared `FakeKvHost`) + hand-rolled rest.

    `.store`/`.kv_calls` delegate to the installed `FakeKvHost` so a test
    reads/asserts on them exactly like `duel`/`fish`'s own hand-rolled fake --
    but any colon (or other host-rejected byte) this bundle's own code
    passes as a key raises `InvalidKvKeyError` instead of silently working
    (gh-631; see module docstring). `flags`/`relay`/`log`/`clock` have no
    shared fake yet, so they're hand-rolled onto the same `wit_world` module
    `install_fake_kv_host` already registered.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, flag_enabled: bool = True) -> None:
        self.kv: FakeKvHost = install_fake_kv_host(monkeypatch)
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []
        self.now_ms = 1_700_000_000_000
        self.day = "2026-10-05"

        wit_world = sys.modules["wit_world"]
        wit_world.imports.flags = types.SimpleNamespace(
            enabled=lambda key, default_value: flag_enabled
        )
        wit_world.imports.relay = types.SimpleNamespace(
            push=lambda provider, msg: self.relay_calls.append((provider, msg))
        )
        wit_world.imports.log = types.SimpleNamespace(
            Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
            write=lambda lvl, msg, fields_json: self.log_calls.append((lvl, msg, fields_json)),
        )
        wit_world.imports.clock = types.SimpleNamespace(
            now_millis=lambda: self.now_ms,
            now_rfc3339=lambda: f"{self.day}T00:00:00.000Z",
            monotonic_nanos=lambda: 0,
        )

    @property
    def store(self) -> dict[str, bytes]:
        return self.kv.store

    @property
    def kv_calls(self) -> list[tuple[str, Any]]:
        return self.kv.calls

    def set_day(self, day: str) -> None:
        self.day = day


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _Host(monkeypatch)


def _sample_event(
    text: str, *, channel_id: str | None = "12345", actor: str | None = "viewer-1"
) -> PlatformEvent:
    return PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=actor,
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-05T00:00:00.000Z",
    )


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    target: str | None = None,
    target_a: str | None = None,
    target_b: str | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if target is not None:
        payload["target"] = target
    if target_a is not None:
        payload["target_a"] = target_a
    if target_b is not None:
        payload["target_b"] = target_b
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.love",
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


@pytest.mark.parametrize("text", ["!love", "!LOVE", "  !love  "])
def test_bare_love_with_no_target_is_usage(text: str, fake_host: _Host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!love bob", "!LOVE bob", "!love @bob"])
def test_love_with_target_matches_as_pair_self(text: str, fake_host: _Host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "pair_self"
    assert result.payload["target"] in ("bob", "@bob")


def test_love_list_matches(fake_host: _Host) -> None:
    result = _run(transform(_sample_event("!love list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_love_list_with_trailing_args_is_usage(fake_host: _Host) -> None:
    result = _run(transform(_sample_event("!love list extra")))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!love enable ai", "!love reset", "!love foo bar"])
def test_unsupported_verbs_and_multiword_targets_reply_usage(text: str, fake_host: _Host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!ship alice bob", "!SHIP alice bob", "!ship @alice @bob"])
def test_ship_with_two_targets_matches(text: str, fake_host: _Host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "ship"
    assert result.payload["target_a"] in ("alice", "@alice")
    assert result.payload["target_b"] in ("bob", "@bob")


@pytest.mark.parametrize("text", ["!ship", "!ship alice", "!ship alice bob carol"])
def test_ship_with_wrong_token_count_is_usage(text: str, fake_host: _Host) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!loveme", "!shipit", "notlove", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _Host) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_string_text_is_ignored(fake_host: _Host) -> None:
    event = PlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": None, "channel_id": "1"},
        occurred_at="",
    )
    assert _run(transform(event)) is None


@pytest.mark.parametrize("text", ["!love bob", "!ship alice bob"])
def test_disabled_flag_suppresses_the_reply(text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _Host(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event(text))) is None


# -- _resolve_love() / _resolve_ship() / _normalize_target() direct unit coverage ----


def test_resolve_love_bare_is_usage() -> None:
    assert _resolve_love("!love") == ("usage", None)


def test_resolve_love_single_token_is_pair_self() -> None:
    assert _resolve_love("!love bob") == ("pair_self", "bob")


def test_resolve_love_list() -> None:
    assert _resolve_love("!love list") == ("list", None)


def test_resolve_love_two_token_non_verb_is_usage() -> None:
    assert _resolve_love("!love foo bar") == ("usage", None)


def test_resolve_ship_two_tokens() -> None:
    assert _resolve_ship("!ship alice bob") == ("ship", ("alice", "bob"))


@pytest.mark.parametrize("text", ["!ship", "!ship alice", "!ship alice bob carol"])
def test_resolve_ship_wrong_token_count_is_usage(text: str) -> None:
    assert _resolve_ship(text) == ("usage", None)


@pytest.mark.parametrize(
    ("raw", "expected"), [("bob", "bob"), ("@bob", "bob"), ("a" * 32, "a" * 32)]
)
def test_normalize_target_accepts_plausible_usernames(raw: str, expected: str) -> None:
    assert _normalize_target(raw) == expected


@pytest.mark.parametrize("raw", ["@", "", "a" * 33, "bob!", "bob bob", "$$$"])
def test_normalize_target_rejects_implausible_strings(raw: str) -> None:
    assert _normalize_target(raw) is None


# -- _compute_match() pure-function behavior -----------------------------------


def test_compute_match_percent_and_flavor_always_in_range() -> None:
    names = ["alice", "bob", "carol", "dave", "eve"]
    days = ["2026-01-01", "2026-06-15", "2026-12-31"]
    for a, b, day in itertools.product(names, names, days):
        percent, flavor = _compute_match(_pseudonym(a), _pseudonym(b), day)
        assert 0 <= percent <= 100
        assert any(flavor in tier for tier in _FLAVOR_TIERS)


def test_compute_match_is_deterministic_for_same_pair_and_day() -> None:
    p_a, p_b = _pseudonym("alice"), _pseudonym("bob")
    assert _compute_match(p_a, p_b, "2026-10-05") == _compute_match(p_a, p_b, "2026-10-05")


def test_compute_match_is_order_independent() -> None:
    p_a, p_b = _pseudonym("alice"), _pseudonym("bob")
    assert _compute_match(p_a, p_b, "2026-10-05") == _compute_match(p_b, p_a, "2026-10-05")


def test_compute_match_varies_by_day() -> None:
    p_a, p_b = _pseudonym("alice"), _pseudonym("bob")
    results = {_compute_match(p_a, p_b, f"2026-01-{day:02d}") for day in range(1, 29)}
    assert len(results) > 1


# -- _handle_ship() direct unit coverage (stateless) ---------------------------


def test_handle_ship_resolves(fake_host: _Host) -> None:
    reply, detail = _handle_ship("alice", "bob")
    assert detail == "resolved"
    assert "alice" in reply and "bob" in reply and "%" in reply


def test_handle_ship_self_pair_case_insensitive() -> None:
    reply, detail = _handle_ship("Alice", "alice")
    assert detail == "self"
    assert "100%" in reply


def test_handle_ship_unknown_target_a() -> None:
    reply, detail = _handle_ship("@", "bob")
    assert detail == "unknown_target"
    assert "@" in reply


def test_handle_ship_unknown_target_b() -> None:
    reply, detail = _handle_ship("alice", "$$$")
    assert detail == "unknown_target"
    assert "$$$" in reply


# -- dispatch(): !ship (stateless) ---------------------------------------------


def test_dispatch_ship_resolves_and_touches_no_state(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a="alice", target_b="bob")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "ship:resolved"
    assert fake_host.kv_calls == []
    provider, message_json = fake_host.relay_calls[-1]
    assert "alice" in message_json and "bob" in message_json


def test_dispatch_ship_is_deterministic_same_day(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a="alice", target_b="bob")
    _run(dispatch(envelope, {}, http_client=None))
    _run(dispatch(envelope, {}, http_client=None))
    first = fake_host.relay_calls[0][1]
    second = fake_host.relay_calls[1][1]
    assert first == second


def test_dispatch_ship_self_pair(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a="bob", target_b="Bob")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "ship:self"
    assert fake_host.kv_calls == []


def test_dispatch_ship_unknown_target(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a="alice", target_b="@")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "ship:unknown_target"
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_ship_target_b_missing(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a="alice", target_b=None)
    with pytest.raises(ValueError, match="requires target_a and target_b"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_ship_target_a_missing(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "ship", target_a=None, target_b="bob")
    with pytest.raises(ValueError, match="requires target_a and target_b"):
        _run(dispatch(envelope, {}, http_client=None))


# -- dispatch(): !love <user> (pair_self) --------------------------------------


def test_pair_self_persists_state_and_replies(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (73, "sparks are flying!"))
    envelope = _sample_envelope("twitch", "pair_self", target="bob")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "love:resolved"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_love_ships_key(pseudonym))].decode()) == 1
    assert int(fake_host.store[_scoped(_love_best_key(pseudonym))].decode()) == 73
    provider, message_json = fake_host.relay_calls[-1]
    assert "73%" in message_json and "sparks are flying!" in message_json


def test_second_pair_self_lower_percent_does_not_lower_best(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    percents = iter([73, 40])
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (next(percents), "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="carol"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_love_ships_key(pseudonym))].decode()) == 2
    assert int(fake_host.store[_scoped(_love_best_key(pseudonym))].decode()) == 73


def test_second_pair_self_higher_percent_updates_best(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    percents = iter([40, 90])
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (next(percents), "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="carol"), {}, http_client=None))

    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_love_best_key(pseudonym))].decode()) == 90


def test_self_love_is_always_100_and_never_calls_compute_match(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(a: str, b: str, day: str) -> tuple[int, str]:
        raise AssertionError("self-love must not call _compute_match")

    monkeypatch.setattr("app._compute_match", _boom)
    envelope = _sample_envelope("twitch", "pair_self", target="viewer-1")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "love:self"
    pseudonym = _expected_pseudonym("viewer-1")
    assert int(fake_host.store[_scoped(_love_best_key(pseudonym))].decode()) == 100
    provider, message_json = fake_host.relay_calls[-1]
    assert "self-love is important" in message_json


def test_self_love_case_insensitive(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "pair_self", target="Viewer-1")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "love:self"


def test_pair_self_unknown_target_touches_no_state(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(a: str, b: str, day: str) -> tuple[int, str]:
        raise AssertionError("unknown target must not call _compute_match")

    monkeypatch.setattr("app._compute_match", _boom)
    envelope = _sample_envelope("twitch", "pair_self", target="@")
    result = _run(dispatch(envelope, {}, http_client=None))

    assert result.detail == "love:unknown_target"
    assert fake_host.kv_calls == []
    provider, message_json = fake_host.relay_calls[-1]
    assert "don't know who" in message_json


def test_dispatch_raises_when_pair_self_target_missing(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "pair_self", target=None)
    with pytest.raises(ValueError, match="requires a target"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "pair_self", target="bob", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _Host) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.love",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "pair_self", "target": "bob", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _Host) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized love command"):
        _run(dispatch(envelope, {}, http_client=None))


# -- !love list -----------------------------------------------------------------


def test_list_before_any_love(fake_host: _Host) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert _NO_RECORD_YET in message_json


def test_list_after_loves_shows_count_and_best(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    percents = iter([60, 95])
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (next(percents), "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="carol"), {}, http_client=None))

    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert result.detail == "list"
    provider, message_json = fake_host.relay_calls[-1]
    assert "2 times" in message_json
    assert "95%" in message_json


def test_list_singular_wording_for_one_ship(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (50, "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "1 time;" in message_json
    assert result.detail == "list"


def test_list_corrupt_ships_falls_back_to_zero(fake_host: _Host) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_love_ships_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert _NO_RECORD_YET in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "love.ships_corrupt"]
    assert corrupt_logs
    assert result.detail == "list"


def test_list_corrupt_best_falls_back_to_zero(fake_host: _Host) -> None:
    pseudonym = _expected_pseudonym("viewer-1")
    fake_host.store[_scoped(_love_ships_key(pseudonym))] = b"2"
    fake_host.store[_scoped(_love_best_key(pseudonym))] = b"not-a-number"
    result = _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    provider, message_json = fake_host.relay_calls[-1]
    assert "2 times" in message_json
    assert "0%" in message_json
    corrupt_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "love.best_corrupt"]
    assert corrupt_logs
    assert result.detail == "list"


# -- usage -----------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _Host) -> None:
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


def test_pair_self_replies_and_raises_on_kv_increment_error(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_increment(key: str, delta: int, ttl: int) -> int:
        raise _KvError()

    sys.modules["wit_world"].imports.kv.increment = raising_increment

    with pytest.raises(RuntimeError, match="kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in message_json
    error_logs = [(lvl, m) for lvl, m, _f in fake_host.log_calls if m == "love.kv_error"]
    assert error_logs


def test_pair_self_replies_and_raises_on_kv_set_error(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (77, "flavor"))

    def raising_set(key: str, value: bytes, ttl: int) -> None:
        raise _KvError()

    sys.modules["wit_world"].imports.kv.set = raising_set

    with pytest.raises(RuntimeError, match="kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in message_json


def test_list_replies_and_raises_on_kv_get_error(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising_get(key: str) -> bytes | None:
        raise _KvError()

    sys.modules["wit_world"].imports.kv.get = raising_get

    with pytest.raises(RuntimeError, match="kv get failed"):
        _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))

    provider, message_json = fake_host.relay_calls[-1]
    assert "unavailable" in message_json


# -- PII: never leak the raw actor/target into kv keys or logs ------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_identity() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_pseudonym_differs_per_identity() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)  # anonymous is stable, still never raw


def test_dispatch_never_logs_the_raw_actor_or_target(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (50, "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "bob" not in message
        assert "bob" not in fields_json


# -- kv key charset: colon-free, caught by the shared FakeKvHost (gh-631) ------


def test_all_kv_keys_used_are_colon_free(fake_host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key this bundle ever constructs must pass the real host's charset.

    `FakeKvHost.get/set/increment` all call `waddle_sdk.kv.validate_key()`
    before touching `.store` -- if any key built by this module contained a
    `:`, every call below would already have raised `InvalidKvKeyError`.
    This test exists to name the property explicitly, not just rely on it
    being incidentally exercised elsewhere.
    """
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (50, "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert fake_host.store  # at least one key was actually written
    for key in fake_host.store:
        assert ":" not in key


# -- PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free) ----------

_SENTINEL = "SENTINELpii9f3a"

#: Strict per-message allowlist: a log line may carry ONLY these fields. A new field (e.g. a
#: raw `target=`/`actor=`/`text=`) fails here instead of silently shipping user input to telemetry.
_ALLOWED_LOG_FIELDS: dict[str, frozenset[str]] = {
    "love.transform matched": frozenset({"command"}),
    "love.dispatch relayed": frozenset({"platform", "command", "detail"}),
    "love.kv_error": frozenset({"op", "error"}),
    "love.best_corrupt": frozenset({"community"}),
    "love.ships_corrupt": frozenset({"community"}),
    "love.missing_community": frozenset({"command"}),
}


def _assert_logs_pii_free(host: _Host, *, minimum_lines: int) -> set[str]:
    """Every captured log line is allow-listed field-by-field and free of the sentinel.

    Asserts a non-empty denominator first -- a check that examined zero log lines proves nothing.
    Returns the distinct messages seen so callers can prove each branch was exercised.
    """
    assert len(host.log_calls) >= minimum_lines
    for _lvl, message, fields_json in host.log_calls:
        assert _SENTINEL not in message
        assert _SENTINEL not in fields_json
        assert message in _ALLOWED_LOG_FIELDS, f"unexpected log message {message!r}"
        assert set(json.loads(fields_json)) <= _ALLOWED_LOG_FIELDS[message]
    return {message for _lvl, message, _f in host.log_calls}


def test_transform_logs_never_carry_user_input(fake_host: _Host) -> None:
    # regression: gh-674
    for text in (
        f"!love {_SENTINEL}",
        f"!love {_SENTINEL} extra",
        f"!love list {_SENTINEL}",
        f"!love enable {_SENTINEL}",
        f"!ship {_SENTINEL} other",
        f"!ship {_SENTINEL}",
        "!love",
        "!love list",
    ):
        assert _run(transform(_sample_event(text, actor=_SENTINEL))) is not None
    seen = _assert_logs_pii_free(fake_host, minimum_lines=8)
    assert seen == {"love.transform matched"}


def test_dispatch_logs_never_carry_user_input_on_any_command(fake_host: _Host) -> None:
    # regression: gh-674 -- sentinel as actor AND as every typed target, across every outcome.
    def go(command: str, **kw: str) -> None:
        envelope = _sample_envelope("twitch", command, actor=_SENTINEL, **kw)
        _run(dispatch(envelope, {}, http_client=None))

    go("pair_self", target=_SENTINEL)  # self-love (target == actor)
    go("pair_self", target=f"{_SENTINEL}x")  # resolved
    go("pair_self", target=f"!!!{_SENTINEL}")  # unknown target
    go("ship", target_a=_SENTINEL, target_b="other")
    go("ship", target_a=_SENTINEL, target_b=_SENTINEL)  # self
    go("ship", target_a=f"!!!{_SENTINEL}", target_b="other")  # unknown a
    go("ship", target_a="other", target_b=f"!!!{_SENTINEL}")  # unknown b
    go("list")
    go("usage")
    pseudonym = _pseudonym(_SENTINEL)
    fake_host.store[_scoped(_love_best_key(pseudonym))] = b"garbage"
    go("list")  # corrupt best
    fake_host.store[_scoped(_love_ships_key(pseudonym))] = b"garbage"
    go("list")  # corrupt ships

    seen = _assert_logs_pii_free(fake_host, minimum_lines=9)
    assert seen == {"love.dispatch relayed", "love.best_corrupt", "love.ships_corrupt"}
    assert all(_SENTINEL not in key for key in fake_host.store)


def test_failure_paths_never_log_user_input(fake_host: _Host) -> None:
    # regression: gh-674 -- a kv failure whose own exception text echoes user-ish data must still
    # log only the classified error-case NAME, and the missing-community guard only the command.
    def boom(key: str, delta: int, ttl: int) -> int:
        raise RuntimeError(f"backend detail {_SENTINEL}")

    sys.modules["wit_world"].imports.kv.increment = boom
    with pytest.raises(RuntimeError) as excinfo:
        _run(
            dispatch(
                _sample_envelope("twitch", "pair_self", actor=_SENTINEL, target="bob"),
                {},
                http_client=None,
            )
        )
    assert _SENTINEL not in str(excinfo.value)
    with pytest.raises(ValueError):
        _run(
            dispatch(
                _sample_envelope("twitch", "list", actor=_SENTINEL, community=None),
                {},
                http_client=None,
            )
        )

    seen = _assert_logs_pii_free(fake_host, minimum_lines=2)
    assert seen == {"love.kv_error", "love.missing_community"}


# -- corrupt store: ERROR-logged (loud) with the community id only -----------------------


@pytest.mark.parametrize("payload", [b"", b"12abc", b"\xff\xfe", b"1.5", b" "])
def test_every_corrupt_counter_shape_is_error_logged_and_reads_as_zero(
    payload: bytes, fake_host: _Host
) -> None:
    fake_host.store[_scoped(_love_ships_key(_pseudonym("viewer-1")))] = payload
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert _NO_RECORD_YET in fake_host.relay_calls[-1][1]
    corrupt = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "love.ships_corrupt"
    ]
    assert corrupt == [(0, {"community": "comm-1"})]  # Level.ERROR


def test_corrupt_best_is_error_logged_and_reads_as_zero(fake_host: _Host) -> None:
    pseudonym = _pseudonym("viewer-1")
    fake_host.store[_scoped(_love_ships_key(pseudonym))] = b"2"
    fake_host.store[_scoped(_love_best_key(pseudonym))] = b"\xff"
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert "best match: 0%" in fake_host.relay_calls[-1][1]
    corrupt = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "love.best_corrupt"
    ]
    assert corrupt == [(0, {"community": "comm-1"})]


def test_corrupt_best_is_replaced_by_the_next_pairing(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt best reads as 0, so any real percent overwrites (self-heals) it."""
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (37, "flavor"))
    best_key = _scoped(_love_best_key(_pseudonym("viewer-1")))
    fake_host.store[best_key] = b"garbage"
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    assert fake_host.store[best_key] == b"37"


# -- no silent fallback: kv failures leave state untouched, one error reply only ----------


def _fail_kv_for(op: str, key_fragment: str | None = None) -> None:
    """Make `wit_world.imports.kv.<op>` raise a WIT-shaped error (optionally for one key only)."""
    kv_ns = sys.modules["wit_world"].imports.kv
    original = getattr(kv_ns, op)

    def _maybe_raise(key: str, *rest: Any) -> Any:
        if key_fragment is None or key_fragment in key:
            raise _KvError()
        return original(key, *rest)

    setattr(kv_ns, op, _maybe_raise)


def test_pair_self_best_read_failure_fails_loud_with_one_reply(fake_host: _Host) -> None:
    _fail_kv_for("get", ".love.best.")
    with pytest.raises(RuntimeError, match="love kv get failed: _ErrorBackend"):
        _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    assert len(fake_host.relay_calls) == 1
    assert "temporarily unavailable" in fake_host.relay_calls[0][1]
    errors = [(lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "love.kv_error"]
    assert errors == [(0, {"op": "get", "error": "_ErrorBackend"})]


def test_failed_increment_writes_no_best_and_relays_no_result(fake_host: _Host) -> None:
    _fail_kv_for("increment")
    with pytest.raises(RuntimeError, match="love kv increment failed"):
        _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    assert fake_host.store == {}
    assert len(fake_host.relay_calls) == 1
    assert "%" not in fake_host.relay_calls[0][1]


def test_failed_best_write_leaves_the_previous_best_intact(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    best_key = _scoped(_love_best_key(_pseudonym("viewer-1")))
    fake_host.store[best_key] = b"10"
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (90, "flavor"))
    _fail_kv_for("set")
    with pytest.raises(RuntimeError, match="love kv set failed"):
        _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    assert fake_host.store[best_key] == b"10"


def test_relay_failure_propagates_and_is_not_logged_as_relayed(fake_host: _Host) -> None:
    def _boom(provider: str, msg: str) -> None:
        raise RuntimeError("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(RuntimeError, match="relay down"):
        _run(
            dispatch(
                _sample_envelope("twitch", "ship", target_a="a", target_b="b"), {}, http_client=None
            )
        )
    assert not any(m == "love.dispatch relayed" for _lvl, m, _f in fake_host.log_calls)


def test_missing_community_logs_error_and_touches_no_state(fake_host: _Host) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_sample_envelope("twitch", "list", community=None), {}, http_client=None))
    assert fake_host.kv_calls == []
    assert fake_host.relay_calls == []
    errors = [
        (lvl, json.loads(f)) for lvl, m, f in fake_host.log_calls if m == "love.missing_community"
    ]
    assert errors == [(0, {"command": "list"})]


# -- state scoping and read-only commands --------------------------------------------------


def test_ship_counts_never_leak_across_communities(
    fake_host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (50, "flavor"))
    _run(
        dispatch(
            _sample_envelope("twitch", "pair_self", community="comm-1", target="bob"),
            {},
            http_client=None,
        )
    )
    _run(dispatch(_sample_envelope("twitch", "list", community="comm-2"), {}, http_client=None))
    assert _NO_RECORD_YET in fake_host.relay_calls[-1][1]


def test_list_is_read_only(fake_host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app._compute_match", lambda a, b, day: (50, "flavor"))
    _run(dispatch(_sample_envelope("twitch", "pair_self", target="bob"), {}, http_client=None))
    before = dict(fake_host.store)
    fake_host.kv_calls.clear()
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert fake_host.store == before
    assert {call[0] for call in fake_host.kv_calls} == {"get"}


def test_missing_actor_pairs_as_someone_and_is_never_self(fake_host: _Host) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "pair_self", actor=None, target="someone"),
            {},
            http_client=None,
        )
    )
    assert result.detail == "love:resolved"
    # No actor identity exists, so a typed "someone" can never be detected as self-pairing.
    assert "self-love is important" not in fake_host.relay_calls[-1][1]
    assert _scoped(_love_ships_key(_pseudonym(None))) in fake_host.store


@pytest.mark.parametrize("platform", ["twitch", "discord"])
def test_replies_relay_to_the_events_own_platform(platform: str, fake_host: _Host) -> None:
    result = _run(dispatch(_sample_envelope(platform, "usage"), {}, http_client=None))
    assert result.transport == platform
    assert fake_host.relay_calls[-1][0] == platform


# -- transform(): payload shape + flag gate ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("!love bob", {"command", "channel_id", "target"}),
        ("!ship a b", {"command", "channel_id", "target_a", "target_b"}),
        ("!love list", {"command", "channel_id"}),
        ("!love", {"command", "channel_id"}),
        ("!SHIP a b", {"command", "channel_id", "target_a", "target_b"}),
        ("  !Love bob  ", {"command", "channel_id", "target"}),
    ],
)
def test_transform_forwards_only_the_fields_dispatch_needs(
    text: str, keys: set[str], fake_host: _Host
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert set(result.payload) == keys


def test_flag_is_checked_by_key_and_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _Host(monkeypatch)
    seen: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        seen.append((key, default_value))
        return False

    sys.modules["wit_world"].imports.flags = types.SimpleNamespace(enabled=_enabled)
    assert _run(transform(_sample_event("!love bob"))) is None
    assert seen == [("waddles.command-love", False)]
    assert host.kv_calls == []
