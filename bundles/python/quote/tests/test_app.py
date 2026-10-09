"""Host-native tests for the `quote` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- see `bundles/python/rank/tests/test_app.py`'s own docstring for the
fake-`wit_world` approach this mirrors, extended with a fake `db` (`wit_fake_db.py`, this
bundle's own small reproduction of `sdk/waddle-sdk/tests/wit_fake_db.py`'s shape, identical to
`bundles/python/rank/tests/wit_fake_db.py`'s own copy, plus a `delete` op `rank` never needed).

**`kv` uses the SHARED charset-enforcing fake** (`waddle_sdk.testing.install_fake_kv_host`,
gh-631), not a hand-rolled permissive stand-in -- this bundle's own `_index_key()` prefix is
`.`-separated (`quote.rowid.<seq>`, never `:`), and this fake fails any test immediately if
that ever regresses to a colon.
"""

from __future__ import annotations

import asyncio
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
    _USAGE,
    _caller_role_signal,
    _format_quote,
    _index_key,
    _resolve_command,
    dispatch,
    transform,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scoped(key: str, community: str = "comm-1") -> str:
    """`fake_host.kv.store` is keyed by `community_kv`'s own `c.<community>.<key>` prefix."""
    return _scoped_key(community, key)


class _FakeHost:
    """Fake WIT host: flags/kv/db/relay/log -- `kv` is the shared charset-enforcing fake."""

    def __init__(self, kv: FakeKvHost) -> None:
        self.kv = kv
        self.db = wit_fake_db.FakeDb()
        self.relay_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[int, str, str]] = []


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
    import wit_world  # noqa: PLC0415 - installed into sys.modules by install_fake_kv_host above

    wit_world.imports.flags = flags_mod  # type: ignore[attr-defined]
    wit_world.imports.db = db_mod  # type: ignore[attr-defined]
    wit_world.imports.relay = relay_mod  # type: ignore[attr-defined]
    wit_world.imports.log = log_mod  # type: ignore[attr-defined]
    return host


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
        occurred_at="2026-10-08T00:00:00.000Z",
    )


def _sample_envelope(
    platform: str,
    command: str,
    *,
    community: str | None = "comm-1",
    actor: str | None = "viewer-1",
    arg: str | None = None,
    raw_option: str | None = None,
    is_mod: bool | None = None,
    is_broadcaster: bool | None = None,
    channel_id: str | None = "12345",
) -> StageEnvelope:
    payload: dict[str, Any] = {"command": command, "channel_id": channel_id}
    if arg is not None:
        payload["arg"] = arg
    if raw_option is not None:
        payload["raw_option"] = raw_option
    if is_mod is not None:
        payload["is_mod"] = is_mod
    if is_broadcaster is not None:
        payload["is_broadcaster"] = is_broadcaster
    return StageEnvelope(
        tenant="tenant-1",
        community=community,
        app_id="waddles.core.example.quote",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=actor,
            payload=payload,
            occurred_at="2026-10-08T00:00:00.000Z",
        ),
        ts="2026-10-08T00:00:00.000Z",
    )


def _reply_text(host: _FakeHost) -> str:
    _provider, message_json = host.relay_calls[-1]
    return str(json.loads(message_json)["text"])


# -- transform() parsing -------------------------------------------------------


@pytest.mark.parametrize("text", ["!quote", "!QUOTE", "  !quote  "])
def test_bare_quote_matches_case_insensitively_and_with_whitespace(
    text: str, fake_host: _FakeHost
) -> None:
    result = _run(transform(_sample_event(text)))
    assert result is not None
    assert result.payload["command"] == "usage"


def test_quote_random_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote random")))
    assert result is not None
    assert result.payload["command"] == "random"


