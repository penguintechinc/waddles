"""Host-native tests for the `rank` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `db` (`wit_fake_db.py`, this
bundle's own small reproduction of `sdk/waddle-sdk/tests/wit_fake_db.py`'s shape, identical to
`bundles/python/loyalty/tests/wit_fake_db.py`'s own copy).

**`kv` uses the SHARED charset-enforcing fake** (`waddle_sdk.testing.install_fake_kv_host`,
gh-631), not a hand-rolled permissive stand-in -- this bundle's own `_index_key()` prefix is
`.`-separated (`rank.rowid.<pseudonym>`, never `:`, see `app.py`'s module docstring), and this
fake fails any test immediately if that ever regresses to a colon (the same class of bug the
module's own docstring documents biting `count`/`lurk` against a permissive fake, and the exact
thing the loyalty bundle's un-merged `_INDEX_KEY_PREFIX = "loyalty:rowid:"` would fail against
this same fake -- a live demonstration this bundle's own choice is correct, not just asserted).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
from typing import Any

import pytest
import wit_fake_db
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.testing import FakeKvHost, install_fake_kv_host

from app import (
    _KNOWN_COMMANDS,
    _USAGE,
    _caller_role_signal,
    _format_adjust_reply,
    _index_key,
    _level_for_xp,
    _normalize_target,
    _pseudonym,
    _resolve_adjust,
    _resolve_command,
    _xp_for_level,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(identity: str | None) -> str:
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return _scoped_key(community, key)


#: A `db` `row_id` shape no `FakeDb` in these tests ever actually inserts -- used to simulate a
#: kv index pointing at a row that `db.get` can no longer find (see the `index_stale` tests).
_MISSING_ROW_ID = b"00000000-0000-0000-0000-999999999999"
#: The first `row_id` `FakeDb.insert()` ever assigns -- used to pre-seed a kv index entry for a
#: row this test then makes `db.get` fail on directly (a real row, a direct backend error).
_FIRST_ROW_ID = b"00000000-0000-0000-0000-000000000001"


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
    """Fake WIT host: flags/kv/db/relay/log -- `kv` is the shared charset-enforcing fake."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.db = wit_fake_db.FakeDb()
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    return _install(monkeypatch)


def _install(monkeypatch: pytest.MonkeyPatch, *, flag_enabled: bool = True) -> _FakeHost:
    fake_kv = install_fake_kv_host(monkeypatch)
    host = _FakeHost(fake_kv)

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_enabled)
    db_mod = types.SimpleNamespace(
        insert=host.db.insert,
        get=host.db.get,
        query=host.db.query,
        update=host.db.update,
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
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: host.relay_calls.append((provider, msg))
    )
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: host.log_calls.append((lvl, msg, fields_json)),
    )
    import wit_world  # noqa: PLC0415 - installed into sys.modules by install_fake_kv_host above

    wit_world.imports.flags = flags_mod  # type: ignore[attr-defined]
    wit_world.imports.db = db_mod  # type: ignore[attr-defined]
    wit_world.imports.relay = relay_mod  # type: ignore[attr-defined]
    wit_world.imports.log = log_mod  # type: ignore[attr-defined]
    return host


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    target: str | None = None,
    amount: int | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if target is not None:
        payload["target"] = target
    if amount is not None:
        payload["amount"] = amount
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.rank",
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


