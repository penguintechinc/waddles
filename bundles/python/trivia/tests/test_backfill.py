"""Backfill coverage for the `trivia` bundle: bank integrity, TTLs, corrupt-state logs, PII logs.

Complements `test_app.py` with what it does not pin: every embedded question is well-formed and
`start` never leaks the answer, the exact TTLs (active round 1 h, scores durable), every corrupt
state context is logged at ERROR with context fields only, the leaderboard ordering/cap/zero-score
rules, the raw actor never reaching a kv key / log line / other users' replies, the flag's
fail-closed default, and the static `_entry_wiring` re-export. `trivia` has no moderator-gated verb
(anyone can start/answer/score), so there is no mod-gate matrix here.

Reuses `test_app.py`'s `fake_host` fixture (shared charset-enforcing `kv` fake + flags/relay/log).
"""

# F811: pytest fixtures re-exported from `test_app.py` are re-bound by the test parameters.
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from test_app import (  # noqa: F401 - `fake_host` is a fixture re-exported for this module
    _envelope,
    _FakeHost,
    _relay_text,
    _sample_event,
    _scoped,
    fake_host,
)

import _entry_wiring
import app
from app import (
    _ACTIVE_KEY,
    _ACTIVE_TTL_SECONDS,
    _SCORE_REGISTRY_KEY,
    _SCORE_TOP_N,
    FLAG_KEY,
    QUESTION_BANK,
    _display_handle,
    _normalize_answer,
    _pseudonym,
    _score_key,
    dispatch,
    transform,
)

CANARY = "CANARYuser9f3a"
ERROR_LEVEL = 0


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _error_logs(host: _FakeHost) -> list[dict[str, Any]]:
    return [
        {"msg": msg, **json.loads(fields)}
        for lvl, msg, fields in host.log_calls
        if lvl == ERROR_LEVEL
    ]


def _win(host: _FakeHost, actor: str, *, community: str = "comm-1") -> str:
    """Start a round and answer it correctly as `actor`; returns the reply text."""
    _run(dispatch(_envelope("start", community=community, actor="starter"), {}, http_client=None))
    active = json.loads(host.store[_scoped(_ACTIVE_KEY, community=community)])
    _run(
        dispatch(
            _envelope("answer", community=community, actor=actor, arg=active["answer_raw"]),
            {},
            http_client=None,
        )
    )
    return _relay_text(host)


# -- bank integrity
@pytest.mark.parametrize(("question", "answer"), QUESTION_BANK)
def test_every_bank_entry_is_well_formed_and_start_does_not_leak_the_answer(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch, question: str, answer: str
) -> None:
    monkeypatch.setattr(app.random, "choice", lambda _seq: (question, answer))

    _run(dispatch(_envelope("start"), {}, http_client=None))

    reply = _relay_text(fake_host)
    assert question in reply
    assert _normalize_answer(answer) not in reply.lower().replace(_normalize_answer(question), "")
    stored = json.loads(fake_host.store[_scoped(_ACTIVE_KEY)])
    assert stored == {
        "question": question,
        "answer_raw": answer,
        "answer_norm": _normalize_answer(answer),
    }
    assert question.endswith("?") and answer.strip() == answer and answer


def test_bank_has_unique_questions_and_a_non_trivial_size() -> None:
    questions = [q for q, _a in QUESTION_BANK]
    assert len(questions) == len(set(questions)) >= 8


@pytest.mark.parametrize(
    ("guess", "correct"),
    [
        ("Paris", True),
        ("  paris  ", True),
        ("PARIS", True),
        ("par  is", False),  # interior whitespace is collapsed, not removed
        ("Paris!", False),  # punctuation is not stripped (documented strictness)
        ("the paris", False),
        ("", False),
    ],
)
def test_answer_matching_is_case_and_whitespace_forgiving_but_otherwise_exact(
    fake_host: _FakeHost, monkeypatch: pytest.MonkeyPatch, guess: str, correct: bool
) -> None:
    monkeypatch.setattr(app.random, "choice", lambda _seq: ("Capital of France?", "Paris"))
    _run(dispatch(_envelope("start"), {}, http_client=None))

    _run(dispatch(_envelope("answer", arg=guess), {}, http_client=None))

    assert _relay_text(fake_host).startswith("\U0001f389 Correct") is correct
    assert (_scoped(_ACTIVE_KEY) not in fake_host.store) is correct


def test_normalize_answer_collapses_all_unicode_whitespace() -> None:
    assert _normalize_answer("  Carbon \t\n  DIOXIDE  ") == "carbon dioxide"


