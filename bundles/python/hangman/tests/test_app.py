"""Host-native tests for the `hangman` bundle's stateful `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/first/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors, including the
shared, charset-enforcing `waddle_sdk.testing.install_fake_kv_host` fake for
`kv`.
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
    _ACTIVE_KEY,
    _INVALID_LETTER,
    _MAX_WRONG_GUESSES,
    _NO_ACTIVE_GAME,
    _USAGE,
    WORD_BANK,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@dataclass
class _FakeHost:
    """Composes the shared `FakeKvHost` with hand-rolled `flags`/`relay`/`log` stubs."""

    kv: FakeKvHost
    relay_calls: list[tuple[str, str]] = field(default_factory=list)
    log_calls: list[tuple[Any, str, str]] = field(default_factory=list)
    flag_enabled: bool = True

    @property
    def store(self) -> dict[str, bytes]:
        """The shared kv fake's in-memory store."""
        return self.kv.store

    @property
    def kv_calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        """The shared kv fake's recorded host calls."""
        return self.kv.calls


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
        app_id="waddles.core.example.hangman",
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


def _start_with_word(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch, word: str = "python"
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: word)
    _run(dispatch(_envelope("start"), {}, http_client=None))


# ---------------------------------------------------------------------------
# transform() parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["!hangman", "!HANGMAN", "  !hangman  "])
def test_bare_hangman_maps_to_reveal_read(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == "reveal"


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [("!hangman start", "start"), ("!hangman START", "start"), ("!hangman reveal", "reveal")],
)
def test_transform_parses_declared_sub_modules(
    text: str, expected_action: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == expected_action


def test_transform_parses_guess_with_a_letter(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!hangman guess p")))
    assert result is not None
    assert result.payload["action"] == "guess"
    assert result.payload["arg"] == "p"


def test_transform_guess_with_no_letter_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!hangman guess")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_valid_grammar_verb_with_no_meaning_for_hangman_returns_usage(
    fake_host: _FakeHost,
) -> None:
    result = _run(transform(_sample_event("!hangman list")))
    assert result is not None
    assert result.payload["action"] == "usage"


def test_unknown_token_returns_usage_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!hangman bogus")))
    assert result is not None
    assert result.payload["action"] == "usage"


@pytest.mark.parametrize("text", ["!hangmania", "hangman", "!hangmaned", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!hangman start"))) is None
    assert _run(transform(_sample_event("!hangman guess p"))) is None


# ---------------------------------------------------------------------------
# start: picks a word, never overwrites an in-progress game
# ---------------------------------------------------------------------------


def test_start_picks_one_of_the_embedded_words(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("start"), {}, http_client=None))
    assert result.detail == "start"
    stored = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)].decode())
    assert stored["word"] in WORD_BANK
    assert stored["guessed"] == []
    assert stored["wrong"] == 0


def test_start_again_does_not_overwrite_the_active_game(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    first_active = fake_host.store[_scoped(_ACTIVE_KEY)]

    result = _run(dispatch(_envelope("start"), {}, http_client=None))
    assert result.detail == "start"
    assert "already active" in _relay_text(fake_host)
    assert fake_host.store[_scoped(_ACTIVE_KEY)] == first_active


# ---------------------------------------------------------------------------
# guess: win / loss / repeat / invalid-input / no-active-game
# ---------------------------------------------------------------------------


def test_correct_guess_reveals_letter_and_continues(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    assert result.detail == "guess"
    text = _relay_text(fake_host)
    assert "p _ _ _ _ _" in text
    assert f"{_MAX_WRONG_GUESSES} lives left" in text
    stored = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)].decode())
    assert stored["guessed"] == ["p"]
    assert stored["wrong"] == 0


def test_incorrect_guess_costs_a_life_and_continues(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("guess", arg="z"), {}, http_client=None))
    assert result.detail == "guess"
    text = _relay_text(fake_host)
    assert "Nope, no 'z'" in text
    assert f"{_MAX_WRONG_GUESSES - 1} lives left" in text
    stored = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)].decode())
    assert stored["wrong"] == 1


