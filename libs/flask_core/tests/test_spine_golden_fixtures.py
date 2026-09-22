"""Golden fixtures for the D30 envelope contract (spec Sec5.11/6.1.2).

`tests/fixtures/spine/{valid,invalid}/*.json` are the SAME fixtures the
Rust `penguin-spine` crate asserts its `StageEnvelope`/`PlatformEvent`
readers against (M1, `flask_core alignment`) -- field names and shapes
match `penguin-spine::envelope` exactly, so a fully populated D30 envelope
validates identically on both sides.

One fixture (`valid/stage_envelope_legacy_pre_d30.json`) is a documented
exception: it carries none of the six D30 fields at all, which is valid
here (additive-optional, see `stream_pipeline.py`'s module note) but would
be REJECTED by the Rust reader, which enforces `schema_version`,
`workstream_id`, `event_id` and `binding` with no dual-read. That
asymmetry is intentional -- this module is still imported by the
pre-cut-over Python-only stage runners, which have not yet been migrated
to mint these fields.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from flask_core.stream_pipeline import (
    Binding,
    EnvelopeError,
    PlatformEvent,
    StageEnvelope,
    Trace,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "spine"
VALID_DIR = FIXTURES_DIR / "valid"
INVALID_DIR = FIXTURES_DIR / "invalid"

_STAGE_ENVELOPE_INVALID = sorted(
    p for p in INVALID_DIR.glob("*.json") if not p.name.startswith("platform_event_")
)
_PLATFORM_EVENT_INVALID = sorted(INVALID_DIR.glob("platform_event_*.json"))

assert _STAGE_ENVELOPE_INVALID, "no StageEnvelope invalid fixtures found -- zero is a failure"
assert _PLATFORM_EVENT_INVALID, "no PlatformEvent invalid fixtures found -- zero is a failure"


def _load(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def test_fixture_directories_are_non_empty() -> None:
    """A zero-fixture count is a failure of this deliverable, not a pass."""
    valid = list(VALID_DIR.glob("*.json"))
    invalid = list(INVALID_DIR.glob("*.json"))
    assert len(valid) >= 4, f"expected >=4 valid fixtures, found {len(valid)}"
    assert len(invalid) >= 10, f"expected >=10 invalid fixtures, found {len(invalid)}"


# --- Valid fixtures: full D30 shape ---


def test_full_d30_stage_envelope_deserializes() -> None:
    envelope = StageEnvelope.from_dict(_load(VALID_DIR / "stage_envelope_full_d30.json"))
    assert envelope.schema_version == 2
    assert envelope.tenant == "global"
    assert envelope.community is None
    assert envelope.app_id == "waddles.bot.commands.default"
    assert envelope.stage == "process"
    assert envelope.target_app_id is None
    assert envelope.workstream_id == "8f14e45f-ceea-467e-adde-3fb5c9752730"
    assert envelope.event_id == "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    assert envelope.session_id is None
    assert envelope.trace == Trace(
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        tracestate=None,
    )
    assert envelope.binding == Binding(
        kid="2026-09",
        mac="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    )


def test_full_d30_stage_envelope_round_trips() -> None:
    data = _load(VALID_DIR / "stage_envelope_full_d30.json")
    envelope = StageEnvelope.from_dict(data)
    assert StageEnvelope.from_dict(envelope.to_dict()) == envelope


def test_community_scoped_stage_envelope_deserializes() -> None:
    envelope = StageEnvelope.from_dict(_load(VALID_DIR / "stage_envelope_community_scoped.json"))
    assert envelope.community == "main"
    assert envelope.target_app_id == "waddles.community.forums.default"
    assert envelope.session_id == "gw-session-abc123"
    assert envelope.schema_version == 2


def test_legacy_pre_d30_stage_envelope_deserializes_with_none_d30_fields() -> None:
    """Additive-optional backward compat: a pre-D30 envelope stays valid here.

    The Rust `penguin-spine` reader would reject this exact shape (missing
    `schema_version`/`workstream_id`/`event_id`/`binding`) -- documented,
    intentional asymmetry during the transition (see module docstring).
    """
    envelope = StageEnvelope.from_dict(_load(VALID_DIR / "stage_envelope_legacy_pre_d30.json"))
    assert envelope.schema_version is None
    assert envelope.workstream_id is None
    assert envelope.event_id is None
    assert envelope.session_id is None
    assert envelope.trace is None
    assert envelope.binding is None


def test_valid_platform_event_fixture_deserializes() -> None:
    event = PlatformEvent.from_dict(_load(VALID_DIR / "platform_event_full.json"))
    assert event.platform == "twitch"
    assert event.event_type == "chat.message"
    assert event.actor == "some_user"
    assert event.occurred_at == "2026-09-14T12:00:00.000Z"


# --- Invalid fixtures: rejected by StageEnvelope.from_dict ---


@pytest.mark.parametrize("path", _STAGE_ENVELOPE_INVALID, ids=lambda p: p.stem)
def test_invalid_stage_envelope_fixture_is_rejected(path: Path) -> None:
    with pytest.raises(EnvelopeError):
        StageEnvelope.from_dict(_load(path))


# --- Invalid fixtures: rejected by PlatformEvent.from_dict ---


@pytest.mark.parametrize("path", _PLATFORM_EVENT_INVALID, ids=lambda p: p.stem)
def test_invalid_platform_event_fixture_is_rejected(path: Path) -> None:
    with pytest.raises(EnvelopeError):
        PlatformEvent.from_dict(_load(path))


# --- Rejection-reason assertions (per-fixture, not just "some error") ---


def test_missing_event_key_mentions_event() -> None:
    with pytest.raises(EnvelopeError, match="event"):
        StageEnvelope.from_dict(
            _load(INVALID_DIR / "missing_event_key_legacy_payload_nesting.json")
        )


def test_unknown_top_level_key_mentions_the_extra_key() -> None:
    with pytest.raises(EnvelopeError, match="extra"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "unknown_top_level_key.json"))


def test_stage_outside_fixed_set_mentions_stage() -> None:
    with pytest.raises(EnvelopeError, match="stage"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "stage_outside_fixed_set.json"))


def test_wrong_type_tenant_mentions_tenant() -> None:
    with pytest.raises(EnvelopeError, match="tenant"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "wrong_type_tenant.json"))


def test_schema_version_wrong_type_mentions_schema_version() -> None:
    with pytest.raises(EnvelopeError, match="schema_version"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "schema_version_wrong_type.json"))


def test_schema_version_value_one_mentions_schema_version() -> None:
    """No dual-read (D3, D30): a present `schema_version` of `1` is rejected outright."""
    with pytest.raises(EnvelopeError, match="schema_version"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "schema_version_value_one.json"))


def test_binding_missing_mac_mentions_mac() -> None:
    with pytest.raises(EnvelopeError, match="mac"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "binding_present_missing_mac.json"))


def test_trace_missing_traceparent_mentions_traceparent() -> None:
    with pytest.raises(EnvelopeError, match="traceparent"):
        StageEnvelope.from_dict(_load(INVALID_DIR / "trace_present_missing_traceparent.json"))


def test_platform_event_missing_occurred_at_mentions_occurred_at() -> None:
    with pytest.raises(EnvelopeError, match="occurred_at"):
        PlatformEvent.from_dict(_load(INVALID_DIR / "platform_event_missing_occurred_at.json"))


def test_platform_event_wrong_type_payload_mentions_payload() -> None:
    with pytest.raises(EnvelopeError, match="payload"):
        PlatformEvent.from_dict(_load(INVALID_DIR / "platform_event_wrong_type_payload.json"))


def test_platform_event_unknown_key_mentions_extra_field() -> None:
    with pytest.raises(EnvelopeError, match="extra_field"):
        PlatformEvent.from_dict(_load(INVALID_DIR / "platform_event_unknown_key.json"))


# --- Trace / Binding: direct unit coverage (D30, spec Sec5.11/6.1.2) ---


def test_trace_round_trip() -> None:
    trace = Trace(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert Trace.from_dict(trace.to_dict()) == trace
    assert trace.tracestate is None


def test_trace_is_slotted_and_frozen() -> None:
    trace = Trace(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert not hasattr(trace, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        trace.traceparent = "other"


def test_trace_missing_traceparent_raises() -> None:
    with pytest.raises(EnvelopeError, match="traceparent"):
        Trace.from_dict({"tracestate": "x=y"})


def test_trace_unknown_key_raises() -> None:
    with pytest.raises(EnvelopeError, match="trace"):
        Trace.from_dict({"traceparent": "00-a-b-01", "bogus": "nope"})


def test_binding_round_trip() -> None:
    binding = Binding(kid="2026-09", mac="a" * 64)
    assert Binding.from_dict(binding.to_dict()) == binding


def test_binding_is_slotted_and_frozen() -> None:
    binding = Binding(kid="2026-09", mac="a" * 64)
    assert not hasattr(binding, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.kid = "other"


def test_binding_missing_kid_raises() -> None:
    with pytest.raises(EnvelopeError, match="kid"):
        Binding.from_dict({"mac": "a" * 64})


def test_binding_unknown_key_raises() -> None:
    with pytest.raises(EnvelopeError, match="binding"):
        Binding.from_dict({"kid": "2026-09", "mac": "a" * 64, "bogus": "nope"})