def test_quote_bare_digit_matches_get(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote 7")))
    assert result is not None
    assert result.payload["command"] == "get"
    assert result.payload["arg"] == "7"


def test_quote_list_matches(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote list")))
    assert result is not None
    assert result.payload["command"] == "list"


def test_quote_add_matches_with_raw_text(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote add hello world")))
    assert result is not None
    assert result.payload["command"] == "add"
    assert result.payload["arg"] == "hello world"


def test_quote_remove_matches_with_target(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote remove 3")))
    assert result is not None
    assert result.payload["command"] == "remove"
    assert result.payload["arg"] == "3"


def test_quote_unsupported_verb_is_unknown(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote set foo")))
    assert result is not None
    assert result.payload["command"] == "unknown"
    assert result.payload["raw_option"] == "set"


def test_quote_unparseable_multi_word_is_usage_with_message(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote frobnicate bar")))
    assert result is not None
    assert result.payload["command"] == "usage"
    assert isinstance(result.payload.get("arg"), str)


@pytest.mark.parametrize("text", ["!quotex", "quote", "!quoter", "hello", ""])
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
    assert _run(transform(_sample_event("!quote"))) is None


def test_transform_forwards_present_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote", is_mod=True, is_broadcaster=False)))
    assert result is not None
    assert result.payload["is_mod"] is True
    assert result.payload["is_broadcaster"] is False


def test_transform_omits_absent_badge_fields(fake_host: _FakeHost) -> None:
    result = _run(transform(_sample_event("!quote")))
    assert result is not None
    assert "is_mod" not in result.payload
    assert "is_broadcaster" not in result.payload


# -- _resolve_command() / _caller_role_signal() direct unit coverage -----------


def test_resolve_command_bare_is_usage() -> None:
    spec = CommandSpec(name="quote", sub_modules=frozenset())
    parsed = parse_command("!quote", spec)
    assert _resolve_command(parsed) == ("usage", None, None)


def test_resolve_command_add_carries_raw_args() -> None:
    parsed = ParsedCommand(command="quote", sub_module=None, option="add", args="hi there")
    assert _resolve_command(parsed) == ("add", "hi there", None)


def test_resolve_command_unimplemented_option_is_unknown() -> None:
    parsed = ParsedCommand(command="quote", sub_module=None, option="set", args=None)
    assert _resolve_command(parsed) == ("unknown", None, "set")


def test_caller_role_signal_absent_is_none() -> None:
    assert _caller_role_signal({}) is None


def test_caller_role_signal_true_for_mod_or_broadcaster() -> None:
    assert _caller_role_signal({"is_mod": True}) is True
    assert _caller_role_signal({"is_broadcaster": True}) is True
    assert _caller_role_signal({"is_mod": False, "is_broadcaster": False}) is False


def test_format_quote_never_shows_a_real_author() -> None:
    assert _format_quote(1, "hi") == '#1: "hi" — unknown'


# -- dispatch: add ---------------------------------------------------------------


def test_add_requires_privilege(fake_host: _FakeHost) -> None:
    result = _run(
        dispatch(_sample_envelope("twitch", "add", arg="hi", is_mod=False), {}, http_client=None)
    )
    assert result.detail == "add"
    assert "Only the broadcaster or a moderator" in _reply_text(fake_host)
    assert fake_host.db.calls == []


def test_add_fails_closed_when_role_info_absent(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="hi"), {}, http_client=None))
    assert "Only the broadcaster or a moderator" in _reply_text(fake_host)


def test_add_empty_text_is_usage(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg=" ", is_mod=True), {}, http_client=None))
    assert _reply_text(fake_host) == "Usage: !quote add <text>"


def test_add_too_long_is_rejected(fake_host: _FakeHost) -> None:
    long_text = "x" * 501
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg=long_text, is_mod=True), {}, http_client=None
        )
    )
    assert _reply_text(fake_host) == "Quotes must be 500 characters or fewer."
    assert fake_host.db.calls == []


def test_add_success_assigns_seq_one_then_two(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hello world", is_mod=True),
            {},
            http_client=None,
        )
    )
    assert _reply_text(fake_host) == "Saved as quote #1."
    _run(
        dispatch(_sample_envelope("twitch", "add", arg="second", is_mod=True), {}, http_client=None)
    )
    assert _reply_text(fake_host) == "Saved as quote #2."
    assert len(fake_host.db.rows) == 2


def test_add_as_broadcaster_succeeds(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hi", is_broadcaster=True), {}, http_client=None
        )
    )
    assert _reply_text(fake_host) == "Saved as quote #1."


def test_add_writes_the_kv_rowid_index(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="hi", is_mod=True), {}, http_client=None))
    assert _scoped(_index_key(1)) in fake_host.kv.store


# -- dispatch: get -----------------------------------------------------------------


def test_get_not_found(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "get", arg="42"), {}, http_client=None))
    assert _reply_text(fake_host) == "Quote #42 not found."


def test_get_found(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hello world", is_mod=True), {}, http_client=None
        )
    )
    _run(dispatch(_sample_envelope("twitch", "get", arg="1"), {}, http_client=None))
    assert _reply_text(fake_host) == '#1: "hello world" — unknown'


def test_get_scoped_per_community(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hi", is_mod=True, community="comm-1"),
            {},
            http_client=None,
        )
    )
    _run(
        dispatch(
            _sample_envelope("twitch", "get", arg="1", community="comm-2"), {}, http_client=None
        )
    )
    assert _reply_text(fake_host) == "Quote #1 not found."


# -- dispatch: random --------------------------------------------------------------


def test_random_no_quotes(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "random"), {}, http_client=None))
    assert _reply_text(fake_host) == "No quotes found."


def test_random_found(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hello world", is_mod=True), {}, http_client=None
        )
    )
    _run(dispatch(_sample_envelope("twitch", "random"), {}, http_client=None))
    assert _reply_text(fake_host) == '#1: "hello world" — unknown'


# -- dispatch: list -----------------------------------------------------------------


