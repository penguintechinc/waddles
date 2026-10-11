"""Host-native tests for the `chat` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime -- see `bundles/python/pyping/tests/test_app.py`'s own
docstring for the fake-`wit_world` approach this mirrors. This bundle
issues no `db`/`kv` host calls at all (see `app.py`'s own module docstring
for why -- the structured `waddle_sdk.db` facade has no path to read the
hub-owned `hub_chat_messages` table), so the fake host here only ever
needs `flags`/`log`/`relay`.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest
from app import DispatchResult, dispatch, transform
from waddle_sdk.flask_core import PlatformEvent, StageEnvelope

TENANT_ID = "tenant-1"
COMMUNITY_ID = "1"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Fake WIT host: `flags.enabled` True by default, recording `log`/`relay` calls."""
    flag_state = {"enabled": True}
    log_calls: list[tuple[str, str]] = []
    relay_calls: list[tuple[str, str]] = []

    flags_mod = types.SimpleNamespace(enabled=lambda key, default_value: flag_state["enabled"])
    log_mod = types.SimpleNamespace(
        Level={"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3},
        write=lambda lvl, msg, fields_json: log_calls.append((msg, fields_json)),
    )
    relay_mod = types.SimpleNamespace(
        push=lambda provider, msg: relay_calls.append((provider, msg))
    )
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        flags=flags_mod, log=log_mod, relay=relay_mod
    )
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    yield types.SimpleNamespace(flag_state=flag_state, log_calls=log_calls, relay_calls=relay_calls)


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


