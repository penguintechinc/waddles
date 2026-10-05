"""Host-native tests for the `inventory` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/pyping/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `kv` (same shape as `fish`'s own
test harness) and a fake `db` (`wit_fake_db.py`, this bundle's own small reproduction of
`sdk/waddle-sdk/tests/wit_fake_db.py`'s shape, extended with `delete`).
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
from waddle_sdk.community_kv import _scoped_key
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

from app import (
    _KNOWN_COMMANDS,
    _USAGE,
    _caller_role_signal,
    _dir_key,
    _format_grant_reply,
    _normalize_item,
    _normalize_target,
    _pseudonym,
    _resolve_command,
    _resolve_give,
    _resolve_grant,
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
#: kv directory pointing at a row that `db.get` can no longer find (see the `directory_stale`
#: tests).
_MISSING_ROW_ID = "00000000-0000-0000-0000-999999999999"
#: The first `row_id` `FakeDb.insert()` ever assigns -- used to pre-seed a directory entry for a
#: row this test then makes `db.get` fail on directly (a real row, a direct backend error).
_FIRST_ROW_ID = "00000000-0000-0000-0000-000000000001"


def _sample_event(
    text: str,
    *,
    channel_id: str | None = "12345",
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    actor: str | None = "viewer-1",
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
        delete=host.db.delete,
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
    item: str | None = None,
    target: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": "12345"}
    if item is not None:
        payload["item"] = item
    if target is not None:
        payload["target"] = target
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.inventory",
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


async def _grant(
    verb: str, item: str, target: str, *, is_mod: bool | None = True
) -> Any:
    return await dispatch(
        _sample_envelope("twitch", verb, item=item, target=target, is_mod=is_mod),
        {},
        http_client=None,
    )


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!inventory", "!INVENTORY", "  !inventory  "])
def test_bare_inventory_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "list_self"


@pytest.mark.parametrize("text", ["!inv", "!INV", "  !inv  "])
def test_bare_inv_alias_also_matches(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "list_self"


def test_inv_bare_target_matches_list_other(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inv alice")))
    assert result is not None
    assert result.payload["command"] == "list_other"
    assert result.payload["target"] == "alice"


def test_inv_give_matches_with_item_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inv give fish alice")))
    assert result is not None
    assert result.payload["command"] == "give"
    assert result.payload["item"] == "fish"
    assert result.payload["target"] == "alice"


def test_inventory_add_matches_with_item_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inventory add fish alice")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["item"] == "fish"
    assert result.payload["target"] == "alice"


def test_inventory_remove_matches_with_item_and_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inv remove fish bob")))
    assert result is not None
    assert result.payload["command"] == "remove"
    assert result.payload["item"] == "fish"
    assert result.payload["target"] == "bob"


@pytest.mark.parametrize(
    "text",
    [
        "!inv add",
        "!inv add fish",
        "!inv add fish alice bob",
        "!inv give",
        "!inv give fish",
        "!inv give fish alice bob",
        "!inv alice bob",
        "!inv enable ai",
        "!inv reset",
        "!inv set foo bar",
    ],
)
def test_malformed_or_unsupported_input_replies_usage(text: str, fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


@pytest.mark.parametrize("text", ["!inventoryx", "inventory", "!inver", "hello", ""])
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
    assert _run(transform(_sample_event("!inventory"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inventory", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!inventory")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() / _resolve_grant() / _resolve_give() direct unit coverage ----


def test_resolve_command_bare_is_list_self() -> None:
    from waddle_sdk.command import CommandSpec, parse_command

    spec = CommandSpec(name="inventory", sub_modules=frozenset())
    parsed = parse_command("!inventory", spec)
    assert _resolve_command(parsed, "") == ("list_self", None, None)


def test_resolve_command_unparseable_single_token_is_list_other() -> None:
    assert _resolve_command(None, "alice") == ("list_other", None, "alice")


def test_resolve_command_unparseable_multi_word_non_give_is_usage() -> None:
    assert _resolve_command(None, "alice bob") == ("usage", None, None)


def test_resolve_command_give_shape_is_give() -> None:
    assert _resolve_command(None, "give fish alice") == ("give", "fish", "alice")


def test_resolve_command_empty_rest_after_parse_failure_is_usage() -> None:
    assert _resolve_command(None, "") == ("usage", None, None)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (None, ("usage", None, None)),
        ("", ("usage", None, None)),
        ("fish", ("usage", None, None)),
        ("fish alice bob", ("usage", None, None)),
        ("fish alice", ("add", "fish", "alice")),
    ],
)
def test_resolve_grant(args: str | None, expected: tuple[str, str | None, str | None]) -> None:
    assert _resolve_grant("add", args) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (None, ("usage", None, None)),
        ("", ("usage", None, None)),
        ("fish", ("usage", None, None)),
        ("fish alice bob", ("usage", None, None)),
        ("fish alice", ("give", "fish", "alice")),
    ],
)
def test_resolve_give(args: str | None, expected: tuple[str, str | None, str | None]) -> None:
    assert _resolve_give(args) == expected


# -- listing -------------------------------------------------------------------


def test_list_self_before_any_items(fake_host: _FakeHost) -> None:
    result = _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert result.detail == "list_self"
    assert _reply_text(fake_host) == "viewer-1 has no items."


def test_list_other_before_any_items(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None)
    )
    assert result.detail == "list_other"
    assert _reply_text(fake_host) == "alice has no items."


def test_list_self_shows_granted_items(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "viewer-1"))
    _run(_grant("add", "sword", "viewer-1"))
    result = _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert result.detail == "list_self"
    text = _reply_text(fake_host)
    assert "fish x1" in text
    assert "sword x1" in text


def test_list_other_shows_the_raw_typed_name_not_the_pseudonym(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "Alice"))
    _run(
        dispatch(_sample_envelope("twitch", "list_other", target="Alice"), {}, http_client=None)
    )
    text = _reply_text(fake_host)
    assert "Alice's inventory" in text
    assert "fish x1" in text


def test_list_other_normalizes_at_mention_and_case(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    result = _run(
        dispatch(_sample_envelope("twitch", "list_other", target="@Alice"), {}, http_client=None)
    )
    assert result.detail == "list_other"
    assert "fish x1" in _reply_text(fake_host)


def test_list_truncates_beyond_max_and_notes_remaining(fake_host: _FakeHost) -> None:
    for i in range(30):
        _run(_grant("add", f"item{i:02d}", "viewer-1"))
    result = _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert result.detail == "list_self"
    text = _reply_text(fake_host)
    assert "(and 5 more)" in text


def test_dispatch_raises_when_community_is_missing(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "list_self", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))
    assert fake_host.kv_calls == []


def test_dispatch_raises_when_channel_id_is_missing(fake_host: _FakeHost) -> None:
    envelope = StageEnvelope(
        tenant="tenant-1",
        community="comm-1",
        app_id="waddles.core.example.inventory",
        stage="action",
        event=PlatformEvent(
            platform="twitch",
            event_type="chat.message",
            actor="viewer-1",
            payload={"command": "list_self", "channel_id": None},
            occurred_at="2026-10-05T00:00:00.000Z",
        ),
        ts="2026-10-05T00:00:00.000Z",
    )
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_raises_on_unrecognized_command(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "not-a-real-command")
    with pytest.raises(ValueError, match="unrecognized inventory command"):
        _run(dispatch(envelope, {}, http_client=None))


def test_every_known_command_is_handled_without_raising_unrecognized() -> None:
    assert _KNOWN_COMMANDS == {"list_self", "list_other", "give", "add", "remove", "usage"}


# -- add / remove ---------------------------------------------------------------


def test_add_creates_a_new_row_for_a_first_time_recipient(fake_host: _FakeHost) -> None:
    result = _run(_grant("add", "fish", "alice"))
    assert result.detail == "add"
    assert "Gave 1 fish to alice" in _reply_text(fake_host)
    assert "now have 1" in _reply_text(fake_host)


def test_add_accumulates_onto_an_existing_row(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    result = _run(_grant("add", "fish", "alice"))
    assert result.detail == "add"
    assert "now have 2" in _reply_text(fake_host)


def test_remove_on_a_first_time_target_is_a_no_op_reply(fake_host: _FakeHost) -> None:
    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "alice has no fish to remove" in _reply_text(fake_host)
    assert fake_host.db.rows == {}


def test_remove_decrements_and_deletes_the_row_at_zero(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "now have 0" in _reply_text(fake_host)
    assert fake_host.db.rows == {}
    key = _scoped(_dir_key(_pseudonym("alice")))
    assert json.loads(fake_host.kv_store[key].decode()) == {}


def test_remove_after_delete_is_a_no_op_reply_again(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    _run(_grant("remove", "fish", "alice"))
    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "alice has no fish to remove" in _reply_text(fake_host)


def test_remove_decrements_without_deleting_above_zero(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    _run(_grant("add", "fish", "alice"))
    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "now have 1" in _reply_text(fake_host)
    assert len(fake_host.db.rows) == 1


@pytest.mark.parametrize(
    ("is_mod", "is_broadcaster"),
    [(True, None), (None, True), (True, True)],
)
def test_grant_allowed_for_mod_or_broadcaster(
    is_mod: bool | None, is_broadcaster: bool | None, fake_host: _FakeHost
) -> None:
    result = _run(
        dispatch(
            _sample_envelope(
                "twitch", "add", item="fish", target="alice", is_mod=is_mod,
                is_broadcaster=is_broadcaster,
            ),
            {},
            http_client=None,
        )
    )
    assert result.detail == "add"


def test_grant_rejected_for_non_mod(fake_host: _FakeHost) -> None:
    result = _run(_grant("add", "fish", "alice", is_mod=False))
    assert result.detail == "add:denied"
    assert "only moderators/broadcasters" in _reply_text(fake_host)
    assert fake_host.db.rows == {}


def test_grant_rejected_when_role_signal_entirely_absent(fake_host: _FakeHost) -> None:
    """Discord today has no is_mod/is_broadcaster at all -- must deny, never implicitly allow."""
    result = _run(
        dispatch(
            _sample_envelope("discord", "add", item="fish", target="alice"), {}, http_client=None
        )
    )
    assert result.detail == "add:denied"
    denial_logs = [m for _lvl, m, _f in fake_host.log_calls if m == "inventory.grant_denied"]
    assert denial_logs


def test_grant_raises_on_malformed_payload(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "add", item=None, target=None, is_mod=True)
    with pytest.raises(ValueError, match="malformed add payload"):
        _run(dispatch(envelope, {}, http_client=None))


def test_add_retries_on_a_single_version_conflict(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))
    real_update = fake_host.db.update
    calls = {"n": 0}

    def flaky_update(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        calls["n"] += 1
        if calls["n"] == 1:
            raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))
        return real_update(row_id, expected_version, column_values)

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = flaky_update

    result = _run(_grant("add", "fish", "alice"))
    assert result.detail == "add"
    assert "now have 2" in _reply_text(fake_host)
    assert calls["n"] == 2


def test_add_fails_loud_when_conflict_retries_are_exhausted(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))

    def always_conflict(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = always_conflict

    with pytest.raises(RuntimeError, match="inventory db_update_retry failed"):
        _run(_grant("add", "fish", "alice"))
    assert "unavailable" in _reply_text(fake_host)


def test_grant_fails_loud_when_kv_directory_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    key = _scoped(_dir_key(_pseudonym("alice")))
    fake_host.kv_store[key] = json.dumps({"fish": _MISSING_ROW_ID}).encode()
    with pytest.raises(RuntimeError, match="inventory directory_stale failed"):
        _run(_grant("add", "fish", "alice"))
    assert "unavailable" in _reply_text(fake_host)


def test_list_fails_loud_when_kv_directory_points_at_a_missing_row(fake_host: _FakeHost) -> None:
    key = _scoped(_dir_key(_pseudonym("alice")))
    fake_host.kv_store[key] = json.dumps({"fish": _MISSING_ROW_ID}).encode()
    with pytest.raises(RuntimeError, match="inventory directory_stale failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None
            )
        )


# -- give -------------------------------------------------------------------


def test_give_transfers_one_unit(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "viewer-1"))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "give", item="fish", target="alice"), {}, http_client=None
        )
    )
    assert result.detail == "give"
    assert "viewer-1 gave 1 fish to alice" in _reply_text(fake_host)


def test_give_decrements_giver_and_increments_recipient(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "viewer-1"))
    _run(_grant("add", "fish", "viewer-1"))
    _run(
        dispatch(
            _sample_envelope("twitch", "give", item="fish", target="alice"), {}, http_client=None
        )
    )
    giver_result = _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert giver_result.detail == "list_self"
    assert "fish x1" in _reply_text(fake_host)
    recipient_result = _run(
        dispatch(_sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None)
    )
    assert recipient_result.detail == "list_other"
    assert "fish x1" in _reply_text(fake_host)


def test_give_with_nothing_to_give_is_a_no_op_reply(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(
            _sample_envelope("twitch", "give", item="fish", target="alice"), {}, http_client=None
        )
    )
    assert result.detail == "give:insufficient"
    assert "viewer-1 has no fish to give" in _reply_text(fake_host)


def test_give_to_self_is_denied(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "viewer-1"))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "give", item="fish", target="viewer-1"),
            {},
            http_client=None,
        )
    )
    assert result.detail == "give:self"
    assert "cannot give an item to yourself" in _reply_text(fake_host)
    # the giver's own single fish must be untouched
    list_result = _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert list_result.detail == "list_self"
    assert "fish x1" in _reply_text(fake_host)


def test_give_does_not_require_mod_permission(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "viewer-1"))
    result = _run(
        dispatch(
            _sample_envelope("twitch", "give", item="fish", target="alice", is_mod=False),
            {},
            http_client=None,
        )
    )
    assert result.detail == "give"


def test_give_raises_on_malformed_payload(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "give", item=None, target=None)
    with pytest.raises(ValueError, match="malformed give payload"):
        _run(dispatch(envelope, {}, http_client=None))


# -- backend failures: fail loud, never silent ---------------------------------


def test_list_self_raises_and_replies_on_kv_get_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    import wit_world  # noqa: PLC0415 - installed by _install above

    def raising_get(key: str) -> bytes | None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    wit_world.imports.kv.get = raising_get

    with pytest.raises(RuntimeError, match="inventory kv_get failed"):
        _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    assert "unavailable" in _reply_text(host)
    error_logs = [(lvl, m) for lvl, m, _f in host.log_calls if m == "inventory.backend_error"]
    assert error_logs


def test_add_raises_and_replies_on_db_insert_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host = _FakeHost()
    _install(monkeypatch, host)
    host.db.raise_on["insert"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="inventory db_insert failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", item="fish", target="alice", is_mod=True),
                {},
                http_client=None,
            )
        )
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

    with pytest.raises(RuntimeError, match="inventory kv_set failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "add", item="fish", target="alice", is_mod=True),
                {},
                http_client=None,
            )
        )
    assert "unavailable" in _reply_text(host)


def test_list_other_raises_and_replies_on_a_direct_db_get_error(fake_host: _FakeHost) -> None:
    key = _scoped(_dir_key(_pseudonym("alice")))
    fake_host.kv_store[key] = json.dumps({"fish": _FIRST_ROW_ID}).encode()
    fake_host.db.raise_on["get"] = wit_fake_db.WitDbError(
        wit_fake_db.Error_Backend("connection lost")
    )

    with pytest.raises(RuntimeError, match="inventory db_get failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_grant_raises_and_replies_on_a_non_conflict_db_update_error(
    fake_host: _FakeHost,
) -> None:
    _run(_grant("add", "fish", "alice"))

    def raising_update(row_id: str, expected_version: int, column_values: Any) -> wit_fake_db.Row:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.update = raising_update

    with pytest.raises(RuntimeError, match="inventory db_update failed"):
        _run(_grant("add", "fish", "alice"))
    assert "unavailable" in _reply_text(fake_host)


def test_dir_get_fails_loud_on_corrupted_json(fake_host: _FakeHost) -> None:
    key = _scoped(_dir_key(_pseudonym("alice")))
    fake_host.kv_store[key] = b"{not valid json"
    with pytest.raises(RuntimeError, match="inventory dir_decode failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None
            )
        )
    assert "unavailable" in _reply_text(fake_host)


def test_dir_get_fails_loud_when_decoded_json_is_not_an_object(fake_host: _FakeHost) -> None:
    key = _scoped(_dir_key(_pseudonym("alice")))
    fake_host.kv_store[key] = json.dumps([1, 2, 3]).encode()
    with pytest.raises(RuntimeError, match="inventory dir_decode failed"):
        _run(
            dispatch(
                _sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None
            )
        )


def test_decrement_treats_a_stale_zero_quantity_row_as_insufficient(
    fake_host: _FakeHost,
) -> None:
    """A row left at quantity 0 by a skipped cleanup (see the conflict test below) must report.

    "nothing to remove", never silently go negative or crash.
    """
    _run(_grant("add", "fish", "alice"))

    def conflicting_delete(row_id: str, expected_version: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    real_delete = fake_host.db.delete
    wit_world.imports.db.delete = conflicting_delete
    _run(_grant("remove", "fish", "alice"))  # zeroes the row; cleanup delete conflicts, skipped
    wit_world.imports.db.delete = real_delete

    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "alice has no fish to remove" in _reply_text(fake_host)


def test_handle_list_filters_out_a_stale_zero_quantity_entry(fake_host: _FakeHost) -> None:
    """Same stale-zero-row setup as above, fed through listing instead of a second remove."""
    _run(_grant("add", "fish", "alice"))

    def conflicting_delete(row_id: str, expected_version: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    real_delete = fake_host.db.delete
    wit_world.imports.db.delete = conflicting_delete
    _run(_grant("remove", "fish", "alice"))
    wit_world.imports.db.delete = real_delete

    result = _run(
        dispatch(_sample_envelope("twitch", "list_other", target="alice"), {}, http_client=None)
    )
    assert result.detail == "list_other"
    assert _reply_text(fake_host) == "alice has no items."


def test_cleanup_raises_and_replies_on_a_non_conflict_delete_error(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "alice"))

    def raising_delete(row_id: str, expected_version: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Backend("connection lost"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.delete = raising_delete

    with pytest.raises(RuntimeError, match="inventory db_delete failed"):
        _run(_grant("remove", "fish", "alice"))
    assert "unavailable" in _reply_text(fake_host)


def test_maybe_cleanup_zero_row_skips_directory_write_when_entry_already_changed(
    fake_host: _FakeHost,
) -> None:
    """Direct unit coverage: the row still gets deleted, but a stale directory is untouched.

    A directory entry that no longer points at this row (changed
    concurrently) must never be overwritten with stale data.
    """
    from app import _maybe_cleanup_zero_row

    inserted_row = fake_host.db.insert(
        [
            wit_fake_db.ColumnValue(column="actor_hash", value=wit_fake_db.wrap("p")),
            wit_fake_db.ColumnValue(column="item", value=wit_fake_db.wrap("fish")),
            wit_fake_db.ColumnValue(column="quantity", value=wit_fake_db.wrap(0)),
        ]
    )
    community = "comm-1"
    pseudonym = "p"
    key = _scoped(_dir_key(pseudonym), community)
    fake_host.kv_store[key] = json.dumps({}).encode()  # no longer points at this row at all

    _run(
        _maybe_cleanup_zero_row(
            community,
            pseudonym,
            "fish",
            inserted_row.row_id,
            inserted_row.version,
            provider="twitch",
            channel_id="12345",
        )
    )
    assert inserted_row.row_id not in fake_host.db.rows
    set_calls = [c for c in fake_host.kv_calls if c[0] == "set" and c[1] == key]
    assert set_calls == []


def test_remove_cleanup_skips_delete_on_conflict_and_leaves_directory_intact(
    fake_host: _FakeHost,
) -> None:
    """A concurrent writer bumped the row after our decrement -- never destroy live data."""
    _run(_grant("add", "fish", "alice"))

    real_delete = fake_host.db.delete

    def conflicting_delete(row_id: str, expected_version: int) -> None:
        raise wit_fake_db.WitDbError(wit_fake_db.Error_Conflict("version mismatch"))

    import wit_world  # noqa: PLC0415 - installed by the fake_host fixture

    wit_world.imports.db.delete = conflicting_delete

    result = _run(_grant("remove", "fish", "alice"))
    assert result.detail == "remove"
    assert "now have 0" in _reply_text(fake_host)
    # the row was NOT deleted (delete call raised a conflict, caught and skipped)
    assert len(fake_host.db.rows) == 1
    # the directory entry is still present too, since the delete never happened
    key = _scoped(_dir_key(_pseudonym("alice")))
    assert json.loads(fake_host.kv_store[key].decode()) != {}

    wit_world.imports.db.delete = real_delete


def test_list_other_raises_on_a_malformed_payload_missing_target(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "list_other", target=None)
    with pytest.raises(ValueError, match="malformed list_other payload"):
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


def test_normalize_item_strips_and_lowercases() -> None:
    assert _normalize_item("  Fish  ") == "fish"
    assert _normalize_item("SWORD") == "sword"


def test_dispatch_never_persists_the_raw_target_name_in_kv_or_db(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "SuperSecretUser99"))
    for key in fake_host.kv_store:
        assert "SuperSecretUser99" not in key
        assert "supersecretuser99" not in key
    for row in fake_host.db.rows.values():
        assert "SuperSecretUser99" not in str(row.get("actor_hash", ""))


def test_dispatch_never_logs_the_raw_actor_or_target(fake_host: _FakeHost) -> None:
    _run(_grant("add", "fish", "SuperSecretUser99"))
    _run(dispatch(_sample_envelope("twitch", "list_self"), {}, http_client=None))
    for _level, message, fields_json in fake_host.log_calls:
        assert "viewer-1" not in message
        assert "viewer-1" not in fields_json
        assert "SuperSecretUser99" not in message
        assert "SuperSecretUser99" not in fields_json


# -- _format_grant_reply() / _caller_role_signal() unit coverage ---------------


def test_format_grant_reply_add() -> None:
    text = _format_grant_reply("add", "fish", "alice", 4)
    assert text == "Gave 1 fish to alice. They now have 4."


def test_format_grant_reply_remove() -> None:
    text = _format_grant_reply("remove", "fish", "alice", 0)
    assert text == "Removed 1 fish from alice. They now have 0."


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_from_mod() -> None:
    assert _caller_role_signal({"is_mod": True}) is True


def test_caller_role_signal_false_when_both_false() -> None:
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False