def test_list_empty(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    assert _reply_text(fake_host) == "No quotes have been saved yet."


def test_list_with_items(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="one", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "add", arg="two", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "list"), {}, http_client=None))
    text = _reply_text(fake_host)
    assert "#1" in text
    assert "#2" in text


# -- dispatch: remove ---------------------------------------------------------------


def test_remove_requires_privilege(fake_host: _FakeHost) -> None:
    _run(
        dispatch(_sample_envelope("twitch", "remove", arg="1", is_mod=False), {}, http_client=None)
    )
    assert "Only the broadcaster or a moderator" in _reply_text(fake_host)


def test_remove_invalid_id_is_usage(fake_host: _FakeHost) -> None:
    _run(
        dispatch(_sample_envelope("twitch", "remove", arg="abc", is_mod=True), {}, http_client=None)
    )
    assert _reply_text(fake_host) == "Usage: !quote remove <id>"


def test_remove_not_found(fake_host: _FakeHost) -> None:
    _run(
        dispatch(_sample_envelope("twitch", "remove", arg="99", is_mod=True), {}, http_client=None)
    )
    assert _reply_text(fake_host) == "Quote #99 not found."


def test_remove_success_then_gone(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="hello world", is_mod=True), {}, http_client=None
        )
    )
    _run(dispatch(_sample_envelope("twitch", "remove", arg="1", is_mod=True), {}, http_client=None))
    assert _reply_text(fake_host) == "Removed quote #1."
    _run(dispatch(_sample_envelope("twitch", "get", arg="1"), {}, http_client=None))
    assert _reply_text(fake_host) == "Quote #1 not found."
    assert fake_host.db.rows == {}


def test_remove_deletes_the_kv_rowid_index(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="hi", is_mod=True), {}, http_client=None))
    _run(dispatch(_sample_envelope("twitch", "remove", arg="1", is_mod=True), {}, http_client=None))
    assert _scoped(_index_key(1)) not in fake_host.kv.store


# -- dispatch: usage / unknown --------------------------------------------------------


def test_usage_command_replies_usage_text(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "usage"), {}, http_client=None))
    assert _reply_text(fake_host) == _USAGE


def test_usage_command_with_message_prefixes_it(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "usage", arg="bad input"), {}, http_client=None))
    assert _reply_text(fake_host) == f"bad input | {_USAGE}"


def test_unknown_command_names_the_option(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "unknown", raw_option="set"), {}, http_client=None))
    assert _reply_text(fake_host) == f"Unknown quote command 'set'. {_USAGE}"


# -- dispatch: structural failures ----------------------------------------------------


def test_dispatch_missing_channel_id_raises(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "usage", channel_id=None)
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_unrecognized_command_raises(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "bogus")
    with pytest.raises(ValueError, match="unrecognized quote command"):
        _run(dispatch(envelope, {}, http_client=None))


def test_dispatch_missing_community_raises(fake_host: _FakeHost) -> None:
    envelope = _sample_envelope("twitch", "random", community=None)
    with pytest.raises(ValueError, match="community"):
        _run(dispatch(envelope, {}, http_client=None))


# -- dispatch: backend failures are fail-loud ------------------------------------------


def test_db_insert_failure_is_fail_loud(fake_host: _FakeHost) -> None:
    fake_host.db.raise_on["insert"] = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="quote db_insert failed"):
        _run(
            dispatch(_sample_envelope("twitch", "add", arg="hi", is_mod=True), {}, http_client=None)
        )
    assert _reply_text(fake_host) == (
        "Something went wrong accessing quote storage - please try again."
    )
    assert any(call[1] == "quote.backend_error" for call in fake_host.log_calls)


def test_db_query_failure_is_fail_loud(fake_host: _FakeHost) -> None:
    fake_host.db.raise_on["query"] = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="quote db_query failed"):
        _run(dispatch(_sample_envelope("twitch", "random"), {}, http_client=None))


def test_get_index_points_at_missing_row_fails_loud(fake_host: _FakeHost) -> None:
    _run(dispatch(_sample_envelope("twitch", "add", arg="hi", is_mod=True), {}, http_client=None))
    # Corrupt the index: point seq 1 at a row_id that never existed.
    fake_host.db.rows.clear()
    fake_host.db.versions.clear()
    with pytest.raises(RuntimeError, match="quote index_stale failed"):
        _run(dispatch(_sample_envelope("twitch", "get", arg="1"), {}, http_client=None))


# -- dispatch: PII-free logging ---------------------------------------------------------


def test_dispatch_never_logs_quote_text_or_target(fake_host: _FakeHost) -> None:
    _run(
        dispatch(
            _sample_envelope("twitch", "add", arg="super secret quote text", is_mod=True),
            {},
            http_client=None,
        )
    )
    for _level, _msg, fields_json in fake_host.log_calls:
        assert "super secret quote text" not in fields_json
