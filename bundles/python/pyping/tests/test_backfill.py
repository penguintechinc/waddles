"""Backfill coverage for the `pyping` bundle: exact-match contract, relay payload, no-leak, wiring.

Complements `test_app.py` with the invariants it does not pin: the reply preserves the event's
actor/timestamp but the *relay* payload carries only `channel`+`text` (never the actor), the
default-text fallback, non-string payloads, the bundle's zero-log / zero-flag surface (it is a
fixture bundle with no feature flag by design), and the static `_entry_wiring` re-export.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import _entry_wiring
import app
import pytest
from app import PING_COMMAND, PONG_REPLY, DispatchResult, dispatch, transform
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: A distinctive token that must never appear in a relayed payload.
CANARY = "CANARYuser9f3a"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(
    text: Any, *, platform: str = "twitch", channel_id: str | None = "chan-1"
) -> PlatformEvent:
    return PlatformEvent(
        platform=platform,
        event_type="chat.message",
        actor=CANARY + "_actor",
        payload={"text": text, "channel_id": channel_id},
        occurred_at="2026-10-09T00:00:00.000Z",
    )


def _envelope(
    platform: str, *, text: Any = PONG_REPLY, channel_id: str | None = "chan-1"
) -> StageEnvelope:
    payload: dict[str, Any] = {"channel_id": channel_id}
    if text is not None:
        payload["text"] = text
    return StageEnvelope(
        tenant="tenant-1",
        community=None,
        app_id="waddles.core.example.pyping",
        stage="action",
        event=PlatformEvent(
            platform=platform,
            event_type="chat.message",
            actor=CANARY + "_actor",
            payload=payload,
            occurred_at="2026-10-09T00:00:00.000Z",
        ),
        ts="2026-10-09T00:00:00.000Z",
    )


@pytest.fixture
def relay_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Install a fake `wit_world` exposing ONLY `relay` -- no `flags`, no `log` (see module doc)."""
    calls: list[tuple[str, str]] = []
    world = types.ModuleType("wit_world")
    world.imports = types.SimpleNamespace(  # type: ignore[attr-defined]
        relay=types.SimpleNamespace(push=lambda p, m: calls.append((p, m)))
    )
    monkeypatch.setitem(sys.modules, "wit_world", world)
    return calls


def test_constants_pin_the_wire_contract() -> None:
    assert PING_COMMAND == "!pyping"
    assert PONG_REPLY == "pong (py)"


def test_transform_works_without_flags_or_log_imports(relay_calls: list[tuple[str, str]]) -> None:
    """`pyping` has no feature flag and never logs: a host exposing only `relay` is enough."""
    result = _run(transform(_event(PING_COMMAND)))
    assert result is not None
    assert result.payload == {"text": PONG_REPLY, "channel_id": "chan-1"}


def test_transform_matches_even_with_no_wit_world_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The match is pure -- no host import is consulted on the transform path."""
    monkeypatch.setitem(sys.modules, "wit_world", None)
    assert _run(transform(_event(PING_COMMAND))) is not None


@pytest.mark.parametrize("platform", ["twitch", "discord", "slack"])
def test_reply_preserves_platform_event_type_actor_and_timestamp(platform: str) -> None:
    event = _event(PING_COMMAND, platform=platform)
    result = _run(transform(event))
    assert result is not None
    assert (result.platform, result.event_type) == (platform, "chat.message")
    assert result.actor == event.actor
    assert result.occurred_at == event.occurred_at


def test_reply_carries_a_none_channel_through_unchanged() -> None:
    """A channel-less inbound event still yields a reply; `dispatch` is what fails loud."""
    result = _run(transform(_event(PING_COMMAND, channel_id=None)))
    assert result is not None
    assert result.payload["channel_id"] is None


@pytest.mark.parametrize("text", [None, 0, 1.5, ["!pyping"], {"text": "!pyping"}, b"!pyping"])
def test_non_string_text_is_ignored_not_coerced(text: Any) -> None:
    assert _run(transform(_event(text))) is None


@pytest.mark.parametrize(
    "text",
    ["!PYPING", "!Pyping", "! pyping", "!pyping\nextra", "!pyping!", f"!pyping {CANARY}"],
)
def test_match_is_exact_and_case_sensitive(text: str) -> None:
    """Documented contract: exact `!pyping` only (trimmed) -- no case folding, no arguments."""
    assert _run(transform(_event(text))) is None


def test_dispatch_relay_payload_has_only_channel_and_text(
    relay_calls: list[tuple[str, str]],
) -> None:
    """No actor/timestamp/tenant ever crosses the relay boundary (PII-free regression)."""
    _run(dispatch(_envelope("twitch"), {}, http_client=None))

    provider, message_json = relay_calls[0]
    assert provider == "twitch"
    assert json.loads(message_json) == {"channel": "chan-1", "text": PONG_REPLY}
    assert CANARY not in message_json


def test_dispatch_result_shape_matches_the_component_entry_contract(
    relay_calls: list[tuple[str, str]],
) -> None:
    result = _run(dispatch(_envelope("discord"), {"unused": True}, http_client=object()))

    assert isinstance(result, DispatchResult)
    assert (result.transport, result.detail) == ("discord", "relayed")
    assert result.sub_type is None
    assert result.http_status is None


def test_dispatch_without_text_falls_back_to_the_pong_reply(
    relay_calls: list[tuple[str, str]],
) -> None:
    _run(dispatch(_envelope("twitch", text=None), {}, http_client=None))
    assert json.loads(relay_calls[0][1])["text"] == PONG_REPLY


@pytest.mark.parametrize("channel_id", [None, ""])
def test_dispatch_fails_loud_without_a_channel_and_never_relays(
    channel_id: str | None, relay_calls: list[tuple[str, str]]
) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        _run(dispatch(_envelope("twitch", channel_id=channel_id), {}, http_client=None))
    assert relay_calls == []


def test_dispatch_surfaces_a_relay_host_failure_instead_of_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-silent-fallback: a failing `relay.push` propagates (it is `_component_entry`'s signal)."""

    def _boom(_provider: str, _message: str) -> None:
        raise RuntimeError("relay backend down")

    world = types.ModuleType("wit_world")
    relay_ns = types.SimpleNamespace(push=_boom)
    world.imports = types.SimpleNamespace(relay=relay_ns)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", world)

    with pytest.raises(RuntimeError, match="relay backend down"):
        _run(dispatch(_envelope("twitch"), {}, http_client=None))


def test_dispatch_result_rejects_unknown_attributes() -> None:
    result = DispatchResult(transport="twitch", detail="relayed")
    with pytest.raises(AttributeError):
        result.bogus = 1  # type: ignore[attr-defined]


def test_entry_wiring_exports_the_stage_functions() -> None:
    assert _entry_wiring.bundle_transform is app.transform
    assert _entry_wiring.bundle_dispatch is app.dispatch
    assert _entry_wiring.__all__ == ["bundle_dispatch", "bundle_transform"]
