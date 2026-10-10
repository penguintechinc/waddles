"""Host-native tests for the `loyalty` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `kv` (same shape as `fish`'s own
test harness) and a fake `db` (`wit_fake_db.py`, this bundle's own small reproduction of
`sdk/waddle-sdk/tests/wit_fake_db.py`'s shape).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from typing import Any

import pytest
import wit_fake_db
from waddle_sdk.command import CommandSpec, ParsedCommand, parse_command
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.kv import validate_key

from app import (
    _KNOWN_COMMANDS,
    _USAGE,
    _caller_role_signal,
    _format_adjust_reply,
    _index_key,
    _normalize_target,
    _pseudonym,
    _resolve_adjust,
    _resolve_command,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _expected_pseudonym(identity: str | None) -> str:
    return hashlib.sha256((identity or "anonymous").encode()).hexdigest()


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.kv_store` is keyed by `community_kv`'s own `c:<community>:<key>` prefix."""
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
    """Fake WIT host: flags/kv/db/relay/log -- `db` delegates to `wit_fake_db.FakeDb`."""

    def __init__(self) -> None:
        self.kv_store: dict[str, bytes] = {}
        self.kv_calls: list[tuple[Any, ...]] = []
        self.db = wit_fake_db.FakeDb()
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, str, str]] = []


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    host = _FakeHost()
    _install(monkeypatch, host)
    return host


def _install(
    monkeypatch: pytest.MonkeyPatch, host: _FakeHost, *, flag_enabled: bool = True
) -> None:
    def kv_get(key: str) -> bytes | None:
        host.kv_calls.append(("get", key))
        return host.kv_store.get(key)

    def kv_set(key: str, value: bytes, ttl: int) -> None:
        host.kv_calls.append(("set", key, bytes(value), ttl))
        host.kv_store[key] = bytes(value)

    def kv_delete(key: str) -> None:
        host.kv_calls.append(("delete", key))
        host.kv_store.pop(key, None)

    def kv_increment(key: str, delta: int, ttl: int) -> int:
        host.kv_calls.append(("increment", key, delta, ttl))
        current = int(host.kv_store.get(key, b"0").decode())
        new_value = current + delta
        host.kv_store[key] = str(new_value).encode()
        return new_value

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_enabled)
    kv_mod = types.SimpleNamespace(
        get=kv_get, set=kv_set, delete=kv_delete, increment=kv_increment
    )
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
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, kv=kv_mod, db=db_mod, relay=relay_mod, log=log_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)


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
        app_id="waddles.core.example.loyalty",
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


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!points", "!POINTS", "  !points  "])
def test_bare_points_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "balance_self"


def test_points_top_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points top")))
    assert result is not None
    assert result.payload["command"] == "leaderboard"