class TestTransformRouting:
    """Cheap-skip / flag-gate / non-matching routing."""

    @pytest.mark.parametrize("text", ["hello", "", "   ", "chat-history", "channels"])
    def test_no_leading_bang_ignored(self, text: str, fake_host: Any) -> None:
        assert _run(transform(_event(text))) is None

    def test_unrecognized_command_ignored(self, fake_host: Any) -> None:
        assert _run(transform(_event("!unknown"))) is None

    def test_command_in_middle_of_text_ignored(self, fake_host: Any) -> None:
        assert _run(transform(_event("please !chat-history for me"))) is None

    def test_non_chat_payload_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="channel.follow",
            actor=None,
            payload={"channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        assert _run(transform(event)) is None

    def test_missing_text_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor=None,
            payload={"channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        assert _run(transform(event)) is None

    def test_non_string_text_ignored(self, fake_host: Any) -> None:
        event = PlatformEvent(
            platform="discord",
            event_type="chat.message",
            actor=None,
            payload={"text": 123, "channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        assert _run(transform(event)) is None

    def test_disabled_flag_suppresses_reply(self, fake_host: Any) -> None:
        fake_host.flag_state["enabled"] = False
        assert _run(transform(_event("!chat-history"))) is None
        assert _run(transform(_event("!channels"))) is None


class TestChatHistoryCommand:
    """`!chat-history` / `!chat-history list` -- always the architecture-gap reply today."""

    def test_bare_command_replies_unavailable(self, fake_host: Any) -> None:
        result = _run(transform(_event("!chat-history")))
        assert result is not None
        assert "aren't available from this bundle yet" in result.payload["text"]
        assert "issues/678" in result.payload["text"]

    def test_list_option_is_equivalent(self, fake_host: Any) -> None:
        result = _run(transform(_event("!chat-history list")))
        assert result is not None
        assert "aren't available from this bundle yet" in result.payload["text"]

    def test_unknown_option_is_usage_error_reply(self, fake_host: Any) -> None:
        result = _run(transform(_event("!chat-history bogus")))
        assert result is not None
        assert result.payload["text"]  # non-empty, fail-loud, never a silent no-op

    def test_recognized_but_unsupported_verb_is_usage_reply(self, fake_host: Any) -> None:
        """`set`/`add`/etc. are valid grammar verbs, but neither command supports them."""
        result = _run(transform(_event("!chat-history set")))
        assert result is not None
        assert result.payload["text"] == "Usage: !chat-history [list]"

    def test_case_insensitive(self, fake_host: Any) -> None:
        for text in ["!CHAT-HISTORY", "!Chat-History"]:
            result = _run(transform(_event(text)))
            assert result is not None
            assert "aren't available from this bundle yet" in result.payload["text"]

    def test_logs_warn_and_info(self, fake_host: Any) -> None:
        _run(transform(_event("!chat-history")))
        messages = [m for m, _ in fake_host.log_calls]
        assert "chat.read_unavailable" in messages
        assert "chat.transform matched" in messages

    def test_preserves_channel_id_and_metadata(self, fake_host: Any) -> None:
        result = _run(transform(_event("!chat-history", author_id="123")))
        assert result is not None
        assert result.payload["channel_id"] == "chan-1"
        assert result.payload.get("author_id") == "123"
        assert result.platform == "discord"
        assert result.event_type == "chat.message"
        assert result.actor == "test_user"
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"


class TestChannelsCommand:
    """`!channels` / `!channels list` -- always the architecture-gap reply today."""

    def test_bare_command_replies_unavailable(self, fake_host: Any) -> None:
        result = _run(transform(_event("!channels")))
        assert result is not None
        assert "aren't available from this bundle yet" in result.payload["text"]
        assert "issues/678" in result.payload["text"]

    def test_list_option_is_equivalent(self, fake_host: Any) -> None:
        result = _run(transform(_event("!channels list")))
        assert result is not None
        assert "aren't available from this bundle yet" in result.payload["text"]

    def test_unknown_option_is_usage_error_reply(self, fake_host: Any) -> None:
        result = _run(transform(_event("!channels bogus")))
        assert result is not None
        assert result.payload["text"]

    def test_logs_warn_and_info(self, fake_host: Any) -> None:
        _run(transform(_event("!channels")))
        messages = [m for m, _ in fake_host.log_calls]
        assert "chat.read_unavailable" in messages
        assert "chat.transform matched" in messages


class TestDispatch:
    """`dispatch` is a pure relay of the text `transform` already built."""

    def test_relays_text_to_platform(self, fake_host: Any) -> None:
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=COMMUNITY_ID,
            app_id="waddles.core.example.chat",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor="test_user",
                payload={"channel_id": "chan-1", "text": "some reply text"},
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
            ts="2026-01-01T00:00:00+00:00",
        )
        result = _run(dispatch(envelope, {}, http_client=None))
        assert isinstance(result, DispatchResult)
        assert result.transport == "discord"
        # `relay.push` serializes to canonical JSON text before crossing the WIT boundary
        # (`waddle_sdk.relay.push`'s own docstring) -- the fake `relay` module records exactly
        # what crosses, i.e. the JSON string, not the original dict.
        assert fake_host.relay_calls == [
            ("discord", '{"channel": "chan-1", "text": "some reply text"}')
        ]

    def test_missing_channel_id_raises(self, fake_host: Any) -> None:
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=COMMUNITY_ID,
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
            community=COMMUNITY_ID,
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


class TestRegressionsAndFailLoud:
    """gh-678 (no serving capability) + gh-674 (PII-free logs) + fail-closed flag."""

    # regression: gh-678 -- the read has no serving capability; every recognized invocation must
    # say so loudly (explicit reply citing the tracking issue + a WARN log), never go silent,
    # never fall back to an empty/fake "no messages" result.
    @pytest.mark.parametrize(
        "text",
        [
            "!chat-history",
            "!chat-history list",
            "!channels",
            "!channels list",
            "!CHAT-HISTORY LIST",
        ],
    )
    def test_unavailable_reply_is_explicit_and_cites_gh_678(
        self, text: str, fake_host: Any
    ) -> None:
        result = _run(transform(_event(text)))
        assert result is not None
        reply = result.payload["text"]
        assert reply and "issues/678" in reply and "aren't available" in reply
        assert "chat.read_unavailable" in [m for m, _ in fake_host.log_calls]

    # regression: gh-678 -- the bundle must not touch kv/db at all (it has no such capability);
    # the fake host exposes only flags/log/relay, so any kv/db access would raise AttributeError.
    def test_no_storage_capability_is_touched(self, fake_host: Any) -> None:
        for text in ("!chat-history", "!channels list"):
            assert _run(transform(_event(text))) is not None

    # regression: gh-674 -- bundles must never log raw user input or raw identity.
    def test_logs_never_contain_actor_or_typed_text(self, fake_host: Any) -> None:
        for text in (
            "!chat-history",
            "!chat-history PIIEXTRA_secret",
            "!channels list PIIEXTRA_secret",
            "!channels bogus PIIEXTRA_secret",
        ):
            event = _event(text, author_id="PIIAUTHOR_id")
            event.actor = "PIIACTOR_alice"
            out = _run(transform(event))
            assert out is not None
            envelope = StageEnvelope(
                tenant=TENANT_ID,
                community=COMMUNITY_ID,
                app_id="waddles.core.example.chat",
                stage="action",
                event=out,
                ts="2026-01-01T00:00:00+00:00",
            )
            _run(dispatch(envelope, {}, http_client=None))

        assert len(fake_host.log_calls) >= 8, "too few log calls -- PII check would be vacuous"
        for message, fields_json in fake_host.log_calls:
            blob = f"{message} {fields_json}".lower()
            for sentinel in ("piiactor_alice", "piiextra_secret", "piiauthor_id"):
                assert sentinel not in blob

    def test_flag_is_queried_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[tuple[str, bool]] = []

        def _enabled(key: str, default_value: bool) -> bool:
            asked.append((key, default_value))
            return default_value

        fake_wit_world = types.ModuleType("wit_world")
        fake_wit_world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
            flags=types.SimpleNamespace(enabled=_enabled)
        )
        monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

        assert _run(transform(_event("!channels"))) is None
        assert asked == [("waddles.command-chat", False)]

    def test_dispatch_empty_text_raises(self, fake_host: Any) -> None:
        """`transform` never emits empty text; an empty one reaching `dispatch` fails loud."""
        envelope = StageEnvelope(
            tenant=TENANT_ID,
            community=COMMUNITY_ID,
            app_id="waddles.core.example.chat",
            stage="action",
            event=PlatformEvent(
                platform="discord",
                event_type="chat.message",
                actor=None,
                payload={"channel_id": "chan-1", "text": ""},
                occurred_at="2026-01-01T00:00:00+00:00",
            ),
            ts="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            _run(dispatch(envelope, {}, http_client=None))
        assert fake_host.relay_calls == []
