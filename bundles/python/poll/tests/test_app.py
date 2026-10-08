"""Host-native tests for the `poll` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/lurk/tests/test_app.py`'s own docstring for
the fake-`wit_world` approach this mirrors, extended with `fake_wit_db.FakeWitDb` (a tiny
in-memory relational engine, see that module's own docstring) instead of a fake `kv`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import fake_wit_db
import pytest
from waddle_sdk.db import DALError
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _actor_hash,
    _is_privileged,
    _map_action,
    dispatch,
    transform,
)
from waddle_sdk.command import CommandSpec, parse_command


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> fake_wit_db.FakeWitDb:
    """Install flags/relay/log/clock + the real in-memory `db` engine as one fake `wit_world`."""
    relay_calls: list[tuple[str, str]] = []
    log_calls: list[tuple[str, str, str]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: True)
    relay_mod = types.SimpleNamespace(push=lambda provider, msg: relay_calls.append((provider, msg)))
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    clock_mod = types.SimpleNamespace(
        now_millis=lambda: 1_700_000_000_000,
        now_rfc3339=lambda: "2026-10-05T00:00:00.000Z",
        monotonic_nanos=lambda: 0,
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, relay=relay_mod, log=log_mod, clock=clock_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    db = fake_wit_db.install(monkeypatch)
    db.relay_calls = relay_calls  # type: ignore[attr-defined]
    db.log_calls = log_calls  # type: ignore[attr-defined]
    return db


def _relay_calls(fake_db: fake_wit_db.FakeWitDb) -> list[tuple[str, str]]:
    return fake_db.relay_calls  # type: ignore[attr-defined,no-any-return]


def _event(text: str, *, is_mod=None, is_broadcaster=None, channel_id="12345") -> PlatformEvent:
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


def _envelope(payload: dict[str, Any], *, community: str | None = "42", actor="viewer-1") -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.poll",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )


def _relay_text(fake_db: fake_wit_db.FakeWitDb) -> str:
    """The `text` field of the most recent `relay.push()` call (JSON text -- see `relay.py`)."""
    _provider, message_json = _relay_calls(fake_db)[-1]
    text = json.loads(message_json)["text"]
    assert isinstance(text, str)
    return text


# ---------------------------------------------------------------------------
# transform: grammar mapping
# ---------------------------------------------------------------------------


def test_non_poll_text_ignored(fake_db) -> None:
    assert _run(transform(_event("hello"))) is None


def test_non_chat_payload_ignored(fake_db) -> None:
    event = PlatformEvent(platform="twitch", event_type="channel.follow", actor=None, payload={}, occurred_at="")
    assert _run(transform(event)) is None


def test_flag_disabled_suppresses_reply(monkeypatch: pytest.MonkeyPatch, fake_db) -> None:
    import wit_world

    wit_world.imports.flags = types.SimpleNamespace(enabled=lambda key, default_value: False)
    assert _run(transform(_event("!poll list"))) is None


def test_bare_poll_returns_usage(fake_db) -> None:
    result = _run(transform(_event("!poll")))
    assert "Usage" in result.payload["text"]


def test_unhandled_verb_returns_usage(fake_db) -> None:
    result = _run(transform(_event("!poll reset")))
    assert "Usage" in result.payload["text"]


def test_unrecognized_option_returns_usage_error(fake_db) -> None:
    result = _run(transform(_event("!poll bogus")))
    assert result.payload["action"] == "usage"


@pytest.mark.parametrize(
    ("text", "expected_action", "expected_args"),
    [
        ('!poll add "t" "a" "b"', "create", '"t" "a" "b"'),
        ("!poll set 5 2", "vote", "5 2"),
        ("!poll remove 5", "close", "5"),
        ("!poll list", "list", None),
        ("!poll list 5", "view", "5"),
    ],
)
def test_grammar_maps_onto_domain_actions(fake_db, text, expected_action, expected_args) -> None:
    result = _run(transform(_event(text)))
    assert result.payload["action"] == expected_action
    assert result.payload.get("args") == expected_args


def test_badge_signal_forwarded_when_present(fake_db) -> None:
    result = _run(transform(_event("!poll remove 5", is_mod=True, is_broadcaster=False)))
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_badge_signal_absent_when_not_on_event(fake_db) -> None:
    result = _run(transform(_event("!poll remove 5")))
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


def test_map_action_direct() -> None:
    spec = CommandSpec(name="poll")
    assert _map_action(parse_command("!poll add x", spec)) == ("create", "x")
    assert _map_action(parse_command("!poll", spec)) is None
    assert _map_action(parse_command("!poll enable foo", spec)) is None


# ---------------------------------------------------------------------------
# dispatch: create
# ---------------------------------------------------------------------------


def test_create_requires_privilege(fake_db) -> None:
    payload = {"action": "create", "channel_id": "12345", "args": '"t" "a" "b"'}
    result = _run(dispatch(_envelope(payload), {}, http_client=None))
    assert result.detail == "create:denied"
    assert "moderator" in _relay_text(fake_db)
    assert fake_db.tables.get("community_polls", []) == []


def test_create_inserts_poll_and_options(fake_db) -> None:
    payload = {
        "action": "create",
        "channel_id": "12345",
        "args": '"Best pizza?" "pepperoni" "mushroom"',
        "is_broadcaster": True,
    }
    result = _run(dispatch(_envelope(payload), {}, http_client=None))
    assert result.detail == "create"
    polls = fake_db.tables["community_polls"]
    assert len(polls) == 1
    assert polls[0]["community_id"] == 42
    assert polls[0]["title"] == "Best pizza?"
    assert polls[0]["created_by"] is None
    assert polls[0]["created_by_hash"] == _actor_hash("viewer-1")
    options = fake_db.tables["poll_options"]
    assert [o["option_text"] for o in options] == ["pepperoni", "mushroom"]
    assert "Poll created!" in _relay_text(fake_db)


def test_create_rejects_too_few_options(fake_db) -> None:
    payload = {"action": "create", "channel_id": "12345", "args": '"t" "a"', "is_broadcaster": True}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "at least 2 options" in _relay_text(fake_db)
    assert fake_db.tables.get("community_polls", []) == []


def test_create_parses_escaped_quotes_in_option_text(fake_db) -> None:
    payload = {
        "action": "create",
        "channel_id": "12345",
        "args": r'"title" "opt \"a\"" "optb"',
        "is_broadcaster": True,
    }
    _run(dispatch(_envelope(payload), {}, http_client=None))
    options = fake_db.tables["poll_options"]
    assert options[0]["option_text"] == 'opt "a"'


def test_create_rejects_empty_args(fake_db) -> None:
    payload = {"action": "create", "channel_id": "12345", "is_broadcaster": True}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Usage" in _relay_text(fake_db)


# ---------------------------------------------------------------------------
# dispatch: vote (incl. double-vote handling)
# ---------------------------------------------------------------------------


def _seed_poll(fake_db, *, is_active: bool = True) -> None:
    fake_db.seed(
        "community_polls",
        [
            {
                "id": 5,
                "community_id": 42,
                "title": "T",
                "is_active": is_active,
                "created_at": 1,
                "updated_at": 1,
            }
        ],
    )
    fake_db.seed(
        "poll_options",
        [
            {"id": 100, "poll_id": 5, "option_text": "A", "sort_order": 0},
            {"id": 101, "poll_id": 5, "option_text": "B", "sort_order": 1},
        ],
    )


def test_vote_records_new_vote(fake_db) -> None:
    _seed_poll(fake_db)
    payload = {"action": "vote", "channel_id": "12345", "args": "5 1"}
    result = _run(dispatch(_envelope(payload), {}, http_client=None))
    assert result.detail == "vote"
    votes = fake_db.tables["poll_votes"]
    assert len(votes) == 1
    assert votes[0] == {
        "id": 1,
        "poll_id": 5,
        "option_id": 100,
        "user_id": None,
        "ip_hash": _actor_hash("viewer-1"),
        "voted_at": "2026-10-05T00:00:00.000Z",
    }


def test_revoting_same_option_updates_not_duplicates(fake_db) -> None:
    """Double-vote handling: re-voting the SAME option updates `voted_at`, never inserts twice."""
    _seed_poll(fake_db)
    payload = {"action": "vote", "channel_id": "12345", "args": "5 1"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    _run(dispatch(_envelope(payload), {}, http_client=None))
    votes = fake_db.tables["poll_votes"]
    assert len(votes) == 1


def test_voting_different_option_adds_second_vote(fake_db) -> None:
    """Approval voting (matches source fidelity): a different option is a separate vote row."""
    _seed_poll(fake_db)
    _run(dispatch(_envelope({"action": "vote", "channel_id": "12345", "args": "5 1"}), {}, http_client=None))
    _run(dispatch(_envelope({"action": "vote", "channel_id": "12345", "args": "5 2"}), {}, http_client=None))
    votes = fake_db.tables["poll_votes"]
    assert len(votes) == 2


def test_vote_rejects_closed_poll(fake_db) -> None:
    _seed_poll(fake_db, is_active=False)
    payload = {"action": "vote", "channel_id": "12345", "args": "5 1"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "not found or is closed" in _relay_text(fake_db)
    assert fake_db.tables.get("poll_votes", []) == []


def test_vote_rejects_unknown_poll(fake_db) -> None:
    payload = {"action": "vote", "channel_id": "12345", "args": "999 1"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "not found or is closed" in _relay_text(fake_db)


def test_vote_rejects_out_of_range_option(fake_db) -> None:
    _seed_poll(fake_db)
    payload = {"action": "vote", "channel_id": "12345", "args": "5 9"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Invalid option number" in _relay_text(fake_db)


@pytest.mark.parametrize("args", [None, "5", "five 1", "5 one"])
def test_vote_rejects_invalid_input(fake_db, args) -> None:
    payload = {"action": "vote", "channel_id": "12345"}
    if args is not None:
        payload["args"] = args
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Usage" in _relay_text(fake_db)


# ---------------------------------------------------------------------------
# dispatch: close
# ---------------------------------------------------------------------------


def test_close_requires_privilege(fake_db) -> None:
    _seed_poll(fake_db)
    payload = {"action": "close", "channel_id": "12345", "args": "5"}
    result = _run(dispatch(_envelope(payload), {}, http_client=None))
    assert result.detail == "close:denied"
    assert fake_db.tables["community_polls"][0]["is_active"] is True


def test_close_deactivates_and_reports_counts(fake_db) -> None:
    _seed_poll(fake_db)
    fake_db.seed(
        "poll_votes",
        [
            {"poll_id": 5, "option_id": 100, "user_id": None, "ip_hash": "h1", "voted_at": "t"},
            {"poll_id": 5, "option_id": 100, "user_id": None, "ip_hash": "h2", "voted_at": "t"},
            {"poll_id": 5, "option_id": 101, "user_id": None, "ip_hash": "h3", "voted_at": "t"},
        ],
    )
    payload = {"action": "close", "channel_id": "12345", "args": "5", "is_broadcaster": True}
    result = _run(dispatch(_envelope(payload), {}, http_client=None))
    assert result.detail == "close"
    assert fake_db.tables["community_polls"][0]["is_active"] is False
    text = _relay_text(fake_db)
    assert "A: 2 votes" in text
    assert "B: 1 vote" in text


def test_close_rejects_unknown_poll(fake_db) -> None:
    payload = {"action": "close", "channel_id": "12345", "args": "999", "is_broadcaster": True}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "not found" in _relay_text(fake_db)


def test_close_rejects_invalid_input(fake_db) -> None:
    payload = {"action": "close", "channel_id": "12345", "args": "nope", "is_broadcaster": True}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Usage" in _relay_text(fake_db)


# ---------------------------------------------------------------------------
# dispatch: list / view
# ---------------------------------------------------------------------------


def test_list_reports_no_active_polls(fake_db) -> None:
    payload = {"action": "list", "channel_id": "12345"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "No active polls" in _relay_text(fake_db)


def test_list_shows_active_polls(fake_db) -> None:
    _seed_poll(fake_db)
    payload = {"action": "list", "channel_id": "12345"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Poll 5: T" in _relay_text(fake_db)


def test_list_excludes_other_communities(fake_db) -> None:
    fake_db.seed("community_polls", [{"id": 9, "community_id": 999, "title": "Other", "is_active": True}])
    payload = {"action": "list", "channel_id": "12345"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "No active polls" in _relay_text(fake_db)


def test_view_shows_options_and_counts(fake_db) -> None:
    _seed_poll(fake_db)
    fake_db.seed(
        "poll_votes", [{"poll_id": 5, "option_id": 100, "user_id": None, "ip_hash": "h1", "voted_at": "t"}]
    )
    payload = {"action": "view", "channel_id": "12345", "args": "5"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    text = _relay_text(fake_db)
    assert "Poll 5: T [Active]" in text
    assert "1. A (1 vote)" in text
    assert "Vote with" in text


def test_view_closed_poll_omits_vote_prompt(fake_db) -> None:
    _seed_poll(fake_db, is_active=False)
    payload = {"action": "view", "channel_id": "12345", "args": "5"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Vote with" not in _relay_text(fake_db)


def test_view_rejects_unknown_poll(fake_db) -> None:
    payload = {"action": "view", "channel_id": "12345", "args": "999"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "not found" in _relay_text(fake_db)


def test_view_rejects_invalid_input(fake_db) -> None:
    payload = {"action": "view", "channel_id": "12345", "args": "nope"}
    _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "Usage" in _relay_text(fake_db)


# ---------------------------------------------------------------------------
# dispatch: usage passthrough, errors, fail-loud
# ---------------------------------------------------------------------------


def test_usage_text_passthrough_needs_no_community(fake_db) -> None:
    payload = {"action": "usage", "channel_id": "12345", "text": "Usage: ..."}
    result = _run(dispatch(_envelope(payload, community=None), {}, http_client=None))
    assert result.detail == "usage"
    assert _relay_text(fake_db) == "Usage: ..."


def test_missing_channel_id_raises(fake_db) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope({"action": "list"}), {}, http_client=None))


def test_missing_community_raises(fake_db) -> None:
    payload = {"action": "list", "channel_id": "12345"}
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(_envelope(payload, community=None), {}, http_client=None))


def test_unrecognized_action_raises(fake_db) -> None:
    payload = {"action": "bogus", "channel_id": "12345"}
    with pytest.raises(ValueError, match="unrecognized poll action"):
        _run(dispatch(_envelope(payload), {}, http_client=None))


def test_db_failure_is_fail_loud(fake_db) -> None:
    fake_db.raise_on["SELECT * FROM community_polls"] = DALError("db.execute failed: backend down")
    payload = {"action": "list", "channel_id": "12345"}
    with pytest.raises(RuntimeError, match="poll db list failed"):
        _run(dispatch(_envelope(payload), {}, http_client=None))
    assert "temporarily unavailable" in _relay_text(fake_db)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_actor_hash_is_sha256_hex() -> None:
    assert _actor_hash("viewer-1") == hashlib.sha256(b"viewer-1").hexdigest()
    assert _actor_hash(None) == hashlib.sha256(b"anonymous").hexdigest()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"is_mod": True}, True),
        ({"is_broadcaster": True}, True),
        ({"is_mod": False, "is_broadcaster": False}, False),
        ({}, False),
    ],
)
def test_is_privileged(payload, expected, fake_db) -> None:
    assert _is_privileged(payload) is expected