def test_points_bare_target_matches_balance_other(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points alice")))
    assert result is not None
    assert result.payload["command"] == "balance_other"
    assert result.payload["target"] == "alice"


def test_points_add_matches_with_amount_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points add 50 alice")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["target"] == "alice"
    assert result.payload["amount"] == 50


def test_points_sub_matches_with_amount_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points sub 20 bob")))
    assert result is not None
    assert result.payload["command"] == "sub"
    assert result.payload["target"] == "bob"
    assert result.payload["amount"] == 20


@pytest.mark.parametrize(
    "text",
    [
        "!points add",
        "!points add alice",
        "!points add 50",
        "!points add abc alice",
        "!points add -5 alice",
        "!points add 0 alice",
        "!points alice bob",
        "!points enable ai",
        "!points reset",
    ],
)
def test_malformed_or_unsupported_input_replies_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!pointsx", "points", "!pointer", "hello", ""])
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
    assert _run(transform(_sample_event("!points"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!points")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() / _resolve_adjust() direct unit coverage ----------------


def test_resolve_command_bare_is_balance_self() -> None:
    spec = CommandSpec(name="points", sub_modules=frozenset({"top"}))
    parsed = parse_command("!points", spec)
    assert _resolve_command(parsed, "!points") == ("balance_self", None, None)


def test_resolve_command_top_is_leaderboard() -> None:
    spec = CommandSpec(name="points", sub_modules=frozenset({"top"}))
    parsed = parse_command("!points top", spec)
    assert _resolve_command(parsed, "!points top") == ("leaderboard", None, None)


def test_resolve_command_unparseable_single_token_is_balance_other() -> None:
    assert _resolve_command(None, "!points alice") == ("balance_other", "alice", None)


def test_resolve_command_unparseable_multi_word_is_usage() -> None:
    assert _resolve_command(None, "!points alice bob") == ("usage", None, None)


def test_resolve_command_unimplemented_option_is_usage() -> None:
    parsed = ParsedCommand(command="points", sub_module=None, option="reset", args=None)
    assert _resolve_command(parsed, "!points reset") == ("usage", None, None)


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
def test_resolve_adjust(
    args: str | None,
    expected: tuple[str, str | None, int | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Install the fake host: an unparseable amount (e.g. "abc alice") logs via `log.debug()`.
    _install(monkeypatch, _FakeHost())
    assert _resolve_adjust("add", args) == expected


# -- balance queries -------------------------------------------------------


def test_balance_self_before_any_points(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "balance_self"), {}, http_client=None))
    assert result.detail == "balance_self"
    assert "0 points" in _reply_text(fake_host)


def test_balance_other_before_any_points(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "balance_other", target="alice"), {}, http_client=None)
    )
    assert result.detail == "balance_other"
    text = _reply_text(fake_host)
    assert "alice" in text
    assert "0 points" in text


def test_balance_other_shows_the_raw_typed_name_not_the_pseudonym(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="Alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "balance_other", target="Alice"), {}, http_client=None)
    )
    assert result.detail == "balance_other"
    text = _reply_text(fake_host)
    assert "Alice has 10 points" in text


def test_balance_other_normalizes_at_mention_and_case(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    result = _run(
        dispatch(_sample_envelope("twitch", "balance_other", target="@Alice"), {}, http_client=None)
    )
    assert result.detail == "balance_other"
    assert "10 points" in _reply_text(fake_host)


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "balance_self", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.loyalty",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "balance_self", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized points command"):
        _run(dispatch(envelope, {}, http_client=None))


def test_every_known_command_is_handled_without_raising_unrecognized(fake_host: _FakeHost) -> None:
    assert _KNOWN_COMMANDS == {
        "balance_self",
        "balance_other",
        "leaderboard",
        "add",
        "sub",
        "usage",
    }


# -- add / sub ---------------------------------------------------------------


def test_add_creates_a_new_row_for_a_first_time_recipient(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=25, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"
    assert "Added 25 points to alice" in _reply_text(fake_host)
    assert "New balance: 25" in _reply_text(fake_host)


def test_add_accumulates_onto_an_existing_row(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=25, is_mod=True),
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
    assert "New balance: 35" in _reply_text(fake_host)


def test_sub_clamps_at_zero_for_a_first_time_target(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "sub", target="alice", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    assert result.detail == "sub"
    assert "New balance: 0" in _reply_text(fake_host)


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
    assert "New balance: 0" in _reply_text(fake_host)


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
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "loyalty.adjust_denied"]
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
    assert "New balance: 15" in _reply_text(fake_host)
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

    with pytest.raises(RuntimeError, match="loyalty db_update_retry failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_adjust_fails_loud_when_kv_index_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    fake_host.kv_store[_scoped(_index_key(_pseudonym("alice")))] = _MISSING_ROW_ID
    with pytest.raises(RuntimeError, match="loyalty index_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_balance_fails_loud_when_kv_index_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    fake_host.kv_store[_scoped(_index_key(_pseudonym("alice")))] = _MISSING_ROW_ID
    with pytest.raises(RuntimeError, match="loyalty index_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "balance_other", target="alice"), {}, http_client=None
            )
        )


# -- leaderboard --------------------------------------------------------------


def test_leaderboard_empty(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert result.detail == "leaderboard"
    assert _reply_text(fake_host) == "no one has any points yet."


def test_leaderboard_orders_by_balance_descending(fake_host: _FakeHost) -> None:
    for name, amount in (("alice", 10), ("bob", 50), ("carol", 25)):
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
    assert text.index("50") < text.index("25") < text.index("10")


def test_leaderboard_entries_show_a_pseudonym_tag_not_a_display_name(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="alice", amount=10, is_mod=True),
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


# -- backend failures: fail loud, never silent ---------------------------------


def test_balance_self_raises_and_replies_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    import wit_world  # noqa: PLC0415 - installed by _install above

    def raising_get(key: str) -> bytes | None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    wit_world.imports.kv.get = raising_get

    with pytest.raises(RuntimeError, match="loyalty kv_get failed"):
        _run(dispatch(_sample_envelope("twitch", "balance_self"), {}, http_client=None))
    assert "unavailable" in _reply_text(host)
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "loyalty.backend_error"]
    assert error_logs


def test_add_raises_and_replies_on_db_insert_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    host.db.raise_on["insert"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="loyalty db_insert failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(host)


def test_leaderboard_raises_and_replies_on_db_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    host.db.raise_on["query"] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    with pytest.raises(RuntimeError, match="loyalty db_query failed"):
        _run(dispatch(_sample_envelope("twitch", "leaderboard"), {}, http_client=None))
    assert "unavailable" in _reply_text(host)


def test_add_raises_and_replies_on_kv_set_error_for_a_new_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    import wit_world  # noqa: PLC0415 - installed by _install above

    def raising_set(key: str, value: bytes, ttl: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    wit_world.imports.kv.set = raising_set

    with pytest.raises(RuntimeError, match="loyalty kv_set failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(host)


def test_balance_other_raises_and_replies_on_a_direct_db_get_error(fake_host: _FakeHost) -> None:
    fake_host.kv_store[_scoped(_index_key(_pseudonym("alice")))] = _FIRST_ROW_ID
    fake_host.db.raise_on["get"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="loyalty db_get failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "balance_other", target="alice"), {}, http_client=None
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

    with pytest.raises(RuntimeError, match="loyalty db_update failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", target="alice", amount=5, is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_balance_other_raises_on_a_malformed_payload_missing_target(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "balance_other", target=None)
    with pytest.raises(ValueError, match="malformed balance_other payload"):
        _run(dispatch(envelope, {}, http_client=None))


# -- usage ---------------------------------------------------------------------


def test_usage_command_replies_with_usage_text(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert result.detail == "usage"
    assert _reply_text(fake_host) == _USAGE


# -- PII: never leak a raw username into kv/db keys or logs --------------------


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


def test_dispatch_never_persists_the_raw_target_name_in_kv_or_db(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", target="SuperSecretUser99", amount=10, is_mod=True),
            {},
            http_client=None,
        )
    )
    for key in fake_host.kv_store:
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
    _run(dispatch(_sample_envelope("twitch", "balance_self"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "SuperSecretUser99" not in message
        assert "SuperSecretUser99" not in fields_json


# -- _format_adjust_reply() / _caller_role_signal() unit coverage ---------------


def test_format_adjust_reply_add() -> None:
    assert (
        _format_adjust_reply("add", "alice", 10, 40)
        == "Added 10 points to alice. New balance: 40."
    )


def test_format_adjust_reply_sub() -> None:
    text = _format_adjust_reply("sub", "alice", 10, 0)
    assert text == "Removed 10 points from alice. New balance: 0."


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


# -- PII-free logs -- regression: gh-674 (bundle-logs-must-be-pii-free) ----------

#: Lower-case on purpose: chat-typed targets are lower-cased before hashing, so a lower-case
#: sentinel round-trips (actor == target) through every code path below.
_SENTINEL = "sentinelpii9f3a"

#: Strict per-message allowlist: a log line may carry ONLY these fields. A new field (e.g. a
#: raw `target=`/`amount_text=`/`actor=`) fails here instead of silently shipping user input.
_ALLOWED_LOG_FIELDS: dict[str, frozenset[str]] = {
    "loyalty.transform matched": frozenset({"command"}),
    "loyalty.adjust_invalid_amount": frozenset({"command"}),
    "loyalty.dispatch relayed": frozenset({"platform", "command"}),
    "loyalty.dispatch adjusted": frozenset({"command"}),
    "loyalty.adjust_denied": frozenset({"command", "role_signal"}),
    "loyalty.backend_error": frozenset({"op", "error"}),
    "loyalty.missing_community": frozenset({"command"}),
}


def _assert_logs_pii_free(host: _FakeHost, *, minimum_lines: int) -> set[str]:
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


def _go(
    command: str,
    *,
    target: str | None = None,
    amount: int | None = None,
    role: bool | None = None,
    actor: str | None = "viewer-1",
    community: str | None = "comm-1",
) -> Any:
    envelope = _sample_envelope(
        "twitch",
        command,
        actor=actor,
        target=target,
        amount=amount,
        is_mod=role,
        community=community,
    )
    return _run(dispatch(envelope, {}, http_client=None))


def test_transform_logs_never_carry_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674
    for text in (
        f"!points {_SENTINEL}",
        f"!points add 5 {_SENTINEL}",
        f"!points sub 5 {_SENTINEL}",
        f"!points add abc {_SENTINEL}",  # invalid amount -> DEBUG line, still no raw text
        f"!points {_SENTINEL} extra",
        f"!points set {_SENTINEL}",
        "!points top",
        "!points",
    ):
        event = _sample_event(text, is_mod=True)
        event.actor = _SENTINEL
        assert _run(transform(event)) is not None
    seen = _assert_logs_pii_free(fake_host, minimum_lines=8)
    assert seen == {"loyalty.transform matched", "loyalty.adjust_invalid_amount"}


def test_dispatch_logs_never_carry_user_input_on_any_command(fake_host: _FakeHost) -> None:
    # regression: gh-674 -- sentinel as actor AND target, across every outcome.
    _go("add", target=_SENTINEL, amount=5, role=True, actor=_SENTINEL)
    _go("add", target=_SENTINEL, amount=5, role=True, actor=_SENTINEL)
    _go("sub", target=_SENTINEL, amount=3, role=True, actor=_SENTINEL)
    _go("sub", target=_SENTINEL, amount=999, role=True, actor=_SENTINEL)  # clamps at 0
    _go("balance_self", actor=_SENTINEL)
    _go("balance_other", target=_SENTINEL, actor=_SENTINEL)
    _go("leaderboard", actor=_SENTINEL)
    _go("add", target=_SENTINEL, amount=5, role=False, actor=_SENTINEL)  # denied
    _go("sub", target=_SENTINEL, amount=5, actor=_SENTINEL)  # denied, no role signal
    _go("usage", actor=_SENTINEL)

    seen = _assert_logs_pii_free(fake_host, minimum_lines=8)
    assert seen == {
        "loyalty.dispatch relayed",
        "loyalty.dispatch adjusted",
        "loyalty.adjust_denied",
    }
    assert all(_SENTINEL not in call[1] for call in fake_host.kv_calls)
    assert all(
        _SENTINEL not in str(row.get("actor_hash", "")) for row in fake_host.db.rows.values()
    )


def test_failure_paths_never_log_user_input(fake_host: _FakeHost) -> None:
    # regression: gh-674 -- a backend failure whose own exception text echoes user-ish data, a
    # kv index holding user-ish bytes, and the missing-community guard.
    fake_host.db.raise_on["insert"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend(f"detail {_SENTINEL}")
    )
    with pytest.raises(RuntimeError) as excinfo:
        _go("add", target=_SENTINEL, amount=5, role=True, actor=_SENTINEL)
    assert _SENTINEL not in str(excinfo.value)
    fake_host.db.raise_on.clear()

    fake_host.kv_store[_scoped(_index_key(_pseudonym(_SENTINEL)))] = _SENTINEL.encode()
    with pytest.raises(RuntimeError):
        _go("balance_self", actor=_SENTINEL)
    with pytest.raises(ValueError):
        _go("balance_self", actor=_SENTINEL, community=None)

    seen = _assert_logs_pii_free(fake_host, minimum_lines=3)
    assert seen == {"loyalty.backend_error", "loyalty.missing_community"}
    errors = [json.loads(f) for _lvl, m, f in fake_host.log_calls if m == "loyalty.backend_error"]
    # `waddle_sdk.db` re-raises the WIT error as its own `DbError`; only that class name is logged.
    assert errors == [
        {"op": "db_insert", "error": "DbError"},
        {"op": "index_stale", "error": "RuntimeError"},
    ]


# -- mod gate: add/sub fail closed, before any kv/db access ---------------------------

_NO_ADJUST_ROLE = [
    pytest.param({}, id="no-signal-at-all"),
    pytest.param({"is_mod": False}, id="mod-false"),
    pytest.param({"is_broadcaster": False}, id="broadcaster-false"),
    pytest.param({"is_mod": False, "is_broadcaster": False}, id="both-false"),
]


@pytest.mark.parametrize("verb", ["add", "sub"])
@pytest.mark.parametrize("role", _NO_ADJUST_ROLE)
def test_adjustments_are_denied_without_any_kv_or_db_access(
    verb: str, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", verb, target="alice", amount=5, **role), {}, http_client=None
        )
    )
    assert result.detail == f"{verb}:denied"
    assert _reply_text(fake_host) == "only moderators/broadcasters can adjust points"
    assert fake_host.kv_calls == []
    assert fake_host.db.calls == []


@pytest.mark.parametrize("verb", ["add", "sub"])
@pytest.mark.parametrize(
    "role",
    [{"is_mod": True}, {"is_broadcaster": True}, {"is_mod": True, "is_broadcaster": True}],
)
def test_either_badge_alone_opens_the_gate(
    verb: str, role: dict[str, bool], fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", verb, target="alice", amount=5, **role), {}, http_client=None
        )
    )
    assert result.detail == verb


def test_present_but_null_badge_fields_are_denied(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", target="alice", amount=5)
    envelope.event.payload["is_mod"] = None
    envelope.event.payload["is_broadcaster"] = None
    result = _run(dispatch(envelope, {}, http_client=None))
    assert result.detail == "add:denied"
    assert fake_host.db.calls == []


@pytest.mark.parametrize("command", ["balance_self", "balance_other", "leaderboard"])
def test_read_commands_never_need_a_role(command: str, fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("discord", command, target="alice", is_mod=False), {}, http_client=None
        )
    )
    assert result.detail == command


# -- corrupt store -----------------------------------------------------------------------


def test_non_utf8_index_value_raises_instead_of_inventing_a_balance(fake_host: _FakeHost) -> None:
    fake_host.kv_store[_scoped(_index_key(_pseudonym("viewer-1")))] = b"\xff\xfe"
    with pytest.raises((ValueError, RuntimeError)):
        _go("balance_self")
    assert fake_host.relay_calls == []
    assert fake_host.db.calls == []


@pytest.mark.parametrize("verb", ["add", "sub"])
def test_adjust_fails_loud_on_a_stale_index_for_both_verbs(verb: str, fake_host: _FakeHost) -> None:
    fake_host.kv_store[_scoped(_index_key(_pseudonym("alice")))] = _MISSING_ROW_ID
    with pytest.raises(RuntimeError, match="loyalty index_stale failed"):
        _go(verb, target="alice", amount=5, role=True)
    assert "temporarily unavailable" in _reply_text(fake_host)
    assert len(fake_host.relay_calls) == 1
    assert fake_host.db.rows == {}


@pytest.mark.parametrize("bad_balance", ["abc", None], ids=["text", "null"])
@pytest.mark.parametrize("command", ["balance_self", "add"])
def test_corrupt_balance_column_raises_instead_of_rendering_garbage(
    command: str, bad_balance: object, fake_host: _FakeHost
) -> None:
    _go("add", target="viewer-1", amount=5, role=True)
    (row,) = fake_host.db.rows.values()
    row["balance"] = bad_balance
    relay_before = len(fake_host.relay_calls)
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        _go(command, target="viewer-1", amount=1, role=True)
    assert not any(
        "points" in json.loads(m)["text"] for _p, m in fake_host.relay_calls[relay_before:]
    )


def test_leaderboard_raises_on_a_corrupt_balance_row(fake_host: _FakeHost) -> None:
    _go("add", target="alice", amount=5, role=True)
    (row,) = fake_host.db.rows.values()
    row["balance"] = "abc"
    relay_before = len(fake_host.relay_calls)
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        _go("leaderboard")
    assert len(fake_host.relay_calls) == relay_before


# -- no silent fallback ------------------------------------------------------------------


def test_every_backend_failure_replies_once_and_raises(fake_host: _FakeHost) -> None:
    cases = [
        ("insert", "add", "db_insert"),
        ("query", "leaderboard", "db_query"),
    ]
    for raise_on, command, op in cases:
        fake_host.relay_calls.clear()
        fake_host.log_calls.clear()
        fake_host.db.raise_on[raise_on] = wit_fake_db.WitDbError(wit_fake_db.Error_Backend("down"))
        with pytest.raises(RuntimeError, match=f"loyalty {op} failed"):
            _go(command, target="alice", amount=5, role=True)
        assert len(fake_host.relay_calls) == 1
        assert "temporarily unavailable" in _reply_text(fake_host)
        errors = [
            (lvl, json.loads(f)["op"])
            for lvl, m, f in fake_host.log_calls
            if m == "loyalty.backend_error"
        ]
        assert errors == [(0, op)]
        fake_host.db.raise_on.clear()


def test_relay_failure_propagates_and_is_not_logged_as_relayed(fake_host: _FakeHost) -> None:
    def _boom(provider: str, msg: str) -> None:
        raise RuntimeError("relay down")

    sys.modules["wit_world"].imports.relay = types.SimpleNamespace(push=_boom)
    with pytest.raises(RuntimeError, match="relay down"):
        _go("balance_self")
    assert not any(m == "loyalty.dispatch relayed" for _lvl, m, _f in fake_host.log_calls)


def test_missing_community_logs_error_and_touches_no_state(fake_host: _FakeHost) -> None:
    with pytest.raises(ValueError, match="community"):
        _go("add", target="alice", amount=5, role=True, community=None)
    assert fake_host.kv_calls == []
    assert fake_host.db.calls == []
    assert fake_host.relay_calls == []
    errors = [
        (lvl, json.loads(f))
        for lvl, m, f in fake_host.log_calls
        if m == "loyalty.missing_community"
    ]
    assert errors == [(0, {"command": "add"})]


# -- ledger behavior ----------------------------------------------------------------------


def test_balances_never_leak_across_communities(fake_host: _FakeHost) -> None:
    _go("add", target="alice", amount=50, role=True, community="comm-1")
    _go("balance_other", target="alice", community="comm-2")
    assert _reply_text(fake_host) == "alice has 0 points."


def test_balance_reads_do_not_write(fake_host: _FakeHost) -> None:
    _go("add", target="viewer-1", amount=5, role=True)
    kv_before = dict(fake_host.kv_store)
    rows_before = {k: dict(v) for k, v in fake_host.db.rows.items()}
    for command in ("balance_self", "balance_other", "leaderboard"):
        _go(command, target="viewer-1")
    assert fake_host.kv_store == kv_before
    assert fake_host.db.rows == rows_before


def test_leaderboard_is_capped_at_ten_entries(fake_host: _FakeHost) -> None:
    for i in range(12):
        _go("add", target=f"user{i:02d}", amount=i + 1, role=True)
    _go("leaderboard")
    text = _reply_text(fake_host)
    assert text.count("player-") == 10
    assert text.startswith("Top points: 1. player-")
    assert ": 12," in text and ": 3" in text and ": 2," not in text


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("007 alice", ("add", "alice", 7)),
        ("+5 alice", ("add", "alice", 5)),
        ("5.5 alice", ("usage", None, None)),
        ("1e3 alice", ("usage", None, None)),
        ("five alice", ("usage", None, None)),
        ("5 alice bob", ("usage", None, None)),
    ],
)
def test_resolve_adjust_amount_shapes(
    args: str, expected: tuple[str, str | None, int | None], fake_host: _FakeHost
) -> None:
    assert _resolve_adjust("add", args) == expected


# -- transform(): payload shape + flag gate -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("!points", {"command", "channel_id"}),
        ("!points top", {"command", "channel_id"}),
        ("!points alice", {"command", "channel_id", "target"}),
        ("!points add 5 alice", {"command", "channel_id", "target", "amount"}),
        ("!POINTS Sub 5 alice", {"command", "channel_id", "target", "amount"}),
        ("!points add alice", {"command", "channel_id"}),
    ],
)
def test_transform_forwards_only_the_fields_dispatch_needs(
    text: str, keys: set[str], fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert set(result.payload) == keys


def test_flag_is_checked_by_key_and_defaults_off(fake_host: _FakeHost) -> None:
    seen: list[tuple[str, bool]] = []

    def _enabled(key: str, default_value: bool) -> bool:
        seen.append((key, default_value))
        return False

    sys.modules["wit_world"].imports.flags = types.SimpleNamespace(enabled=_enabled)
    assert _run(transform(_sample_event("!points"))) is None
    assert seen == [("waddles.command-loyalty", False)]
    assert fake_host.kv_calls == [] and fake_host.db.calls == []


# -- kv key charset -- regression: gh-631 ---------------------------------------------------


def test_every_kv_key_the_bundle_touches_satisfies_the_host_charset(fake_host: _FakeHost) -> None:
    # regression: gh-631 -- the host rejects any guest key outside ASCII alnum + `_`/`-`/`.`
    # (notably `:`). This suite's own kv fake is permissive, so check the keys directly.
    _go("add", target="alice", amount=5, role=True)
    _go("balance_other", target="alice")
    _go("add", target="alice", amount=5, role=True)
    keys = [call[1] for call in fake_host.kv_calls]
    assert keys
    for key in keys:
        validate_key(key)
        assert ":" not in key
    validate_key(_index_key(_pseudonym("alice")))
