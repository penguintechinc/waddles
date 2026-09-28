"""Host-native tests for the `quotes` bundle's `transform`/`dispatch` logic.

No WASM/wasmtime here -- `transform()` is plain async Python, testable
directly; `dispatch()` is tested against a fake `wit_world.imports.kv`/`log`/
`relay`/`flags` (this bundle's own `tests/wit_fakes.py`), the same
oracle-proof scope `sdk/waddle-sdk/tests/test_oracle_alias_bundle.py`
documents ("does this bundle build the right calls" vs. "does the WIT import
actually work", the latter being `waddle_sdk`'s own, already-covered
concern). Mirrors `bundles/python/pyping/tests/test_app.py`'s structure.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import wit_fakes
from waddle_sdk.flask_core.bundle_runtime import bundle_context
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

import app


def _run(coro):
    return asyncio.run(coro)


def _sample_event(
    text: str, *, platform: str = "twitch", channel_id: str | None = "chan-1", **payload_extra
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor="viewer-1",
        payload={"text": text, "channel_id": channel_id, **payload_extra},
        occurred_at="2026-09-28T00:00:00.000Z",
    )


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch):
    """Install a fresh fake WIT host (kv/log/relay/flags), enabling the feature flag by default."""
    return wit_fakes.install(monkeypatch, flags_enabled={app._FEATURE_FLAG: True})


def _transform(event: PlatformEvent, *, community: str | None = "42") -> PlatformEvent | None:
    with bundle_context(tenant="acme", community=community, app_id="waddles.core.quotes.default"):
        return _run(app.transform(event))


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------


def test_non_quote_text_is_ignored(fakes) -> None:
    assert _transform(_sample_event("hello there")) is None
    assert _transform(_sample_event("!pyping")) is None


def test_flag_disabled_produces_no_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    wit_fakes.install(monkeypatch, flags_enabled={app._FEATURE_FLAG: False})
    assert _transform(_sample_event("!quote random")) is None


def test_bare_quote_parses_as_random(fakes) -> None:
    result = _transform(_sample_event("!quote"))
    assert result is not None
    assert result.payload["quote_action"] == "random"


def test_quote_random_subcommand_parses_as_random(fakes) -> None:
    result = _transform(_sample_event("!quote random"))
    assert result.payload["quote_action"] == "random"


def test_quote_add_parses_text_and_id_none(fakes) -> None:
    result = _transform(_sample_event("!quote add be excellent to each other"))
    assert result.payload["quote_action"] == "add"
    assert result.payload["quote_text"] == "be excellent to each other"
    assert result.payload["quote_id"] is None


def test_quote_add_with_no_text_is_usage(fakes) -> None:
    result = _transform(_sample_event("!quote add"))
    assert result.payload["quote_action"] == "usage"


def test_quote_get_parses_id(fakes) -> None:
    result = _transform(_sample_event("!quote get 7"))
    assert result.payload["quote_action"] == "get"
    assert result.payload["quote_id"] == 7


@pytest.mark.parametrize("bad_id", ["", "abc", "7.5"])
def test_quote_get_non_numeric_id_is_usage(fakes, bad_id: str) -> None:
    result = _transform(_sample_event(f"!quote get {bad_id}".rstrip()))
    assert result.payload["quote_action"] == "usage"


@pytest.mark.parametrize("verb", ["delete", "del", "remove"])
def test_quote_delete_aliases_parse_id(fakes, verb: str) -> None:
    result = _transform(_sample_event(f"!quote {verb} 3"))
    assert result.payload["quote_action"] == "delete"
    assert result.payload["quote_id"] == 3


def test_unknown_subcommand_is_usage(fakes) -> None:
    result = _transform(_sample_event("!quote frobnicate"))
    assert result.payload["quote_action"] == "usage"


def test_no_community_forces_community_required_action(fakes) -> None:
    result = _transform(_sample_event("!quote random"), community=None)
    assert result.payload["quote_action"] == "community_required"


def test_moderator_flags_are_copied_through_to_dispatch_payload(fakes) -> None:
    result = _transform(_sample_event("!quote delete 1", is_mod=True))
    assert result.payload["is_mod"] is True


# ---------------------------------------------------------------------------
# dispatch()
# ---------------------------------------------------------------------------


def _envelope(payload: dict, *, community: str | None = "42", platform: str = "twitch"):
    return StageEnvelope(
        tenant="acme",
        community=community,
        app_id="waddles.core.quotes.default",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor="viewer-1",
            payload=payload,
            occurred_at="2026-09-28T00:00:00.000Z",
        ),
        ts="2026-09-28T00:00:00.000Z",
    )


def _dispatch(payload: dict, *, community: str | None = "42", platform: str = "twitch"):
    envelope = _envelope(payload, community=community, platform=platform)
    return _run(app.dispatch(envelope, {}, http_client=None))


def test_dispatch_add_stores_and_relays_confirmation(fakes) -> None:
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    result = _dispatch(
        {"quote_action": "add", "quote_text": "hello world", "channel_id": "chan-1"}
    )
    assert result.transport == "twitch"
    provider, message_json = fake_relay.calls[0]
    assert provider == "twitch"
    assert json.loads(message_json)["text"] == "quote #1 added"


def test_dispatch_get_returns_stored_quote(fakes) -> None:
    _dispatch({"quote_action": "add", "quote_text": "gg wp", "channel_id": "chan-1"})
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    _dispatch({"quote_action": "get", "quote_id": 1, "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert provider == "twitch"
    assert json.loads(message_json)["text"] == 'quote #1: "gg wp"'


def test_dispatch_get_missing_id_reports_not_found(fakes) -> None:
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    _dispatch({"quote_action": "get", "quote_id": 999, "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == "no quote #999"


def test_dispatch_random_with_no_quotes_reports_none(fakes) -> None:
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    _dispatch({"quote_action": "random", "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert "no quotes yet" in json.loads(message_json)["text"]


def test_dispatch_random_picks_from_the_index(fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    _dispatch({"quote_action": "add", "quote_text": "first", "channel_id": "chan-1"})
    _dispatch({"quote_action": "add", "quote_text": "second", "channel_id": "chan-1"})
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    monkeypatch.setattr(app.random, "choice", lambda seq: seq[-1])
    _dispatch({"quote_action": "random", "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == 'quote #2: "second"'


def test_dispatch_delete_denied_without_moderator_flag(fakes) -> None:
    _dispatch({"quote_action": "add", "quote_text": "keep me", "channel_id": "chan-1"})
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    _dispatch({"quote_action": "delete", "quote_id": 1, "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == "only moderators/admins can delete quotes"


@pytest.mark.parametrize("flag", ["is_mod", "is_moderator", "is_broadcaster", "is_admin"])
def test_dispatch_delete_allowed_with_any_moderator_flag(fakes, flag: str) -> None:
    _dispatch({"quote_action": "add", "quote_text": "bye", "channel_id": "chan-1"})
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    _dispatch({"quote_action": "delete", "quote_id": 1, "channel_id": "chan-1", flag: True})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == "quote #1 deleted"

    # A second delete of the same id (now gone) reports not-found, never crashes.
    fake_relay.calls.clear()
    _dispatch({"quote_action": "delete", "quote_id": 1, "channel_id": "chan-1", flag: True})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == "no quote #1"


def test_dispatch_deleted_quote_is_no_longer_returned_by_random(fakes) -> None:
    _dispatch({"quote_action": "add", "quote_text": "only one", "channel_id": "chan-1"})
    _dispatch({"quote_action": "delete", "quote_id": 1, "channel_id": "chan-1", "is_mod": True})
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    _dispatch({"quote_action": "random", "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert "no quotes yet" in json.loads(message_json)["text"]


def test_dispatch_community_required_action_relays_without_touching_kv(fakes) -> None:
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    _dispatch({"quote_action": "community_required", "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert "community context" in json.loads(message_json)["text"]
    assert _fake_kv.store == {}
    assert _fake_kv.counters == {}


def test_dispatch_usage_action_relays_the_hint(fakes) -> None:
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    _dispatch({"quote_action": "usage", "reply_hint": app._USAGE, "channel_id": "chan-1"})
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == app._USAGE


def test_dispatch_raises_when_channel_id_is_missing(fakes) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _dispatch({"quote_action": "random", "channel_id": None})


def test_quotes_are_isolated_per_community(fakes) -> None:
    _dispatch(
        {"quote_action": "add", "quote_text": "for community 42", "channel_id": "chan-1"},
        community="42",
    )
    _fake_kv, _fake_log, fake_relay, _fake_flags = fakes
    fake_relay.calls.clear()

    _dispatch({"quote_action": "get", "quote_id": 1, "channel_id": "chan-1"}, community="99")
    provider, message_json = fake_relay.calls[0]
    assert json.loads(message_json)["text"] == "no quote #1"
