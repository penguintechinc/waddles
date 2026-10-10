"""Host-native tests for the `poll` bundle (structured `db` + `community_kv`, v2).

No WASM/wasmtime here -- the real `waddle_sdk` (`db`, `kv`, `community_kv`, `command`, `log`,
`relay`) runs unmodified over a fake `wit_world` host: `waddle_sdk.testing.install_fake_kv_host`
(charset-validating `kv`, gh-631) plus `wit_fake_db.FakeDb` (in-memory structured `db`) and
small flags/relay/log fakes. Every scenario goes through the public `transform` -> `dispatch`
path a real chat message takes.

Regression: gh-675 (same defect class -- the retired `create_dal`/`TableProxy` facade) -- the
v1 build did `from waddle_sdk.db import DALError, create_dal`, which raised `ImportError` at
import time, so the bundle could neither load nor build. `TestModuleLoads` pins that.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import wit_fake_db
from waddle_sdk import db as sdk_db
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

import app
from app import (
    MAX_OPTION_LEN,
    MAX_OPTIONS,
    MAX_TITLE_LEN,
    _actor_hash,
    _is_privileged,
    _map_action,
    _parse_quoted_args,
    dispatch,
    transform,
)

_COMMUNITY = "comm-1"
_ACTOR = "viewer-1"
#: Host limit per invocation for both `kv` and `db` (`MAX_OPS_PER_INVOKE`).
_OPS_BUDGET = 64


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Host:
    """Everything the fake host records: kv, db, relay, log, plus the flag switch."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.db = wit_fake_db.FakeDb()
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []
        self.flag_enabled = True
        self.wit_world: Any = None

    def replies(self) -> list[str]:
        return [str(json.loads(msg)["text"]) for _prov, msg in self.relay_calls]

    def last_reply(self) -> str:
        return self.replies()[-1]

    def scoped_kv(self, key: str, community: str | None = _COMMUNITY) -> bytes | None:
        return self.kv.store.get(_scoped_key(community, key))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    fake_kv = install_fake_kv_host(monkeypatch)
    h = _Host(fake_kv)
    wit_world = sys.modules["wit_world"]
    h.wit_world = wit_world
    wit_world.imports.flags = types.SimpleNamespace(  # type: ignore[attr-defined]
        enabled=lambda key, default_value: h.flag_enabled
    )
    wit_world.imports.relay = types.SimpleNamespace(  # type: ignore[attr-defined]
        push=lambda provider, msg: h.relay_calls.append((provider, msg))
    )
    wit_world.imports.log = types.SimpleNamespace(  # type: ignore[attr-defined]
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: h.log_calls.append((lvl, msg, fields_json)),
    )
    wit_world.imports.db = types.SimpleNamespace(  # type: ignore[attr-defined]
        insert=h.db.insert,
        get=h.db.get,
        query=h.db.query,
        update=h.db.update,
        delete=h.db.delete,
        ColumnValue=wit_fake_db.ColumnValue,
        OrderColumn=wit_fake_db.OrderColumn,
        OrderBy_Column=wit_fake_db.OrderBy_Column,
        OrderBy_Random=wit_fake_db.OrderBy_Random,
        Value_NullValue=wit_fake_db.Value_NullValue,
        Value_BoolValue=wit_fake_db.Value_BoolValue,
        Value_IntValue=wit_fake_db.Value_IntValue,
        Value_FloatValue=wit_fake_db.Value_FloatValue,
        Value_TextValue=wit_fake_db.Value_TextValue,
        Value_BytesValue=wit_fake_db.Value_BytesValue,
        Row=wit_fake_db.Row,
    )
    return h


def _event(
    text: str,
    *,
    actor: str | None = _ACTOR,
    is_mod: Any = None,
    is_broadcaster: Any = None,
    channel_id: str | None = "12345",
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


def _envelope(event: PlatformEvent, *, community: str | None = _COMMUNITY) -> StageEnvelope:
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.poll",
        stage="action",
        event=event,
        ts="2026-10-05T00:00:00.000Z",
    )


def say(
    host: _Host,
    text: str,
    *,
    actor: str | None = _ACTOR,
    mod: bool = False,
    community: str | None = _COMMUNITY,
) -> str:
    """Send one chat line through the real `transform` -> `dispatch` path; return the reply."""
    event = _event(text, actor=actor, is_mod=mod, is_broadcaster=False)
    transformed = _run(transform(event))
    assert transformed is not None, f"{text!r} produced no reply"
    before = len(host.relay_calls)
    kv_before, db_before = len(host.kv.calls), len(host.db.calls)
    _run(dispatch(_envelope(transformed, community=community), {}, http_client=None))
    assert len(host.relay_calls) == before + 1, "exactly one relay per command"
    assert len(host.kv.calls) - kv_before <= _OPS_BUDGET, "kv ops exceed host per-invoke budget"
    assert len(host.db.calls) - db_before <= _OPS_BUDGET, "db ops exceed host per-invoke budget"
    return host.last_reply()


