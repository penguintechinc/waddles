"""Frozen typed stage-to-stage pipeline contract (`PlatformEvent`, `StageEnvelope`).

Root cause under test: stages used to pass untyped dicts over Valkey, and one
stage wrapped an already-normalized dict under a second `payload` key --
real fields ended up one level too deep, and a lookup returned `None`
silently instead of raising. These dataclasses make that class of bug
impossible: the queue-crossing field is `event`, never `payload`, so a
legacy-shaped message fails loudly in `from_dict` instead of feeding a
wrong-but-plausible object downstream. Kept in its own file (not the shared
`conftest.py`) since these are pure dataclass tests with no fixture
dependencies -- same rationale as `test_bundle_isolation_keys.py`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from flask_core.stream_pipeline import (
    BUNDLE_STAGES,
    EnvelopeError,
    PlatformEvent,
    StageEnvelope,
)

TENANT = "tenant-1"
COMMUNITY = "community-1"
APP_ID = "penguintechinc.waddles.bot-example"


def _make_event(**overrides: object) -> PlatformEvent:
    fields: dict[str, object] = {
        "platform": "example-platform",
        "event_type": "message",
        "actor": "actor-1",
        "payload": {"text": "hello", "channel_id": "chan-1"},
        "occurred_at": "2026-09-04T00:00:00Z",
    }
    fields.update(overrides)
    return PlatformEvent(**fields)


def _make_envelope(**overrides: object) -> StageEnvelope:
    fields: dict[str, object] = {
        "tenant": TENANT,
        "community": COMMUNITY,
        "app_id": APP_ID,
        "stage": "process",
        "event": _make_event(),
        "ts": "2026-09-04T00:00:01Z",
    }
    fields.update(overrides)
    return StageEnvelope(**fields)


# --- PlatformEvent ---


def test_platform_event_round_trip() -> None:
    event = _make_event()
    assert PlatformEvent.from_dict(event.to_dict()) == event


def test_platform_event_round_trip_actor_none() -> None:
    event = _make_event(actor=None)
    assert PlatformEvent.from_dict(event.to_dict()) == event
    assert event.to_dict()["actor"] is None


def test_platform_event_is_slotted_and_frozen() -> None:
    event = _make_event()
    assert not hasattr(event, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.platform = "other"


@pytest.mark.parametrize("key", ["platform", "event_type", "occurred_at"])
def test_platform_event_from_dict_missing_required_field_raises(key: str) -> None:
    data = _make_event().to_dict()
    del data[key]
    with pytest.raises(EnvelopeError, match=key):
        PlatformEvent.from_dict(data)


@pytest.mark.parametrize("key", ["platform", "event_type", "occurred_at"])
def test_platform_event_from_dict_wrong_type_required_field_raises(key: str) -> None:
    data = _make_event().to_dict()
    data[key] = 12345  # not a string
    with pytest.raises(EnvelopeError, match=key):
        PlatformEvent.from_dict(data)


def test_platform_event_from_dict_wrong_type_actor_raises() -> None:
    data = _make_event().to_dict()
    data["actor"] = 42
    with pytest.raises(EnvelopeError, match="actor"):
        PlatformEvent.from_dict(data)


def test_platform_event_from_dict_wrong_type_payload_raises() -> None:
    data = _make_event().to_dict()
    data["payload"] = "not-an-object"
    with pytest.raises(EnvelopeError, match="payload"):
        PlatformEvent.from_dict(data)


def test_platform_event_from_dict_missing_payload_raises() -> None:
    data = _make_event().to_dict()
    del data["payload"]
    with pytest.raises(EnvelopeError, match="payload"):
        PlatformEvent.from_dict(data)


# --- StageEnvelope ---


def test_stage_envelope_round_trip() -> None:
    envelope = _make_envelope()
    assert StageEnvelope.from_dict(envelope.to_dict()) == envelope


def test_stage_envelope_round_trip_community_none() -> None:
    envelope = _make_envelope(community=None)
    restored = StageEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    assert restored.community is None


def test_stage_envelope_nested_event_round_trips() -> None:
    """The nested `PlatformEvent`, not just the envelope shell, round-trips too."""
    payload = {"text": "hi", "channel_id": "chan-42", "guild_id": "g-9"}
    envelope = _make_envelope(event=_make_event(payload=payload))
    restored = StageEnvelope.from_dict(envelope.to_dict())
    assert restored.event == envelope.event
    assert restored.event.payload["channel_id"] == "chan-42"


def test_stage_envelope_json_round_trip_through_valkey() -> None:
    """`json.dumps(env.to_dict())` on write, `from_dict(json.loads(raw))` on read."""
    envelope = _make_envelope()
    raw = json.dumps(envelope.to_dict())
    restored = StageEnvelope.from_dict(json.loads(raw))
    assert restored == envelope


# --- target_app_id (gh #298, feature-bundle-routing) ---


def test_stage_envelope_target_app_id_defaults_to_none() -> None:
    """Omitting `target_app_id` at construction defaults to `None` -- backward compatible."""
    envelope = _make_envelope()
    assert envelope.target_app_id is None
    assert envelope.to_dict()["target_app_id"] is None


def test_stage_envelope_target_app_id_round_trips() -> None:
    envelope = _make_envelope(target_app_id="penguintechinc.waddles.forums-pipeline")
    restored = StageEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    assert restored.target_app_id == "penguintechinc.waddles.forums-pipeline"


def test_stage_envelope_from_dict_missing_target_app_id_key_defaults_to_none() -> None:
    """A legacy message with no `target_app_id` key at all still deserializes cleanly."""
    data = _make_envelope().to_dict()
    del data["target_app_id"]
    restored = StageEnvelope.from_dict(data)
    assert restored.target_app_id is None


def test_stage_envelope_from_dict_wrong_type_target_app_id_raises() -> None:
    data = _make_envelope().to_dict()
    data["target_app_id"] = 12345
    with pytest.raises(EnvelopeError, match="target_app_id"):
        StageEnvelope.from_dict(data)


def test_stage_envelope_is_slotted_and_frozen() -> None:
    envelope = _make_envelope()
    assert not hasattr(envelope, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        envelope.tenant = "other-tenant"


def test_stage_envelope_tenant_stays_str() -> None:
    envelope = _make_envelope()
    restored = StageEnvelope.from_dict(envelope.to_dict())
    assert isinstance(restored.tenant, str)
    assert restored.tenant == TENANT


@pytest.mark.parametrize("key", ["tenant", "app_id", "stage", "ts"])
def test_stage_envelope_from_dict_missing_required_field_raises(key: str) -> None:
    data = _make_envelope().to_dict()
    del data[key]
    with pytest.raises(EnvelopeError, match=key):
        StageEnvelope.from_dict(data)


@pytest.mark.parametrize("key", ["tenant", "app_id", "stage", "ts"])
def test_stage_envelope_from_dict_wrong_type_required_field_raises(key: str) -> None:
    data = _make_envelope().to_dict()
    data[key] = 12345  # not a string
    with pytest.raises(EnvelopeError, match=key):
        StageEnvelope.from_dict(data)


def test_stage_envelope_from_dict_wrong_type_community_raises() -> None:
    data = _make_envelope().to_dict()
    data["community"] = 999
    with pytest.raises(EnvelopeError, match="community"):
        StageEnvelope.from_dict(data)


@pytest.mark.parametrize("bad_stage", ["response", "inbound", "", "PROCESS"])
def test_stage_envelope_from_dict_invalid_stage_raises(bad_stage: str) -> None:
    data = _make_envelope().to_dict()
    data["stage"] = bad_stage
    with pytest.raises(EnvelopeError, match="stage"):
        StageEnvelope.from_dict(data)


@pytest.mark.parametrize("stage", BUNDLE_STAGES)
def test_stage_envelope_from_dict_accepts_all_bundle_stages(stage: str) -> None:
    data = _make_envelope(stage=stage).to_dict()
    assert StageEnvelope.from_dict(data).stage == stage


def test_stage_envelope_from_dict_missing_event_key_raises() -> None:
    """No `event` key at all -- the base malformed-shape case `from_dict` rejects."""
    data = _make_envelope().to_dict()
    del data["event"]
    with pytest.raises(EnvelopeError, match="event"):
        StageEnvelope.from_dict(data)


def test_stage_envelope_from_dict_rejects_legacy_payload_nested_shape() -> None:
    """Regression guard for the double-nesting bug this contract retires.

    The legacy shape carried the normalized event data directly under a
    top-level `payload` key (no `event` key at all) -- exactly the wrapper
    that produced `envelope["payload"]["payload"]["channel_id"]`. That shape
    must raise `EnvelopeError`, never be silently coerced into a `StageEnvelope`.
    """
    legacy_shaped = {
        "tenant": TENANT,
        "community": COMMUNITY,
        "app_id": APP_ID,
        "stage": "action",
        "payload": {
            "platform": "example-platform",
            "event_type": "message",
            "actor": "actor-1",
            "payload": {"text": "hello", "channel_id": "chan-1"},
            "occurred_at": "2026-09-04T00:00:00Z",
        },
        "ts": "2026-09-04T00:00:01Z",
    }
    with pytest.raises(EnvelopeError, match="event"):
        StageEnvelope.from_dict(legacy_shaped)


def test_stage_envelope_from_dict_wrong_type_event_raises() -> None:
    data = _make_envelope().to_dict()
    data["event"] = "not-an-object"
    with pytest.raises(EnvelopeError, match="event"):
        StageEnvelope.from_dict(data)


def test_envelope_error_is_a_value_error() -> None:
    """`EnvelopeError` is a `ValueError` subtype -- callers can catch either."""
    assert issubclass(EnvelopeError, ValueError)


def test_stage_replace_produces_new_instance_not_mutation() -> None:
    """Stages build a NEW envelope/event via `dataclasses.replace`, never mutate one."""
    envelope = _make_envelope()
    transformed = dataclasses.replace(
        envelope,
        event=dataclasses.replace(
            envelope.event, payload={**envelope.event.payload, "text": "HELLO"}
        ),
    )
    assert transformed is not envelope
    assert transformed.event is not envelope.event
    assert envelope.event.payload["text"] == "hello"  # original untouched
    assert transformed.event.payload["text"] == "HELLO"
    assert transformed.event.payload["channel_id"] == "chan-1"  # other keys preserved
