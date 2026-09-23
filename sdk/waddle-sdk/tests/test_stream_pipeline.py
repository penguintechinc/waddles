"""Tests for `waddle_sdk.flask_core.stream_pipeline`."""

from __future__ import annotations

import json
import types

import wit_shapes

from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope


def test_platform_event_round_trips() -> None:
    """to_dict()/from_dict() round-trip every field."""
    e = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload={"text": "hi"},
        occurred_at="2026-09-14T12:00:00.000Z",
    )
    d = e.to_dict()
    e2 = PlatformEvent.from_dict(d)
    assert e2 == e


def test_stage_envelope_round_trips_with_optional_fields_absent() -> None:
    """Optional fields are omitted from to_dict() when None, and restored as None by from_dict()."""
    e = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor=None,
        payload={},
        occurred_at="2026-09-14T12:00:00.000Z",
    )
    env = StageEnvelope(
        tenant="acme",
        community=None,
        app_id="waddles.core.example.echo",
        stage="process",
        event=e,
        ts="2026-09-14T12:00:00.123Z",
    )
    d = env.to_dict()
    assert "target_app_id" not in d
    assert "trace_context" not in d
    env2 = StageEnvelope.from_dict(d)
    assert env2 == env


def test_platform_event_from_wit_record_parses_payload_json() -> None:
    """from_wit_record() parses the WIT record's payload_json text into a plain dict."""
    record = wit_shapes.WitPlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor="penguin",
        payload_json=json.dumps({"text": "!alias sr songrequest"}),
        occurred_at="2026-09-14T00:00:00Z",
    )
    event = PlatformEvent.from_wit_record(record)
    assert event.payload == {"text": "!alias sr songrequest"}
    assert event.platform == "twitch"


def test_platform_event_from_wit_record_handles_empty_payload_json() -> None:
    """An empty payload_json string yields an empty dict, never a JSON decode error."""
    record = wit_shapes.WitPlatformEvent(
        platform="twitch",
        event_type="chat.message",
        actor=None,
        payload_json="",
        occurred_at="2026-09-14T00:00:00Z",
    )
    event = PlatformEvent.from_wit_record(record)
    assert event.payload == {}


def test_platform_event_to_wit_record_round_trips_through_payload_json() -> None:
    """to_wit_record()/from_wit_record() round-trip through the fake types module."""
    fake_types = types.ModuleType("fake_types")
    fake_types.PlatformEvent = wit_shapes.WitPlatformEvent  # type: ignore[attr-defined]
    event = PlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload={"n": 1},
        occurred_at="ts",
    )
    record = event.to_wit_record(fake_types)
    assert record.payload_json == json.dumps({"n": 1})
    assert PlatformEvent.from_wit_record(record) == event


def test_stage_envelope_from_wit_record() -> None:
    """StageEnvelope.from_wit_record() converts a nested WIT record correctly."""
    wit_event = wit_shapes.WitPlatformEvent(
        platform="discord",
        event_type="chat.message",
        actor="u1",
        payload_json="{}",
        occurred_at="ts",
    )
    wit_envelope = wit_shapes.WitStageEnvelope(
        tenant="acme",
        community="main",
        app_id="waddles.social.alias.default",
        stage="process",
        event=wit_event,
        ts="ts",
        target_app_id=None,
        trace_context=None,
    )
    envelope = StageEnvelope.from_wit_record(wit_envelope)
    assert envelope.tenant == "acme"
    assert envelope.event.platform == "discord"
    assert envelope.target_app_id is None