def create(host: _Host, title: str = "Best snack?", *opts: str, actor: str = "mod-1") -> int:
    options = opts or ("Tacos", "Pizza", "Sushi")
    quoted = " ".join(f'"{o}"' for o in (title, *options))
    reply = say(host, f"!poll add {quoted}", actor=actor, mod=True)
    assert reply.startswith("Poll created! ID: "), reply
    return int(reply.split("ID: ")[1].split("\n")[0])


# regression: the bundle loads (gh-675 class)


class TestModuleLoads:
    def test_module_exposes_entrypoints(self) -> None:
        import _entry_wiring

        assert _entry_wiring.bundle_transform is transform
        assert _entry_wiring.bundle_dispatch is dispatch

    def test_every_sdk_db_name_the_bundle_uses_exists(self) -> None:
        """Static guard: no retired `waddle_sdk.db` symbol (`create_dal`...) can come back."""
        tree = ast.parse(Path(app.__file__).read_text())
        sdk_names = set(dir(sdk_db))
        imported: set[str] = set()
        accessed: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "waddle_sdk.db":
                imported |= {alias.name for alias in node.names}
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "db"
            ):
                accessed.add(node.attr)
        assert accessed, "bundle must use the structured db facade (denominator)"
        assert not (imported | accessed) - sdk_names, (imported | accessed) - sdk_names
        assert "create_dal" not in (imported | accessed)

    def test_uses_only_the_structured_db_ops(self) -> None:
        tree = ast.parse(Path(app.__file__).read_text())
        used = {
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "db"
        }
        assert used <= {"insert", "get", "query", "update", "delete", "ConflictError"}


# transform


class TestTransform:
    @pytest.mark.parametrize("text", ["hello", "!pollx", "poll", "", "!pol", "x !poll"])
    def test_non_matching_text_is_ignored(self, host: _Host, text: str) -> None:
        assert _run(transform(_event(text))) is None

    def test_non_string_text_is_ignored(self, host: _Host) -> None:
        event = _event("x")
        event.payload["text"] = 5
        assert _run(transform(event)) is None

    def test_flag_off_suppresses_everything(self, host: _Host) -> None:
        host.flag_enabled = False
        assert _run(transform(_event('!poll add "t" "a" "b"', is_mod=True))) is None

    @pytest.mark.parametrize(
        ("text", "action"),
        [
            ('!poll add "t" "a" "b"', "create"),
            ("!poll set 1 2", "vote"),
            ("!poll remove 1", "close"),
            ("!poll list", "list"),
            ("!poll list 7", "view"),
            ("  !POLL   list  ", "list"),
        ],
    )
    def test_maps_grammar_onto_actions(self, host: _Host, text: str, action: str) -> None:
        out = _run(transform(_event(text)))
        assert out is not None
        assert out.payload["action"] == action

    @pytest.mark.parametrize("text", ["!poll", "!poll reset", "!poll enable x", "!poll sub 1"])
    def test_grammar_valid_but_unhandled_verbs_reply_usage(self, host: _Host, text: str) -> None:
        out = _run(transform(_event(text)))
        assert out is not None
        assert out.payload["action"] == "usage"
        assert out.payload["text"] == app._USAGE

    def test_unknown_option_replies_usage_error(self, host: _Host) -> None:
        out = _run(transform(_event("!poll frobnicate")))
        assert out is not None
        assert out.payload["action"] == "usage"
        assert "unknown option" in out.payload["text"]

    def test_badge_forwarding_is_strict_boolean(self, host: _Host) -> None:
        out = _run(transform(_event("!poll list", is_mod="false", is_broadcaster="true")))
        assert out is not None
        assert out.payload["is_mod"] is False
        assert out.payload["is_broadcaster"] is False

    def test_real_booleans_are_forwarded_and_absence_stays_absent(self, host: _Host) -> None:
        out = _run(transform(_event("!poll list", is_mod=True)))
        assert out is not None
        assert out.payload["is_mod"] is True
        assert "is_broadcaster" not in out.payload

    def test_map_action_none_for_bare_poll(self) -> None:
        from waddle_sdk.command import ParsedCommand

        assert _map_action(ParsedCommand("poll", None, None, None)) is None


# permissions: the mod-gate bypass regression