def test_repeat_guess_is_a_no_op(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    stored_before = fake_host.store[_scoped(_ACTIVE_KEY)]

    result = _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    assert result.detail == "guess"
    assert "already guessed" in _relay_text(fake_host)
    assert fake_host.store[_scoped(_ACTIVE_KEY)] == stored_before


def test_guessing_every_letter_wins_and_clears_state(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "go")
    _run(dispatch(_envelope("guess", arg="g"), {}, http_client=None))
    result = _run(dispatch(_envelope("guess", arg="o"), {}, http_client=None))
    assert result.detail == "guess"
    text = _relay_text(fake_host)
    assert "You win!" in text
    assert "go" in text
    assert _scoped(_ACTIVE_KEY) not in fake_host.store


def test_running_out_of_lives_loses_and_clears_state(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "go")
    for letter in "abcdef"[:_MAX_WRONG_GUESSES]:
        result = _run(dispatch(_envelope("guess", arg=letter), {}, http_client=None))
    assert result.detail == "guess"
    text = _relay_text(fake_host)
    assert "Out of lives!" in text
    assert "go" in text
    assert _scoped(_ACTIVE_KEY) not in fake_host.store


def test_guess_with_no_active_game(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    assert result.detail == "guess"
    assert _relay_text(fake_host) == _NO_ACTIVE_GAME


@pytest.mark.parametrize("bad_letter", ["ab", "5", "", "  ", "p1"])
def test_guess_rejects_invalid_letter_input(
    bad_letter: str, fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("guess", arg=bad_letter), {}, http_client=None))
    assert result.detail == "guess"
    assert _relay_text(fake_host) == _INVALID_LETTER


def test_guess_with_missing_arg_is_treated_as_empty(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("guess"), {}, http_client=None))
    assert result.detail == "guess"
    assert _relay_text(fake_host) == _INVALID_LETTER


def test_guess_is_case_insensitive(fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("guess", arg="P"), {}, http_client=None))
    assert result.detail == "guess"
    assert "p _ _ _ _ _" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# reveal: pure reads, never mutate, never ends the game
# ---------------------------------------------------------------------------


def test_reveal_with_no_active_game(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("reveal"), {}, http_client=None))
    assert result.detail == "reveal"
    assert _relay_text(fake_host) == _NO_ACTIVE_GAME


def test_reveal_shows_mask_guessed_letters_and_lives(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    _run(dispatch(_envelope("guess", arg="z"), {}, http_client=None))
    calls_before = list(fake_host.kv_calls)

    result = _run(dispatch(_envelope("reveal"), {}, http_client=None))
    assert result.detail == "reveal"
    text = _relay_text(fake_host)
    assert "p _ _ _ _ _" in text
    assert "guessed: p, z" in text
    assert f"{_MAX_WRONG_GUESSES - 1} lives left" in text
    # reveal is a pure read: no new set/delete calls beyond what the two guesses already made
    assert not any(op in ("set", "delete") for op, *_ in fake_host.kv_calls[len(calls_before) :])


def test_bare_hangman_reveal_matches_explicit_reveal(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "python")
    result = _run(dispatch(_envelope("reveal"), {}, http_client=None))
    assert result.detail == "reveal"
    assert "lives left" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# different communities never share state
# ---------------------------------------------------------------------------


def test_different_communities_never_share_hangman_state(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: "python")
    _run(dispatch(_envelope("start", community="comm-1"), {}, http_client=None))
    _run(dispatch(_envelope("start", community="comm-2"), {}, http_client=None))

    _run(dispatch(_envelope("guess", community="comm-1", arg="p"), {}, http_client=None))
    stored_comm2 = json.loads(fake_host.store[_scoped(_ACTIVE_KEY, community="comm-2")].decode())
    assert stored_comm2["guessed"] == []


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
        _run(dispatch(_envelope("start", channel_id=None), {}, http_client=None))


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope("start", community=None), {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_on_unrecognized_action(fake_host: _FakeHost) -> None:
    envelope = _envelope("start")
    envelope.event.payload["action"] = "self-destruct"
    with pytest.raises(ValueError, match="unrecognized hangman action"):
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


def test_kv_failure_on_start_get_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="hangman kv get failed"):
        _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert any(msg == "hangman.kv_error" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_failure_on_start_set_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="hangman kv set failed"):
        _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_winning_delete_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start_with_word(fake_host, monkeypatch, "go")
    _run(dispatch(_envelope("guess", arg="g"), {}, http_client=None))
    monkeypatch.setattr(community_kv, "delete", _raise_err)
    with pytest.raises(RuntimeError, match="hangman kv delete failed"):
        _run(dispatch(_envelope("guess", arg="o"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_reveal_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="hangman kv get failed"):
        _run(dispatch(_envelope("reveal"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# corrupt state: self-heals, never crashes
# ---------------------------------------------------------------------------


def test_corrupt_active_game_is_treated_as_no_active_game(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = b"not json"

    result = _run(dispatch(_envelope("reveal"), {}, http_client=None))
    assert result.detail == "reveal"
    assert _relay_text(fake_host) == _NO_ACTIVE_GAME
    assert any(msg == "hangman.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_active_game_missing_required_fields_is_treated_as_no_active_game(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = json.dumps({"word": "python"}).encode("utf-8")

    result = _run(dispatch(_envelope("guess", arg="p"), {}, http_client=None))
    assert result.detail == "guess"
    assert _relay_text(fake_host) == _NO_ACTIVE_GAME
    assert any(msg == "hangman.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


# ---------------------------------------------------------------------------
# kv key charset -- regression: gh-631, colon-free always
# ---------------------------------------------------------------------------


def test_kv_key_builders_satisfy_host_guest_key_charset() -> None:
    validate_key(_ACTIVE_KEY)
    assert ":" not in _ACTIVE_KEY
