"""Host-native tests for the `trivia` bundle's stateful `transform`/`dispatch` logic.

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
    _NO_ACTIVE_QUESTION,
    _SCORE_REGISTRY_KEY,
    _USAGE,
    QUESTION_BANK,
    _display_handle,
    _pseudonym,
    _score_key,
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
        app_id="waddles.core.example.trivia",
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


@pytest.mark.parametrize("text", ["!trivia", "!TRIVIA", "  !trivia  "])
def test_bare_trivia_maps_to_score_read(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == "score"


@pytest.mark.parametrize(
    ("text", "expected_action"),
    [("!trivia start", "start"), ("!trivia START", "start"), ("!trivia score", "score")],
)
def test_transform_parses_declared_sub_modules(
    text: str, expected_action: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["action"] == expected_action


def test_transform_parses_answer_with_free_text(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!trivia answer Paris France")))
    assert result is not None
    assert result.payload["action"] == "answer"
    assert result.payload["arg"] == "Paris France"


def test_transform_answer_with_no_text_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!trivia answer")))
    assert result is not None
    assert result.payload["action"] == "usage"
    assert result.payload["arg"] == _USAGE


def test_valid_grammar_verb_with_no_meaning_for_trivia_returns_usage(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!trivia list")))
    assert result is not None
    assert result.payload["action"] == "usage"


def test_unknown_token_returns_usage_action(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!trivia bogus")))
    assert result is not None
    assert result.payload["action"] == "usage"


@pytest.mark.parametrize("text", ["!triviality", "trivia", "!triviaed", "hello", ""])
def test_non_matching_text_produces_no_reply(text: str, fake_host: _FakeHost) -> None:
    assert _run(transform(_sample_event(text))) is None


def test_non_chat_payload_is_ignored_rather_than_erroring(fake_host: _FakeHost) -> None:
    event = PlatformEvent(
        platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at=""
    )
    assert _run(transform(event)) is None


def test_disabled_flag_suppresses_every_reply(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event("!trivia start"))) is None
    assert _run(transform(_sample_event("!trivia answer Paris"))) is None


# ---------------------------------------------------------------------------
# start: poses a question, never overwrites an in-progress round
# ---------------------------------------------------------------------------


def test_start_poses_one_of_the_embedded_questions(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("start"), {}, http_client=None))
    assert result.detail == "start"
    text = _relay_text(fake_host)
    assert any(question in text for question, _answer in QUESTION_BANK)


def test_start_again_does_not_overwrite_the_active_question(fake_host: _FakeHost) -> None:
    _run(dispatch(_envelope("start"), {}, http_client=None))
    first_active = fake_host.store[_scoped(_ACTIVE_KEY)]

    result = _run(dispatch(_envelope("start"), {}, http_client=None))
    assert result.detail == "start"
    assert "already active" in _relay_text(fake_host)
    assert fake_host.store[_scoped(_ACTIVE_KEY)] == first_active


# ---------------------------------------------------------------------------
# answer: win / incorrect / no-active-game / invalid-input
# ---------------------------------------------------------------------------


def test_correct_answer_wins_increments_score_and_ends_round(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))

    result = _run(
        dispatch(_envelope("answer", actor="viewer-1", arg="Paris"), {}, http_client=None)
    )
    assert result.detail == "answer"
    text = _relay_text(fake_host)
    assert "Correct, viewer-1" in text
    assert "Your score: 1" in text
    assert _scoped(_ACTIVE_KEY) not in fake_host.store


def test_correct_answer_is_case_and_whitespace_insensitive(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))
    result = _run(
        dispatch(_envelope("answer", actor="viewer-1", arg="  paRIS  "), {}, http_client=None)
    )
    assert result.detail == "answer"
    assert "Correct, viewer-1" in _relay_text(fake_host)


def test_incorrect_answer_does_not_end_the_round_or_score(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))

    result = _run(
        dispatch(_envelope("answer", actor="viewer-1", arg="London"), {}, http_client=None)
    )
    assert result.detail == "answer"
    assert _relay_text(fake_host) == "Not quite -- try again!"
    assert _scoped(_ACTIVE_KEY) in fake_host.store
    assert _scoped(_score_key(_pseudonym("viewer-1"))) not in fake_host.store


def test_second_correct_answerer_loses_to_the_first(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))
    _run(dispatch(_envelope("answer", actor="viewer-1", arg="Paris"), {}, http_client=None))

    # round already ended -- viewer-2's later correct-shaped guess has nothing active to match
    result = _run(
        dispatch(_envelope("answer", actor="viewer-2", arg="Paris"), {}, http_client=None)
    )
    assert result.detail == "answer"
    assert _relay_text(fake_host) == _NO_ACTIVE_QUESTION


def test_answer_with_no_active_question(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("answer", arg="Paris"), {}, http_client=None))
    assert result.detail == "answer"
    assert _relay_text(fake_host) == _NO_ACTIVE_QUESTION


def test_answer_action_with_missing_arg_is_treated_as_empty(fake_host: _FakeHost) -> None:
    envelope = _envelope("answer")
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "answer"
    assert _relay_text(fake_host) == _NO_ACTIVE_QUESTION


# ---------------------------------------------------------------------------
# score: pure reads, never mutate
# ---------------------------------------------------------------------------


def test_score_before_any_correct_answer(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("score", actor="viewer-1"), {}, http_client=None))
    assert result.detail == "score"
    text = _relay_text(fake_host)
    assert "your score: 0" in text
    assert "No one has scored yet" in text
    assert not any(op in ("set", "increment") for op, *_ in fake_host.kv_calls)


def test_score_reports_own_score_and_top_scorers(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))
    _run(dispatch(_envelope("answer", actor="viewer-1", arg="Paris"), {}, http_client=None))

    result = _run(dispatch(_envelope("score", actor="viewer-1"), {}, http_client=None))
    assert result.detail == "score"
    text = _relay_text(fake_host)
    handle = _display_handle(_pseudonym("viewer-1"))
    assert "your score: 1" in text
    assert f"{handle} (1)" in text


def test_different_communities_never_share_trivia_state(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start", community="comm-1"), {}, http_client=None))
    _run(dispatch(_envelope("start", community="comm-2"), {}, http_client=None))

    _run(
        dispatch(
            _envelope("answer", community="comm-1", actor="viewer-1", arg="Paris"),
            {},
            http_client=None,
        )
    )
    # comm-2's round is untouched by comm-1's correct answer
    assert _scoped(_ACTIVE_KEY, community="comm-2") in fake_host.store
    result = _run(
        dispatch(_envelope("score", community="comm-2", actor="viewer-1"), {}, http_client=None)
    )
    assert "your score: 0" in _relay_text(fake_host)
    assert result.detail == "score"


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
    with pytest.raises(ValueError, match="unrecognized trivia action"):
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
    with pytest.raises(RuntimeError, match="trivia kv get failed"):
        _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert any(msg == "trivia.kv_error" for _lvl, msg, _fields in fake_host.log_calls)


def test_kv_failure_on_start_set_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "set", _raise_err)
    with pytest.raises(RuntimeError, match="trivia kv set failed"):
        _run(dispatch(_envelope("start"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_answer_delete_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(dispatch(_envelope("start"), {}, http_client=None))
    monkeypatch.setattr(community_kv, "delete", _raise_err)
    active = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)].decode())
    with pytest.raises(RuntimeError, match="trivia kv delete failed"):
        _run(
            dispatch(
                _envelope("answer", actor="viewer-1", arg=active["answer_raw"]),
                {},
                http_client=None,
            )
        )
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_answer_increment_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(dispatch(_envelope("start"), {}, http_client=None))
    active = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)].decode())
    monkeypatch.setattr(community_kv, "increment", _raise_err)
    with pytest.raises(RuntimeError, match="trivia kv increment failed"):
        _run(
            dispatch(
                _envelope("answer", actor="viewer-1", arg=active["answer_raw"]),
                {},
                http_client=None,
            )
        )
    assert "temporarily unavailable" in _relay_text(fake_host)


def test_kv_failure_on_score_produces_error_reply_and_raises(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(community_kv, "get", _raise_err)
    with pytest.raises(RuntimeError, match="trivia kv get failed"):
        _run(dispatch(_envelope("score"), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_host)


# ---------------------------------------------------------------------------
# corrupt state: self-heals, never crashes
# ---------------------------------------------------------------------------


def test_corrupt_active_question_is_treated_as_no_active_game(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = b"not json"

    result = _run(dispatch(_envelope("answer", arg="Paris"), {}, http_client=None))
    assert result.detail == "answer"
    assert _relay_text(fake_host) == _NO_ACTIVE_QUESTION
    assert any(msg == "trivia.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_active_question_missing_required_fields_is_treated_as_no_active_game(
    fake_host: _FakeHost,
) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = json.dumps({"question": "incomplete"}).encode("utf-8")

    result = _run(dispatch(_envelope("answer", arg="Paris"), {}, http_client=None))
    assert result.detail == "answer"
    assert _relay_text(fake_host) == _NO_ACTIVE_QUESTION
    assert any(msg == "trivia.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_winning_twice_does_not_duplicate_the_score_registry(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.random.choice", lambda seq: seq[0])
    _run(dispatch(_envelope("start"), {}, http_client=None))
    _run(dispatch(_envelope("answer", actor="viewer-1", arg="Paris"), {}, http_client=None))
    _run(dispatch(_envelope("start"), {}, http_client=None))
    _run(dispatch(_envelope("answer", actor="viewer-1", arg="Paris"), {}, http_client=None))

    registry = json.loads(fake_host.store[_scoped(_SCORE_REGISTRY_KEY)].decode())
    assert registry == [_pseudonym("viewer-1")]


def test_corrupt_score_registry_is_treated_as_empty(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = b"not json"

    result = _run(dispatch(_envelope("score"), {}, http_client=None))
    assert result.detail == "score"
    assert "No one has scored yet" in _relay_text(fake_host)
    assert any(msg == "trivia.state_corrupt" for _lvl, msg, _fields in fake_host.log_calls)


def test_corrupt_own_score_reads_as_zero(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_score_key(_pseudonym("viewer-1")))] = b"not-a-number"

    result = _run(dispatch(_envelope("score", actor="viewer-1"), {}, http_client=None))
    assert "your score: 0" in _relay_text(fake_host)
    assert result.detail == "score"


def test_score_registry_not_a_list_of_strings_is_treated_as_empty(fake_host: _FakeHost) -> None:
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = json.dumps([1, 2, 3]).encode("utf-8")

    result = _run(dispatch(_envelope("score"), {}, http_client=None))
    assert "No one has scored yet" in _relay_text(fake_host)
    assert result.detail == "score"


# ---------------------------------------------------------------------------
# kv key charset -- regression: gh-631, colon-free always
# ---------------------------------------------------------------------------


def test_kv_key_builders_satisfy_host_guest_key_charset() -> None:
    keys = (_ACTIVE_KEY, _SCORE_REGISTRY_KEY, _score_key(_pseudonym("viewer-1")))
    for key in keys:
        validate_key(key)
        assert ":" not in key