# -- TTL / durability
def test_active_round_expires_after_one_hour_while_scores_are_durable(fake_host: _FakeHost) -> None:
    _win(fake_host, "alice")

    sets = {args[0]: args[2] for op, args in fake_host.kv_calls if op == "set"}
    incs = {args[0]: args[2] for op, args in fake_host.kv_calls if op == "increment"}
    assert sets[_scoped(_ACTIVE_KEY)] == _ACTIVE_TTL_SECONDS == 3600
    assert sets[_scoped(_SCORE_REGISTRY_KEY)] == 0
    assert incs[_scoped(_score_key(_pseudonym("alice")))] == 0


# -- leaderboard rules
def test_top_scorers_are_capped_sorted_by_score_then_pseudonym_and_exclude_zero(
    fake_host: _FakeHost,
) -> None:
    scores = {f"player{i}": i for i in range(1, 8)}  # 1..7 points
    registry = [*(_pseudonym(p) for p in scores), _pseudonym("zero-point")]
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = json.dumps(registry).encode()
    for player, points in scores.items():
        fake_host.store[_scoped(_score_key(_pseudonym(player)))] = str(points).encode()
    fake_host.store[_scoped(_score_key(_pseudonym("zero-point")))] = b"0"

    _run(dispatch(_envelope("score", actor="viewer-1"), {}, http_client=None))

    reply = _relay_text(fake_host)
    expected = ", ".join(
        f"{_display_handle(_pseudonym(f'player{i}'))} ({i})" for i in range(7, 7 - _SCORE_TOP_N, -1)
    )
    assert reply == f"viewer-1, your score: 0. Top scorers: {expected}"
    assert _display_handle(_pseudonym("zero-point")) not in reply


def test_equal_scores_break_ties_deterministically_by_pseudonym(fake_host: _FakeHost) -> None:
    pseudonyms = sorted([_pseudonym("tie-a"), _pseudonym("tie-b")])
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = json.dumps(pseudonyms[::-1]).encode()
    for pseudonym in pseudonyms:
        fake_host.store[_scoped(_score_key(pseudonym))] = b"3"

    _run(dispatch(_envelope("score"), {}, http_client=None))

    first, second = (_display_handle(p) for p in pseudonyms)
    assert _relay_text(fake_host).endswith(f"Top scorers: {first} (3), {second} (3)")


def test_score_is_a_pure_read(fake_host: _FakeHost) -> None:
    _win(fake_host, "alice")
    fake_host.kv_calls.clear()
    _run(dispatch(_envelope("score", actor="alice"), {}, http_client=None))
    assert {op for op, _args in fake_host.kv_calls} == {"get"}


def test_other_players_are_shown_only_as_short_handles_never_their_name(
    fake_host: _FakeHost,
) -> None:
    _win(fake_host, f"{CANARY}_winner")
    _run(dispatch(_envelope("score", actor="someone-else"), {}, http_client=None))
    reply = _relay_text(fake_host)
    assert CANARY not in reply
    assert _display_handle(_pseudonym(f"{CANARY}_winner")) in reply
    assert len(_display_handle(_pseudonym("x"))) == len("user-") + 8


# -- corrupt state: every context is logged at ERROR
@pytest.mark.parametrize(
    "blob",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b'"s"',
        b"42",
        b"{}",
        json.dumps({"question": 1, "answer_raw": "a", "answer_norm": "a"}).encode(),
        json.dumps({"question": "q", "answer_raw": None, "answer_norm": "a"}).encode(),
        json.dumps({"question": "q", "answer_raw": "a", "answer_norm": ["a"]}).encode(),
    ],
)
def test_corrupt_active_state_is_logged_at_error_and_answers_are_refused(
    fake_host: _FakeHost, blob: bytes
) -> None:
    fake_host.store[_scoped(_ACTIVE_KEY)] = blob

    _run(dispatch(_envelope("answer", arg="Paris"), {}, http_client=None))

    assert (
        _relay_text(fake_host)
        == "No trivia question is active right now -- start one with !trivia start!"
    )
    assert _error_logs(fake_host) == [
        {"msg": "trivia.state_corrupt", "context": "active", "community": "comm-1"}
    ]


@pytest.mark.parametrize("blob", [b"\xff", b"nope", b'{"a": 1}', b"[1, 2]", b'["ok", 3]'])
def test_corrupt_score_registry_is_logged_at_error_and_reads_as_empty(
    fake_host: _FakeHost, blob: bytes
) -> None:
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = blob
    _run(dispatch(_envelope("score"), {}, http_client=None))
    assert _error_logs(fake_host) == [
        {"msg": "trivia.state_corrupt", "context": "score_registry", "community": "comm-1"}
    ]
    assert "No one has scored yet" in _relay_text(fake_host)