class TestModGate:
    """Regression: `bool("false")` is `True`, so a string badge let non-mods create/close."""

    @pytest.mark.parametrize("badge", ["false", "False", "0", "no", "", "true", "1", 0, 1, None])
    def test_non_boolean_badges_never_pass_the_gate(self, host: _Host, badge: Any) -> None:
        assert _is_privileged({"is_mod": badge, "is_broadcaster": badge}) is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"is_mod": "false", "is_broadcaster": False},
            {"is_mod": False, "is_broadcaster": "false"},
            {"is_mod": "0", "is_broadcaster": False},
            {"is_mod": False, "is_broadcaster": "no"},
        ],
    )
    def test_one_string_badge_beside_a_real_false_is_still_a_denial(
        self, host: _Host, payload: dict[str, Any]
    ) -> None:
        """The exact bypass shape.

        The isinstance guard passes (one real bool), then `bool("false")` used to be `True`.
        """
        assert _is_privileged(payload) is False

    def test_string_false_mod_is_rejected_end_to_end(self, host: _Host) -> None:
        event = _event('!poll add "t" "a" "b"', is_mod="false", is_broadcaster="false")
        out = _run(transform(event))
        assert out is not None
        _run(dispatch(_envelope(out), {}, http_client=None))
        assert host.last_reply() == app._PERMISSION_DENIED_MSG
        assert host.db.rows == {}, "a rejected create must not write"

    def test_string_badge_in_dispatch_payload_is_rejected_too(self, host: _Host) -> None:
        """Defense in depth: a dispatch payload that bypassed `transform` is judged strictly too."""
        pid = create(host)
        raw = _event("x", is_mod="false")
        raw.payload.update({"action": "close", "args": str(pid)})
        del raw.payload["text"]
        _run(dispatch(_envelope(raw), {}, http_client=None))
        assert host.last_reply() == app._PERMISSION_DENIED_MSG
        assert _only_row(host)["is_active"] is True

    def test_non_mod_cannot_create_or_close(self, host: _Host) -> None:
        assert say(host, '!poll add "t" "a" "b"', mod=False) == app._PERMISSION_DENIED_MSG
        pid = create(host)
        assert say(host, f"!poll remove {pid}", mod=False) == app._PERMISSION_DENIED_MSG
        assert _only_row(host)["is_active"] is True

    def test_no_role_info_fails_closed(self, host: _Host) -> None:
        out = _run(transform(_event('!poll add "t" "a" "b"')))
        assert out is not None
        assert "is_mod" not in out.payload
        _run(dispatch(_envelope(out), {}, http_client=None))
        assert host.last_reply() == app._PERMISSION_DENIED_MSG

    def test_broadcaster_without_mod_is_allowed(self, host: _Host) -> None:
        event = _event('!poll add "t" "a" "b"', is_mod=False, is_broadcaster=True)
        out = _run(transform(event))
        assert out is not None
        _run(dispatch(_envelope(out), {}, http_client=None))
        assert host.last_reply().startswith("Poll created!")

    def test_mod_is_allowed(self, host: _Host) -> None:
        assert create(host) == 1

    def test_voting_and_reading_need_no_role(self, host: _Host) -> None:
        pid = create(host)
        assert "Vote recorded" in say(host, f"!poll set {pid} 1", mod=False)
        assert "Active polls" in say(host, "!poll list", mod=False)
        assert "[Active]" in say(host, f"!poll list {pid}", mod=False)


def _only_row(host: _Host) -> dict[str, Any]:
    assert len(host.db.rows) == 1
    return next(iter(host.db.rows.values()))


# create / vote / close lifecycle (real paths)