def _reply_text(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return str(json.loads(message_json)["text"])


# -- level formula -------------------------------------------------------------


def test_level_for_zero_or_negative_xp_is_one() -> None:
    assert _level_for_xp(0) == 1
    assert _level_for_xp(-5) == 1


@pytest.mark.parametrize(
    ("xp", "expected_level"),
    [
        (0, 1),
        (99, 1),
        (100, 2),
        (399, 2),
        (400, 3),
        (899, 3),
        (900, 4),
        (1599, 4),
        (1600, 5),
    ],
)
def test_level_for_xp_thresholds(xp: int, expected_level: int) -> None:
    assert _level_for_xp(xp) == expected_level


def test_level_for_xp_is_monotonic_non_decreasing() -> None:
    previous = _level_for_xp(0)
    for xp in range(0, 5000, 7):
        current = _level_for_xp(xp)
        assert current >= previous
        previous = current


def test_xp_for_level_is_the_exact_inverse_of_level_for_xp() -> None:
    for level in range(1, 20):
        threshold = _xp_for_level(level)
        assert _level_for_xp(threshold) == level
        if threshold > 0:
            assert _level_for_xp(threshold - 1) == level - 1


def test_xp_for_level_one_or_below_is_zero() -> None:
    assert _xp_for_level(1) == 0
    assert _xp_for_level(0) == 0


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!rank", "!RANK", "  !rank  "])
def test_bare_rank_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "rank_self"


def test_rank_top_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank top")))
    assert result is not None
    assert result.payload["command"] == "leaderboard"


def test_rank_bare_target_matches_rank_other(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank alice")))
    assert result is not None
    assert result.payload["command"] == "rank_other"
    assert result.payload["target"] == "alice"


def test_rank_add_matches_with_amount_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank add 50 alice")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["target"] == "alice"
    assert result.payload["amount"] == 50


def test_rank_sub_matches_with_amount_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank sub 20 bob")))
    assert result is not None
    assert result.payload["command"] == "sub"
    assert result.payload["target"] == "bob"
    assert result.payload["amount"] == 20


@pytest.mark.parametrize(
    "text",
    [
        "!rank add",
        "!rank add alice",
        "!rank add 50",
        "!rank add abc alice",
        "!rank add -5 alice",
        "!rank add 0 alice",
        "!rank alice bob",
        "!rank enable ai",
        "!rank reset",
    ],
)
def test_malformed_or_unsupported_input_replies_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!rankx", "rank", "!ranker", "hello", ""])
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
    _install(monkeypatch, flag_enabled=False)
    assert _run(transform(_sample_event("!rank"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!rank")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() / _resolve_adjust() direct unit coverage ----------------


def test_resolve_command_bare_is_rank_self() -> None:
    spec = CommandSpec(name="rank", sub_modules=frozenset({"top"}))
    parsed = parse_command("!rank", spec)
    assert _resolve_command(parsed, "!rank") == ("rank_self", None, None)


def test_resolve_command_top_is_leaderboard() -> None:
    spec = CommandSpec(name="rank", sub_modules=frozenset({"top"}))
    parsed = parse_command("!rank top", spec)
    assert _resolve_command(parsed, "!rank top") == ("leaderboard", None, None)


def test_resolve_command_unparseable_single_token_is_rank_other() -> None:
    assert _resolve_command(None, "!rank alice") == ("rank_other", "alice", None)


def test_resolve_command_unparseable_multi_word_is_usage() -> None:
    assert _resolve_command(None, "!rank alice bob") == ("usage", None, None)


def test_resolve_command_unimplemented_option_is_usage() -> None:
    parsed = ParsedCommand(command="rank", sub_module=None, option="reset", args=None)
    assert _resolve_command(parsed, "!rank reset") == ("usage", None, None)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (None, ("usage", None, None)),
        ("", ("usage", None, None)),
        ("alice", ("usage", None, None)),
        ("50", ("usage", None, None)),
        ("abc alice", ("usage", None, None)),
        ("-5 alice", ("usage", None, None)),
        ("0 alice", ("usage", None, None)),
        ("50 alice", ("add", "alice", 50)),
    ],
)
def test_resolve_adjust(args: str | None, expected: tuple[str, str | None, int | None]) -> None:
    assert _resolve_adjust("add", args) == expected


# -- rank queries ---------------------------------------------------------------


def test_rank_self_before_any_xp(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "rank_self"), {}, http_client=None))
    assert result.detail == "rank_self"
    assert "level 1" in _reply_text(fake_host)
    assert "0 XP" in _reply_text(fake_host)


def test_rank_other_before_any_xp(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "rank_other", target="alice"), {}, http_client=None)
    )
    assert result.detail == "rank_other"
    text = _reply_text(fake_host)
    assert "alice" in text
    assert "level 1" in text
    assert "0 XP" in text


def test_rank_other_shows_the_raw_typed_name_not_the_pseudonym(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="Alice", amount=150, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "rank_other", target="Alice"), {}, http_client=None)
    )
    assert result.detail == "rank_other"
    text = _reply_text(fake_host)
    assert "Alice is level 2 (150 XP)" in text