@pytest.mark.parametrize(("who", "context"), [("viewer-1", "own_score"), ("other", "other_score")])
def test_corrupt_individual_score_is_logged_with_its_context_and_reads_as_zero(
    fake_host: _FakeHost, who: str, context: str
) -> None:
    fake_host.store[_scoped(_SCORE_REGISTRY_KEY)] = json.dumps([_pseudonym(who)]).encode()
    fake_host.store[_scoped(_score_key(_pseudonym(who)))] = b"\xff-not-a-number"

    _run(dispatch(_envelope("score", actor="viewer-1"), {}, http_client=None))

    assert {
        "msg": "trivia.state_corrupt",
        "context": context,
        "community": "comm-1",
    } in _error_logs(fake_host)
    assert "your score: 0" in _relay_text(fake_host)


@pytest.mark.parametrize(
    ("kv_op", "action", "arg", "expected_op"),
    [("get", "start", None, "get"), ("set", "start", None, "set")],
)
def test_kv_backend_error_log_carries_only_op_and_exception_type(
    fake_host: _FakeHost, kv_op: str, action: str, arg: str | None, expected_op: str
) -> None:
    def _boom(*_a: Any) -> Any:
        raise RuntimeError(f"backend echoed {CANARY}")

    setattr(sys.modules["wit_world"].imports.kv, kv_op, _boom)

    with pytest.raises(RuntimeError, match=f"trivia kv {expected_op} failed: RuntimeError"):
        _run(dispatch(_envelope(action, arg=arg), {}, http_client=None))

    assert _error_logs(fake_host) == [
        {"msg": "trivia.kv_error", "op": expected_op, "error": "RuntimeError"}
    ]
    assert "temporarily unavailable" in _relay_text(fake_host)
    assert CANARY not in _relay_text(fake_host)


# -- flag fail-closed
def test_flag_is_requested_with_a_fail_closed_default(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []
    sys.modules["wit_world"].imports.flags.enabled = (
        lambda key, default_value: seen.append((key, default_value)) or default_value
    )
    assert _run(transform(_sample_event("!trivia"))) is None
    assert seen == [(FLAG_KEY, False)]
    assert FLAG_KEY == "waddles.command-trivia"


def test_missing_wit_world_keeps_the_command_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_sample_event("!trivia"))) is None


def test_flag_off_does_no_io_and_logs_nothing(fake_host: _FakeHost) -> None:
    fake_host.flag_enabled = False
    assert _run(transform(_sample_event(f"!trivia answer {CANARY}"))) is None
    assert fake_host.log_calls == [] and fake_host.kv_calls == []


# -- PII-free logs / keys
def test_raw_actor_never_reaches_a_kv_key_or_value(fake_host: _FakeHost) -> None:
    _win(fake_host, f"{CANARY}_actor")
    assert fake_host.store, "expected state to have been written (denominator must be non-zero)"
    for key, value in fake_host.store.items():
        assert CANARY.lower() not in key.lower()
        assert CANARY.encode() not in value


def test_no_log_line_in_any_flow_contains_the_guess_or_the_raw_actor(fake_host: _FakeHost) -> None:
    """PII-free-log regression: typed guesses, grammar text and the raw actor never log."""
    actor = f"{CANARY}_actor"
    _run(dispatch(_envelope("start", actor=actor), {}, http_client=None))
    for guess in (f"{CANARY} wrong guess", ""):
        _run(dispatch(_envelope("answer", actor=actor, arg=guess), {}, http_client=None))
    _win(fake_host, actor)
    _run(dispatch(_envelope("score", actor=actor), {}, http_client=None))
    for text in (
        f"!trivia answer {CANARY}",
        f"!trivia {CANARY}",
        "!trivia answer",
        "!trivia start",
        "!trivia score",
    ):
        _run(transform(_sample_event(text, actor=actor)))
    # corrupt-state logging paths must be PII-free too
    fake_host.store[_scoped(_ACTIVE_KEY)] = b"garbage"
    _run(dispatch(_envelope("answer", actor=actor, arg=CANARY), {}, http_client=None))

    assert fake_host.log_calls, "expected log lines (denominator must be non-zero)"
    for _lvl, message, fields_json in fake_host.log_calls:
        assert CANARY.lower() not in message.lower()
        assert CANARY.lower() not in fields_json.lower()


def test_replies_go_to_the_events_own_origin_platform(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_envelope("score", platform="discord"), {}, http_client=None))
    assert result.transport == "discord"
    assert fake_host.relay_calls[-1][0] == "discord"


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