class TestLifecycle:
    def test_create_vote_view_close_end_to_end(self, host: _Host) -> None:
        pid = create(host, "Best snack?", "Tacos", "Pizza", "Sushi")
        assert pid == 1
        created = host.replies()[-1]
        assert "Title: Best snack?" in created
        assert "  1. Tacos" in created and "  3. Sushi" in created
        assert f"!poll set {pid} <option_number>" in created

        assert "Vote recorded for option 2" in say(host, f"!poll set {pid} 2", actor="alice")
        assert "Vote recorded for option 2" in say(host, f"!poll set {pid} 2", actor="bob")
        assert "Vote recorded for option 1" in say(host, f"!poll set {pid} 1", actor="carol")

        view = say(host, f"!poll list {pid}")
        assert "Poll 1: Best snack? [Active]" in view
        assert "1. Tacos (1 vote)" in view
        assert "2. Pizza (2 votes)" in view
        assert "3. Sushi (0 votes)" in view

        closed = say(host, f"!poll remove {pid}", mod=True)
        assert closed.startswith("Poll 1 closed: Best snack?")
        assert "Pizza: " not in closed  # format is "N. Pizza (2 votes)"
        assert "2. Pizza (2 votes)" in closed
        row = _only_row(host)
        assert row["is_active"] is False
        assert json.loads(row["results"]) == [1, 2, 0]

        assert say(host, f"!poll set {pid} 1", actor="dave") == (
            f"Poll {pid} not found or is closed."
        )
        final = say(host, f"!poll list {pid}")
        assert "[Closed]" in final and "2. Pizza (2 votes)" in final
        assert "Vote with" not in final

    def test_closed_results_survive_kv_tally_expiry(self, host: _Host) -> None:
        pid = create(host)
        say(host, f"!poll set {pid} 1", actor="alice")
        say(host, f"!poll remove {pid}", mod=True)
        for key in [k for k in host.kv.store if ".tally." in k]:
            del host.kv.store[key]  # simulate the 30-day TTL lapsing
        assert "1. Tacos (1 vote)" in say(host, f"!poll list {pid}")

    def test_closing_twice_is_idempotent_and_reports_snapshot(self, host: _Host) -> None:
        pid = create(host)
        say(host, f"!poll set {pid} 3", actor="alice")
        say(host, f"!poll remove {pid}", mod=True)
        again = say(host, f"!poll remove {pid}", mod=True)
        assert again.startswith(f"Poll {pid} is already closed")
        assert "3. Sushi (1 vote)" in again
        assert [c[0] for c in host.db.calls].count("update") == 1

    def test_approval_voting_allows_several_options_but_never_double_counts(
        self, host: _Host
    ) -> None:
        pid = create(host)
        assert "Vote recorded" in say(host, f"!poll set {pid} 1", actor="alice")
        assert "Vote recorded" in say(host, f"!poll set {pid} 2", actor="alice")
        dup = say(host, f"!poll set {pid} 1", actor="alice")
        assert dup == f"You already voted for option 1 on poll {pid}."
        view = say(host, f"!poll list {pid}")
        assert "1. Tacos (1 vote)" in view and "2. Pizza (1 vote)" in view

    def test_voter_identity_is_case_and_whitespace_insensitive(self, host: _Host) -> None:
        pid = create(host)
        say(host, f"!poll set {pid} 1", actor="Alice")
        assert "already voted" in say(host, f"!poll set {pid} 1", actor="  alice ")

    def test_poll_ids_are_sequential_per_community(self, host: _Host) -> None:
        assert create(host) == 1
        assert create(host, "Second?", "x", "y") == 2

    def test_communities_are_isolated(self, host: _Host) -> None:
        pid = create(host)
        say(host, f"!poll set {pid} 1", actor="alice")
        other = say(host, f"!poll list {pid}", community="comm-2")
        assert other == f"Poll {pid} not found."

    def test_list_shows_only_active_newest_first_capped(self, host: _Host) -> None:
        ids = [create(host, f"Q{i}", "a", "b") for i in range(12)]
        say(host, f"!poll remove {ids[11]}", mod=True)
        reply = say(host, "!poll list")
        lines = [ln for ln in reply.splitlines() if ln.startswith("  - Poll")]
        assert len(lines) == 10
        assert lines[0].startswith("  - Poll 11:")  # newest ACTIVE first (12 was closed)
        assert "Poll 12:" not in reply

    def test_list_with_no_active_polls(self, host: _Host) -> None:
        assert say(host, "!poll list") == "No active polls in this community."
        pid = create(host)
        say(host, f"!poll remove {pid}", mod=True)
        assert say(host, "!poll list") == "No active polls in this community."

    def test_view_unknown_poll(self, host: _Host) -> None:
        assert say(host, "!poll list 99") == "Poll 99 not found."

    def test_close_unknown_poll(self, host: _Host) -> None:
        assert say(host, "!poll remove 99", mod=True) == "Poll 99 not found."

    def test_created_poll_is_attributed_by_hash_not_raw_actor(self, host: _Host) -> None:
        create(host, actor="Mod-Person")
        row = _only_row(host)
        assert row["created_by_hash"] == _actor_hash("mod-person")
        assert len(row["created_by_hash"]) == 64
        assert "mod-person" not in json.dumps(row).lower()


# input validation


