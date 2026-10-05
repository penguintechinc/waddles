"""Host-native tests for the `chat` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime -- see `bundles/python/count/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors (fake `flags`/
`log`/`relay`). The `db` host call is faked one level below `wit_world`:
`waddle_sdk.db._cross` is this facade's own one-and-only WIT crossing
point (its docstring: "the one place every statement crosses the WIT `db`
import"), and this bundle only ever issues one query shape (`SELECT * FROM
hub_chat_messages WHERE hub_chat_messages.community_id = $1`), so faking
`_cross` directly -- rather than re-implementing the WIT `Value` variant
marshalling `waddle_sdk.db` already owns and tests itself -- is the
correct seam for this bundle's own tests.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest
import waddle_sdk.db as db_module
from app import (
    ChatChannel,
    ChatMessage,
    _format_channels,
    _format_chat_history,
    dispatch,
    transform,
)
from waddle_sdk.db import AsyncDB
from waddle_sdk.flask_core import (
    PlatformEvent,
    StageEnvelope,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)

TENANT_ID = "tenant-1"
COMMUNITY_ID = 1


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake WIT host: `flags.enabled` True by default, recording `log`/`relay`, faked `db`."""
    flag_state = {"enabled": True}
    log_calls: list[tuple[int, str, str]] = []
    relay_calls: list[tuple[str, dict[str, Any]]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_state["enabled"])
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((lvl, msg, fields_json)),
    )
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, log=log_mod, relay=relay_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    db_rows: list[dict[str, Any]] = []
    fail_box: dict[str, Exception | None] = {"exc": None}
    cross_calls: list[tuple[str, list[Any]]] = []

    def _fake_cross(statement: str, params: list[Any]) -> tuple[list[dict[str, Any]], int]:
        cross_calls.append((statement, list(params)))
        if fail_box["exc"] is not None:
            exc = fail_box["exc"]
            fail_box["exc"] = None
            raise exc
        assert "hub_chat_messages" in statement
        community_id = params[0] if params else None
        matched = [dict(r) for r in db_rows if r["community_id"] == community_id]
        return matched, len(matched)

    monkeypatch.setattr(db_module, "_cross", _fake_cross)
    dal = AsyncDB()
    set_bundle_dal(dal)

    yield types.SimpleNamespace(
        flag_state=flag_state,
        log_calls=log_calls,
        relay_calls=relay_calls,
        rows=db_rows,
        fail=fail_box,
        cross_calls=cross_calls,
        dal=dal,
    )
    reset_bundle_dal_for_tests()


def _row(
    *,
    id: int,
    community_id: int = COMMUNITY_ID,
    channel_name: str | None = "general",
    sender_username: str | None,
    message_content: str,
    message_type: str = "text",
    created_at: str | None,
) -> dict[str, Any]:
    return {
        "id": id,
        "community_id": community_id,
        "channel_name": channel_name,
        "sender_username": sender_username,
        "message_content": message_content,
        "message_type": message_type,
        "created_at": created_at,
    }


def _event(text: Any, **payload_overrides: object) -> PlatformEvent:
    default_payload: dict[str, Any] = {"text": text, "channel_id": "chan-1"}
    default_payload.update(payload_overrides)
    return PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="test_user",
        payload=default_payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


def _with_community() -> Any:
    return bundle_context(
        tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.core.example.chat"
    )