def test_rank_other_normalizes_at_mention_and_case(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "rank_other", target="@Alice"), {}, http_client=None)
    )
    assert result.detail == "rank_other"
    assert "10 XP" in _reply_text(fake_host)


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "rank_self", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv.calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.rank",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "rank_self", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized rank command"):
        _run(dispatch(envelope, {}, http_client=None))


def test_every_known_command_is_handled_without_raising_unrecognized(fake_host: _FakeHost) -> None:
    assert _KNOWN_COMMANDS == {
        "rank_self",
        "rank_other",
        "leaderboard",
        "add",
        "sub",
        "usage",
    }


# -- add / sub -----------------------------------------------------------------


def test_add_creates_a_new_row_for_a_first_time_recipient(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=25, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"
    assert "Added 25 XP to alice" in _reply_text(fake_host)
    assert "Now level 1 (25 XP)" in _reply_text(fake_host)


def test_add_accumulates_onto_an_existing_row(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=90, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"
    assert "Now level 2 (100 XP)" in _reply_text(fake_host)


def test_sub_clamps_at_zero_for_a_first_time_target(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "sub", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "sub"
    assert "Now level 1 (0 XP)" in _reply_text(fake_host)


def test_sub_clamps_at_zero_rather_than_going_negative(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(
            _sample_envelope("twitch", "sub", target="alice", amount=20, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "sub"
    assert "Now level 1 (0 XP)" in _reply_text(fake_host)


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(True, None), (None, True), (True, True)],
)
def test_adjust_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch",
                "add",
                target="alice",
                amount=5,
                is_mod=is_mod,
                is_broadcaster=is_broadcaster,
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"


def test_adjust_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "add", target="alice", amount=5, is_mod=False, is_broadcaster=False
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add:denied"
    assert "only moderators/broadcasters" in _reply_text(fake_host)
    assert fake_host.db.rows == {}


def test_adjust_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(
        dispatch(
            _sample_envelope("discord", "add", target="alice", amount=5), {}, http_client=None
        )
    )
    assert result.detail == "add:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "rank.adjust_denied"]
    assert denial_logs


def test_adjust_raises_on_malformed_payload(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", target=None, amount=None, is_mod=True)
    with pytest.raises(ValueError, match="malformed add payload"):
        _run(dispatch(envelope, {}, http_client=None))


def test_adjust_retries_on_a_single_version_conflict(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    real_update = fake_host.db.update
    calls = {"n": 0}

    def flaky_update(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        calls["n"] += 1
        if calls["n"] == 1:
            raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))
        return real_update(row_id, expected_version, column_values)

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = flaky_update

    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"
    assert "Now level 1 (15 XP)" in _reply_text(fake_host)
    assert calls["n"] == 2


def test_adjust_fails_loud_when_conflict_retries_are_exhausted(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )

    def always_conflict(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = always_conflict

    with pytest.raises(RuntimeError, match="rank db_update_retry failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_adjust_fails_loud_when_kv_index_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_index_key(_pseudonym("alice")))] = _MISSING_ROW_ID
    with pytest.raises(RuntimeError, match="rank index_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_rank_fails_loud_when_kv_index_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_index_key(_pseudonym("alice")))] = _MISSING_ROW_ID
    with pytest.raises(RuntimeError, match="rank index_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "rank_other", target="alice"), {}, http_client=None
            )
        )


# -- leaderboard ----------------------------------------------------------------


def test_leaderboard_empty(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    assert _reply_text(fake_host) == "no one has any rank yet."


def test_leaderboard_orders_by_xp_descending(fake_host: _FakeHost) -> None:
    for name, amount in (("alice", 10), ("bob", 500), ("carol", 250)):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target=name, amount=amount, is_mod=True),
                {},
                http_client=None,
            )
        )
    result = _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    text = _reply_text(fake_host)
    assert text.index("500") < text.index("250") < text.index("10 XP")


def test_leaderboard_entries_show_a_pseudonym_tag_not_a_display_name(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=150, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    text = _reply_text(fake_host)
    assert "alice" not in text
    assert "player-" in text
    assert _pseudonym("alice")[:8] in text
    assert "level 2" in text


# -- backend failures: fail loud, never silent ----------------------------------


def test_rank_self_raises_and_replies_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _install(monkeypatch)
    import wit_world  # noqa: PLC0415 - installed by _install above

    def raising_get(key: str) -> bytes | None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    wit_world.imports.kv.get = raising_get

    with pytest.raises(RuntimeError, match="rank kv_get failed"):
        _run(dispatch(_sample_envelope("twitch", "rank_self"), {}, http_client=None))
    assert "unavailable" in _reply_text(host)
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "rank.backend_error"]
    assert error_logs


def test_add_raises_and_replies_on_db_insert_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _install(monkeypatch)
    host.db.raise_on["insert"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="rank db_insert failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(host)


def test_leaderboard_raises_and_replies_on_db_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _install(monkeypatch)
    host.db.raise_on["query"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    with pytest.raises(RuntimeError, match="rank db_query failed"):
        _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert "unavailable" in _reply_text(host)


def test_add_raises_and_replies_on_kv_set_error_for_a_new_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _install(monkeypatch)
    import wit_world  # noqa: PLC0415 - installed by _install above

    def raising_set(key: str, value: bytes, ttl: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    wit_world.imports.kv.set = raising_set

    with pytest.raises(RuntimeError, match="rank kv_set failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(host)


def test_rank_other_raises_and_replies_on_a_direct_db_get_error(fake_host: _FakeHost) -> None:
    fake_host.kv.store[_scoped(_index_key(_pseudonym("alice")))] = _FIRST_ROW_ID
    fake_host.db.raise_on["get"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="rank db_get failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "rank_other", target="alice"), {}, http_client=None
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_adjust_raises_and_replies_on_a_non_conflict_db_update_error(
    fake_host: _FakeHost,
) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )

    def raising_update(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = raising_update

    with pytest.raises(RuntimeError, match="rank db_update failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_rank_other_raises_on_a_malformed_payload_missing_target(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "rank_other", target=None)
    with pytest.raises(ValueError, match="malformed rank_other payload"):
        _run(dispatch(envelope, {}, http_client=None))


# -- usage -----------------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert _reply_text(fake_host) == _USAGE


# -- PII: never leak a raw username into kv/db keys or logs ----------------------


def test_pseudonym_is_a_non_reversible_hash_not_the_raw_identity() -> None:
    assert _pseudonym("viewer-1") == _expected_pseudonym("viewer-1")
    assert "viewer-1" not in _pseudonym("viewer-1")


def test_pseudonym_differs_per_identity() -> None:
    assert _pseudonym("viewer-1") != _pseudonym("viewer-2")
    assert _pseudonym(None) == _pseudonym(None)


def test_normalize_target_strips_mention_and_lowercases() -> None:
    assert _normalize_target("@Alice") == "alice"
    assert _normalize_target("ALICE") == "alice"
    assert _normalize_target("  alice  ") == "alice"


def test_index_key_is_colon_free(fake_host: _FakeHost) -> None:
    """The real `kv` host rejects `:` (gh-631) -- assert the built key never contains one."""
    key = _index_key(_pseudonym("alice"))
    assert ":" not in key
    assert "." in key


def test_dispatch_never_persists_the_raw_target_name_in_kv_or_db(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="SuperSecretUser99", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    for key in fake_host.kv.store:
        assert "SuperSecretUser99" not in key
        assert "supersecretuser99" not in key
    for row in fake_host.db.rows.values():
        assert "SuperSecretUser99" not in str(row.get("actor_hash", ""))


def test_dispatch_never_logs_the_raw_actor_or_target(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="SuperSecretUser99", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    _run(dispatch(_sample_envelope("twitch", "rank_self"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "SuperSecretUser99" not in message
        assert "SuperSecretUser99" not in fields_json


# -- _format_adjust_reply() / _caller_role_signal() unit coverage ----------------


def test_format_adjust_reply_add() -> None:
    assert (
        _format_adjust_reply("add", "alice", 10, 40)
        == "Added 10 XP to alice. Now level 1 (40 XP)."
    )


def test_format_adjust_reply_sub() -> None:
    text = _format_adjust_reply("sub", "alice", 10, 0)
    assert text == "Removed 10 XP from alice. Now level 1 (0 XP)."


def test_format_adjust_reply_crosses_a_level_boundary() -> None:
    text = _format_adjust_reply("add", "alice", 100, 400)
    assert text == "Added 100 XP to alice. Now level 3 (400 XP)."


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