class TestValidation:
    def test_create_without_args_is_usage(self, host: _Host) -> None:
        assert say(host, "!poll add", mod=True).startswith("Usage: !poll add")
        assert host.db.rows == {}

    def test_create_needs_title_and_two_options(self, host: _Host) -> None:
        reply = say(host, '!poll add "title" "only-one"', mod=True)
        assert reply == "Poll must have a title and at least 2 options."
        assert host.db.rows == {}

    def test_create_rejects_empty_pieces(self, host: _Host) -> None:
        assert say(host, '!poll add "   " "a" "b"', mod=True) == (
            "Poll title and options cannot be empty."
        )
        assert say(host, '!poll add "t" "a" "  "', mod=True) == (
            "Poll title and options cannot be empty."
        )

    def test_create_rejects_oversized_title_and_option(self, host: _Host) -> None:
        long_title = "x" * (MAX_TITLE_LEN + 1)
        assert "characters or fewer" in say(host, f'!poll add "{long_title}" "a" "b"', mod=True)
        long_opt = "y" * (MAX_OPTION_LEN + 1)
        assert "characters or fewer" in say(host, f'!poll add "t" "{long_opt}" "b"', mod=True)
        assert host.db.rows == {}

    def test_create_accepts_values_exactly_at_the_bounds(self, host: _Host) -> None:
        title, opt = "x" * MAX_TITLE_LEN, "y" * MAX_OPTION_LEN
        opts = [f"{opt[:-2]}{i:02d}" for i in range(MAX_OPTIONS)]
        quoted = " ".join(f'"{o}"' for o in (title, *opts))
        assert say(host, f"!poll add {quoted}", mod=True).startswith("Poll created!")

    def test_create_rejects_too_many_options(self, host: _Host) -> None:
        opts = " ".join(f'"o{i}"' for i in range(MAX_OPTIONS + 1))
        assert say(host, f'!poll add "t" {opts}', mod=True) == (
            f"Polls support at most {MAX_OPTIONS} options."
        )

    def test_create_rejects_duplicate_options_case_insensitively(self, host: _Host) -> None:
        assert say(host, '!poll add "t" "Tacos" "tacos"', mod=True) == (
            "Poll options must be unique."
        )

    @pytest.mark.parametrize(
        "args",
        ["", "5", "abc 1", "1 abc", "1 2 3", "-1 2", "1.5 2", "٣ ١", "1_0 2", "9999999999 1"],
    )
    def test_vote_rejects_malformed_numbers(self, host: _Host, args: str) -> None:
        pid = create(host)
        assert pid == 1
        reply = say(host, f"!poll set {args}".strip())
        assert reply.startswith("Usage: !poll set")

    @pytest.mark.parametrize("option", [0, 4, 999])
    def test_vote_rejects_out_of_range_option(self, host: _Host, option: int) -> None:
        pid = create(host)
        reply = say(host, f"!poll set {pid} {option}")
        assert reply == f"Invalid option number. Poll {pid} has 3 options."
        assert not any(".tally." in k for k in host.kv.store)

    def test_vote_on_missing_poll(self, host: _Host) -> None:
        assert say(host, "!poll set 42 1") == "Poll 42 not found or is closed."

    @pytest.mark.parametrize("args", ["", "x", "-3", "1 2", "٣"])
    def test_close_and_view_reject_bad_ids(self, host: _Host, args: str) -> None:
        close = say(host, f"!poll remove {args}".strip(), mod=True)
        assert close.startswith("Usage: !poll remove")
        if args:
            assert say(host, f"!poll list {args}").startswith("Usage: !poll list")

    def test_parse_quoted_args_handles_quotes_and_escapes(self) -> None:
        assert _parse_quoted_args('"a b" c\t"d\\"e"') == ["a b", "c", 'd"e']
        assert _parse_quoted_args("") == []
        assert _parse_quoted_args("a    b \t c") == ["a", "b", "c"], "runs of whitespace collapse"
        assert _parse_quoted_args('"unterminated tail') == ["unterminated tail"]


# fail-loud + compensation


def _fail_kv(host: _Host, op: str, key_contains: str = "") -> None:
    """Make `wit_world.imports.kv.<op>` raise a WIT-shaped backend error for matching keys."""
    kv_mod = host.wit_world.imports.kv
    original = getattr(kv_mod, op)

    def _wrapped(key: str, *a: Any, **k: Any) -> Any:
        if key_contains in key:
            raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("boom"))
        return original(key, *a, **k)

    setattr(kv_mod, op, _wrapped)


def _dispatch_raw(host: _Host, text: str, *, mod: bool = True, actor: str = _ACTOR) -> None:
    out = _run(transform(_event(text, actor=actor, is_mod=mod, is_broadcaster=False)))
    assert out is not None
    _run(dispatch(_envelope(out), {}, http_client=None))