class TestTransformRouting:
    """Cheap-skip / flag-gate / non-matching routing."""

    @pytest.mark.parametrize("text", ["hello", "", "   ", "chat-history", "channels"])
    def test_no_leading_bang_ignored(self, text: str, fake_host: Any) -> None:
        with _with_community():
            assert _run(transform(_event(text))) is None
        assert fake_host.cross_calls == []  # cheap-skip: zero db ops

    def test_unrecognized_command_ignored(self, fake_host: Any) -> None:
        with _with_community():
            assert _run(transform(_event("!unknown"))) is None
        assert fake_host.cross_calls == []

    def test_command_in_middle_of_text_ignored(self, fake_host: Any) -> None:
        with _with_community():
            assert _run(transform(_event("please !chat-history for me"))) is None

    def test_non_chat_payload_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="channel.follow",
            actor=None,
            payload={"channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with _with_community():
            assert _run(transform(event)) is None

    def test_missing_text_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor=None,
            payload={"channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with _with_community():
            assert _run(transform(event)) is None

    def test_non_string_text_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor=None,
            payload={"text": 123, "channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with _with_community():
            assert _run(transform(event)) is None

    def test_disabled_flag_suppresses_reply(self, fake_host: Any) -> None:
        fake_host.flag_state["enabled"] = False
        with _with_community():
            assert _run(transform(_event("!chat-history"))) is None
            assert _run(transform(_event("!channels"))) is None
        assert fake_host.cross_calls == []


class TestChatHistoryCommand:
    """`!chat-history` / `!chat-history list`."""

    def test_bare_command_returns_history(self, fake_host: Any) -> None:
        fake_host.rows.append(
            _row(id=1, sender_username="alice", message_content="hello", created_at="2026-01-01")
        )
        with _with_community():
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "Chat History" in result.payload["text"]
        assert "alice" in result.payload["text"]

    def test_list_option_is_equivalent(self, fake_host: Any) -> None:
        fake_host.rows.append(
            _row(id=1, sender_username="alice", message_content="hello", created_at="2026-01-01")
        )
        with _with_community():
            result = _run(transform(_event("!chat-history list")))
        assert result is not None
        assert "alice" in result.payload["text"]

    def test_empty_history(self, fake_host: Any) -> None:
        with _with_community():
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "no messages found" in result.payload["text"]

    def test_other_community_messages_excluded(self, fake_host: Any) -> None:
        fake_host.rows.append(
            _row(
                id=1,
                community_id=999,
                sender_username="bob",
                message_content="other community",
                created_at="2026-01-01",
            )
        )
        with _with_community():
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "bob" not in result.payload["text"]
        assert "no messages found" in result.payload["text"]

    def test_unknown_option_is_usage_error_reply(self, fake_host: Any) -> None:
        with _with_community():
            result = _run(transform(_event("!chat-history bogus")))
        assert result is not None
        assert result.payload["text"]  # non-empty, fail-loud, never a silent no-op

    def test_recognized_but_unsupported_verb_is_usage_reply(self, fake_host: Any) -> None:
        """`set`/`add`/etc. are valid grammar verbs, but neither command supports them."""
        with _with_community():
            result = _run(transform(_event("!chat-history set")))
        assert result is not None
        assert result.payload["text"] == "Usage: !chat-history [list]"

    def test_non_numeric_community_treated_as_no_community(self, fake_host: Any) -> None:
        with bundle_context(
            tenant=TENANT_ID, community="not-a-number", app_id="waddles.core.example.chat"
        ):
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "community" in result.payload["text"].lower()
        assert fake_host.cross_calls == []

    def test_case_insensitive(self, fake_host: Any) -> None:
        fake_host.rows.append(
            _row(id=1, sender_username="alice", message_content="hi", created_at="2026-01-01")
        )
        with _with_community():
            for text in ["!CHAT-HISTORY", "!Chat-History"]:
                result = _run(transform(_event(text)))
                assert result is not None

    def test_no_community_context(self, fake_host: Any) -> None:
        with bundle_context(
            tenant=TENANT_ID, community=None, app_id="waddles.core.example.chat"
        ):
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "community" in result.payload["text"].lower()
        assert fake_host.cross_calls == []

    def test_db_failure_is_fail_loud_reply_and_logged(self, fake_host: Any) -> None:
        fake_host.fail["exc"] = db_module.DALError("db.execute failed: boom")
        with _with_community():
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert result.payload["text"]
        assert any(call[1] == "chat.db_failure" for call in fake_host.log_calls)

    def test_preserves_channel_id_and_metadata(self, fake_host: Any) -> None:
        with _with_community():
            result = _run(transform(_event("!chat-history", author_id="123")))
        assert result is not None
        assert result.payload["channel_id"] == "chan-1"
        assert result.payload.get("author_id") == "123"
        assert result.platform == "discord"
        assert result.event_type == "chat.message"
        assert result.actor == "test_user"
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"

    def test_oldest_first_ordering(self, fake_host: Any) -> None:
        fake_host.rows.extend(
            [
                _row(id=1, sender_username="a", message_content="first", created_at="2026-01-01"),
                _row(id=2, sender_username="b", message_content="second", created_at="2026-01-02"),
            ]
        )
        with _with_community():
            result = _run(transform(_event("!chat-history")))
        assert result is not None
        text = result.payload["text"]
        assert text.index("first") < text.index("second")


class TestChannelsCommand:
    """`!channels` / `!channels list`."""

    def test_bare_command_lists_channels(self, fake_host: Any) -> None:
        fake_host.rows.append(
            _row(
                id=1,
                channel_name="general",
                sender_username="alice",
                message_content="hi",
                created_at="2026-01-01",
            )
        )
        with _with_community():
            result = _run(transform(_event("!channels")))
        assert result is not None
        assert "Chat Channels" in result.payload["text"]
        assert "general" in result.payload["text"]

    def test_list_option_is_equivalent(self, fake_host: Any) -> None:
        with _with_community():
            result = _run(transform(_event("!channels list")))
        assert result is not None
        assert "general" in result.payload["text"]  # sentinel, zero messages

    def test_empty_channels_still_shows_general_sentinel(self, fake_host: Any) -> None:
        with _with_community():
            result = _run(transform(_event("!channels")))
        assert result is not None
        assert "general" in result.payload["text"]
        assert "0 messages" in result.payload["text"]

    def test_aggregates_message_counts_per_channel(self, fake_host: Any) -> None:
        fake_host.rows.extend(
            [
                _row(
                    id=1,
                    channel_name="random",
                    sender_username="a",
                    message_content="x",
                    created_at="2026-01-01",
                ),
                _row(
                    id=2,
                    channel_name="random",
                    sender_username="b",
                    message_content="y",
                    created_at="2026-01-02",
                ),
            ]
        )
        with _with_community():
            result = _run(transform(_event("!channels")))
        assert result is not None
        assert "random: 2 messages" in result.payload["text"]

    def test_db_failure_is_fail_loud_reply_and_logged(self, fake_host: Any) -> None:
        fake_host.fail["exc"] = db_module.DALError("db.execute failed: boom")
        with _with_community():
            result = _run(transform(_event("!channels")))
        assert result is not None
        assert result.payload["text"]
        assert any(call[1] == "chat.db_failure" for call in fake_host.log_calls)

    def test_no_community_context(self, fake_host: Any) -> None:
        with bundle_context(
            tenant=TENANT_ID, community=None, app_id="waddles.core.example.chat"
        ):
            result = _run(transform(_event("!channels")))
        assert result is not None
        assert "community" in result.payload["text"].lower()


class TestDispatch:
    """`dispatch` is a pure relay of the text `transform` already built."""

    def test_relays_text_to_platform(self, fake_host: Any) -> None:
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=str(COMMUNITY_ID),
            app_id="waddles.core.example.chat",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor="test_user",
                payload={"channel_id": "chan-1", "text": "Chat Channels: general: 0 messages"},
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
            ts="2026-01-01T00:00:00+00:00",
        )
        result = _run(dispatch(envelope, {}, http_client=None))
        assert result.transport == "discord"
        # `relay.push` serializes to canonical JSON text before crossing the WIT boundary
        # (`waddle_sdk.relay.push`'s own docstring) -- the fake `relay` module records exactly
        # what crosses, i.e. the JSON string, not the original dict.
        assert fake_host.relay_calls == [
            ("discord", '{"channel": "chan-1", "text": "Chat Channels: general: 0 messages"}')
        ]

    def test_missing_channel_id_raises(self, fake_host: Any) -> None:
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=str(COMMUNITY_ID),
            app_id="waddles.core.example.chat",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor=None,
                payload={"text": "hi"},
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
            ts="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="channel_id"):
            _run(dispatch(envelope, {}, http_client=None))

    def test_missing_text_raises(self, fake_host: Any) -> None:
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=str(COMMUNITY_ID),
            app_id="waddles.core.example.chat",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor=None,
                payload={"channel_id": "chan-1"},
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
            ts="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            _run(dispatch(envelope, {}, http_client=None))


class TestFormatChatHistory:
    """Pure-function tests for `_format_chat_history`."""

    def test_empty_list(self) -> None:
        assert _format_chat_history([]) == "(no messages found)"

    def test_single_message(self) -> None:
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="hello world",
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        result = _format_chat_history([msg])
        assert "Chat History" in result
        assert "alice" in result
        assert "hello world" in result
        assert "2026-01-01" in result

    def test_truncates_long_message_content(self) -> None:
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="x" * 200,
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        result = _format_chat_history([msg])
        assert len(result) < 4100

    def test_missing_created_at(self) -> None:
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="hello",
            message_type="text",
            created_at=None,
        )
        assert "?" in _format_chat_history([msg])

    def test_missing_sender_username(self) -> None:
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username=None,
            content="hello",
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        assert "unknown" in _format_chat_history([msg])

    def test_truncates_entire_output_if_too_long(self) -> None:
        # `lines[:_MAX_HISTORY_MESSAGES]` caps the OUTPUT at 20 lines (header + 19 messages,
        # see `_format_chat_history`'s own comment on the source's slicing quirk), so a long
        # per-line length -- not message count -- is what's needed to push the 19-message,
        # 20-line cap over `_MAX_REPLY_CHARS` and actually exercise the truncation branch.
        msgs = [
            ChatMessage(
                id=i,
                community_id=1,
                channel_name="general",
                sender_username="very_long_username_" * 15,
                content="message content " * 10,
                message_type="text",
                created_at="2026-01-01T12:00:00",
            )
            for i in range(50)
        ]
        result = _format_chat_history(msgs)
        assert result.endswith("...(truncated)")
        assert len(result) <= 4100


class TestFormatChannels:
    """Pure-function tests for `_format_channels`."""

    def test_empty_list(self) -> None:
        assert _format_channels([]) == "(no channels found)"

    def test_single_channel(self) -> None:
        ch = ChatChannel(name="general", message_count=42, last_message_at="2026-01-01T12:00:00")
        result = _format_channels([ch])
        assert "Chat Channels" in result
        assert "general" in result
        assert "42" in result

    def test_truncates_if_too_long(self) -> None:
        channels = [
            ChatChannel(
                name=f"channel_with_a_very_long_name_{i}",
                message_count=1000000,
                last_message_at="2026-01-01T12:00:00",
            )
            for i in range(100)
        ]
        result = _format_channels(channels)
        assert len(result) <= 4100