class TestFailLoud:
    def _assert_loud(self, host: _Host, op: str) -> None:
        errors = [(m, json.loads(f)) for lvl, m, f in host.log_calls if lvl == 0]
        assert any(m == "poll.backend_error" and f["op"] == op for m, f in errors), errors
        assert host.last_reply() == app._UNAVAILABLE_MSG

    def test_seq_increment_failure(self, host: _Host) -> None:
        _fail_kv(host, "increment", "poll.seq")
        with pytest.raises(RuntimeError, match="poll kv_increment failed"):
            _dispatch_raw(host, '!poll add "t" "a" "b"')
        self._assert_loud(host, "kv_increment")
        assert host.db.rows == {}

    def test_db_insert_failure(self, host: _Host) -> None:
        host.db.raise_on["insert"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("x"))
        with pytest.raises(RuntimeError, match="poll db_insert failed"):
            _dispatch_raw(host, '!poll add "t" "a" "b"')
        self._assert_loud(host, "db_insert")

    def test_index_write_failure_discards_the_orphan_row(self, host: _Host) -> None:
        _fail_kv(host, "set", "poll.rowid")
        with pytest.raises(RuntimeError, match="poll kv_set failed"):
            _dispatch_raw(host, '!poll add "t" "a" "b"')
        self._assert_loud(host, "kv_set")
        assert host.db.rows == {}, "an unreachable orphan poll row must be compensated away"

    def test_failed_orphan_cleanup_is_logged_not_swallowed(self, host: _Host) -> None:
        _fail_kv(host, "set", "poll.rowid")
        host.db.raise_on["delete"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("x"))
        with pytest.raises(RuntimeError, match="poll kv_set failed"):
            _dispatch_raw(host, '!poll add "t" "a" "b"')
        names = [m for lvl, m, _f in host.log_calls if lvl == 0]
        assert "poll.orphan_cleanup_failed" in names and "poll.backend_error" in names

    def test_vote_tally_failure_releases_the_marker_so_the_voter_can_retry(
        self, host: _Host
    ) -> None:
        pid = create(host)
        _fail_kv(host, "increment", "poll.tally")
        with pytest.raises(RuntimeError, match="poll kv_tally failed"):
            _dispatch_raw(host, f"!poll set {pid} 1", mod=False, actor="alice")
        self._assert_loud(host, "kv_tally")
        assert not any(".voted." in k for k in host.kv.store), "marker must be released"

    def test_marker_release_failure_is_logged(self, host: _Host) -> None:
        pid = create(host)
        _fail_kv(host, "increment", "poll.tally")
        _fail_kv(host, "delete", "poll.voted")
        with pytest.raises(RuntimeError, match="poll kv_tally failed"):
            _dispatch_raw(host, f"!poll set {pid} 1", mod=False, actor="alice")
        names = [m for lvl, m, _f in host.log_calls if lvl == 0]
        assert "poll.marker_release_failed" in names

    def test_marker_claim_failure(self, host: _Host) -> None:
        pid = create(host)
        _fail_kv(host, "increment", "poll.voted")
        with pytest.raises(RuntimeError, match="poll kv_claim failed"):
            _dispatch_raw(host, f"!poll set {pid} 1", mod=False)
        self._assert_loud(host, "kv_claim")

    def test_kv_index_lookup_failure(self, host: _Host) -> None:
        _fail_kv(host, "get", "poll.rowid")
        with pytest.raises(RuntimeError, match="poll kv_get failed"):
            _dispatch_raw(host, "!poll list 1", mod=False)
        self._assert_loud(host, "kv_get")

    def test_db_get_failure(self, host: _Host) -> None:
        pid = create(host)
        host.db.raise_on["get"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("x"))
        with pytest.raises(RuntimeError, match="poll db_get failed"):
            _dispatch_raw(host, f"!poll list {pid}", mod=False)
        self._assert_loud(host, "db_get")

    def test_db_query_failure(self, host: _Host) -> None:
        host.db.raise_on["query"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("x"))
        with pytest.raises(RuntimeError, match="poll db_query failed"):
            _dispatch_raw(host, "!poll list", mod=False)
        self._assert_loud(host, "db_query")

    def test_index_pointing_at_missing_row_fails_loud(self, host: _Host) -> None:
        pid = create(host)
        host.db.rows.clear()
        host.db.versions.clear()
        with pytest.raises(RuntimeError, match="poll index_stale failed"):
            _dispatch_raw(host, f"!poll list {pid}", mod=False)
        self._assert_loud(host, "index_stale")

    def test_non_utf8_index_value_fails_loud(self, host: _Host) -> None:
        pid = create(host)
        host.kv.store[_scoped_key(_COMMUNITY, f"poll.rowid.{pid}")] = b"\xff\xfe"
        with pytest.raises(RuntimeError, match="poll index_decode failed"):
            _dispatch_raw(host, f"!poll list {pid}", mod=False)

    @pytest.mark.parametrize(
        "patch",
        [
            {"options": "not json"},
            {"options": "[]"},
            {"options": '["a", 3]'},
            {"options": '{"a": 1}'},
            {"is_active": "yes"},
            {"results": '["x", "y", "z"]'},
            {"results": "[1, 2]"},
            {"is_active": False, "results": None},
        ],
    )
    def test_corrupt_row_fails_loud_not_silently_empty(
        self, host: _Host, patch: dict[str, Any]
    ) -> None:
        pid = create(host)
        _only_row(host).update(patch)
        with pytest.raises(RuntimeError, match="poll row_decode failed"):
            _dispatch_raw(host, f"!poll list {pid}", mod=False)
        self._assert_loud(host, "row_decode")

    def test_corrupt_tally_fails_loud(self, host: _Host) -> None:
        pid = create(host)
        host.kv.store[_scoped_key(_COMMUNITY, f"poll.tally.{pid}.1")] = b"not-a-number"
        with pytest.raises(RuntimeError, match="poll tally_decode failed"):
            _dispatch_raw(host, f"!poll list {pid}", mod=False)

    def test_close_retries_a_concurrent_conflict_then_succeeds(self, host: _Host) -> None:
        pid = create(host)
        host.db.conflicts_remaining = 2
        reply = say(host, f"!poll remove {pid}", mod=True)
        assert reply.startswith(f"Poll {pid} closed")
        assert [c[0] for c in host.db.calls].count("update") == 3

    def test_close_gives_up_loudly_after_retry_budget(self, host: _Host) -> None:
        pid = create(host)
        host.db.conflicts_remaining = 99
        with pytest.raises(RuntimeError, match="poll db_update_retry failed"):
            _dispatch_raw(host, f"!poll remove {pid}")
        self._assert_loud(host, "db_update_retry")
        assert _only_row(host)["is_active"] is True

    def test_close_update_failure_is_loud(self, host: _Host) -> None:
        pid = create(host)
        host.db.raise_on["update"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("x"))
        with pytest.raises(RuntimeError, match="poll db_update failed"):
            _dispatch_raw(host, f"!poll remove {pid}")
        self._assert_loud(host, "db_update")

    def test_concurrent_close_replies_with_the_winners_snapshot(self, host: _Host) -> None:
        """Another closer lands between our get and update -> we re-read and report, not retry."""
        pid = create(host)
        row_id, row = next(iter(host.db.rows.items()))
        original_update = host.db.update
        calls = {"n": 0}

        def _racing_update(rid: str, ver: int, cvs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                row.update({"is_active": False, "results": "[0, 0, 0]"})
                host.db.versions[row_id] += 1
            return original_update(rid, ver, cvs)

        host.wit_world.imports.db.update = _racing_update
        reply = say(host, f"!poll remove {pid}", mod=True)
        assert reply.startswith(f"Poll {pid} is already closed")

    def test_relay_failure_propagates(self, host: _Host) -> None:
        def _boom(provider: str, msg: str) -> None:
            raise OSError("relay down")

        host.wit_world.imports.relay.push = _boom
        with pytest.raises(OSError, match="relay down"):
            _dispatch_raw(host, "!poll list", mod=False)


# dispatch guards, tenant-wide scope


class TestDispatchGuards:
    def test_missing_channel_id_raises(self, host: _Host) -> None:
        out = _run(transform(_event("!poll list", channel_id=None)))
        assert out is not None
        with pytest.raises(ValueError, match="channel_id"):
            _run(dispatch(_envelope(out), {}, http_client=None))

    def test_unknown_action_raises(self, host: _Host) -> None:
        raw = _event("x")
        del raw.payload["text"]
        raw.payload["action"] = "explode"
        with pytest.raises(ValueError, match="unrecognized poll action"):
            _run(dispatch(_envelope(raw), {}, http_client=None))

    def test_empty_string_community_raises_and_logs(self, host: _Host) -> None:
        out = _run(transform(_event("!poll list")))
        assert out is not None
        with pytest.raises(ValueError, match="empty-string community"):
            _run(dispatch(_envelope(out, community=""), {}, http_client=None))
        assert any(m == "poll.empty_community" for _l, m, _f in host.log_calls)
        assert host.relay_calls == []

    def test_tenant_wide_sentinel_community_works_and_is_scoped_under_zero(
        self, host: _Host
    ) -> None:
        pid = create_tenant_wide(host)
        assert "Vote recorded" in say(host, f"!poll set {pid} 1", actor="a", community=None)
        assert host.scoped_kv(f"poll.rowid.{pid}", None) is not None
        assert any(k.startswith("c.0.poll.") for k in host.kv.store)
        assert say(host, f"!poll list {pid}", community=None).count("1 vote") == 1

    def test_usage_reply_text_is_relayed_without_touching_kv_or_db(self, host: _Host) -> None:
        assert say(host, "!poll").startswith("Usage: !poll add")
        assert host.kv.calls == [] and host.db.calls == []

    def test_dispatch_result_shape(self, host: _Host) -> None:
        out = _run(transform(_event("!poll list")))
        assert out is not None
        result = _run(dispatch(_envelope(out), {}, http_client=None))
        assert (result.transport, result.detail, result.sub_type, result.http_status) == (
            "twitch",
            "list",
            None,
            None,
        )


def create_tenant_wide(host: _Host) -> int:
    reply = say(host, '!poll add "t" "a" "b"', actor="mod-1", mod=True, community=None)
    assert reply.startswith("Poll created!")
    return int(reply.split("ID: ")[1].split("\n")[0])


# logging / storage hygiene


class TestHygiene:
    _SECRETS = ("zzTITLEzz", "zzOPTIONzz", "zzACTORzz", "zzTARGETzz")

    def _drive_everything(self, host: _Host) -> None:
        t, o, a, tg = self._SECRETS
        reply = say(host, f'!poll add "{t}" "{o}1" "{o}2"', actor=f"{a}-mod", mod=True)
        pid = int(reply.split("ID: ")[1].split("\n")[0])
        say(host, f"!poll set {pid} 1", actor=a)
        say(host, f"!poll set {pid} 1", actor=a)  # duplicate
        say(host, f"!poll set {pid} 9", actor=a)  # invalid option
        say(host, "!poll set 77 1", actor=a)  # missing poll
        say(host, f"!poll set {pid}", actor=tg)  # malformed
        say(host, f'!poll add "{t}" "{o}"', actor=a, mod=False)  # denied
        say(host, "!poll list")
        say(host, f"!poll list {pid}")
        say(host, f"!poll remove {pid}", actor=f"{a}-mod", mod=True)
        say(host, f"!poll remove {pid}", actor=f"{a}-mod", mod=True)  # already closed
        say(host, f"!poll {tg}")  # usage error text from the parser

    def test_no_log_line_carries_raw_user_input_or_identity(self, host: _Host) -> None:
        self._drive_everything(host)
        assert len(host.log_calls) >= 10, "denominator: the scenario must actually log"
        blob = " ".join(f"{msg} {fields}" for _lvl, msg, fields in host.log_calls)
        for secret in self._SECRETS:
            assert secret.lower() not in blob.lower(), f"log leaked {secret!r}: {blob}"

    def test_failure_logs_carry_error_class_names_only(self, host: _Host) -> None:
        host.db.raise_on["insert"] = wit_fake_db.WitDbError(
            wit_fake_db.Error_Backend(f"detail {self._SECRETS[0]}")
        )
        with pytest.raises(RuntimeError):
            _dispatch_raw(host, f'!poll add "{self._SECRETS[0]}" "a" "b"')
        errors = [json.loads(f) for lvl, _m, f in host.log_calls if lvl == 0]
        assert errors and all(set(e) <= {"op", "error"} for e in errors)
        assert errors[0]["error"] == "DbError"  # the SDK facade's own classified exception type
        assert self._SECRETS[0] not in json.dumps(errors)

    def test_no_raw_actor_in_kv_keys_or_db_rows(self, host: _Host) -> None:
        self._drive_everything(host)
        stored = " ".join(host.kv.store) + json.dumps(list(host.db.rows.values()))
        for secret in self._SECRETS[2:3]:
            assert secret.lower() not in stored.lower()

    def test_every_kv_key_is_colon_free_and_ttl_bounded(self, host: _Host) -> None:
        self._drive_everything(host)
        keys = [args[0] for op, args in host.kv.calls if op in ("increment", "set", "get")]
        assert keys and all(":" not in k.replace("c.", "", 1) for k in keys)
        for op, args in host.kv.calls:
            if op == "increment" and (".tally." in args[0] or ".voted." in args[0]):
                assert args[2] == app._KV_TTL_SECONDS
        assert app._KV_TTL_SECONDS <= 30 * 24 * 60 * 60

    def test_is_privileged_logs_when_role_info_missing(self, host: _Host) -> None:
        assert _is_privileged({}) is False
        assert any(m == "poll.role_info_unavailable" for _l, m, _f in host.log_calls)
