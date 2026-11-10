"""Tests for the manifest `schedule: {interval_s, jitter}` block (host-delivered reconcile)."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import (
    SCHEDULE_DEFAULT_JITTER,
    ManifestV2Error,
    ScheduleDeclaration,
    parse_bundle_manifest_v2,
    parse_schedule,
)
from tests.test_bundle_manifest_v2 import _VALID_MANIFEST


def _parse(schedule: object) -> object:
    return parse_bundle_manifest_v2(
        {**_VALID_MANIFEST, "schedule": schedule},
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


def test_absent_schedule_is_none() -> None:
    assert parse_schedule(None) is None
    manifest = parse_bundle_manifest_v2(
        dict(_VALID_MANIFEST),
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert manifest.schedule is None


def test_valid_schedule_parses_with_default_and_explicit_jitter() -> None:
    assert parse_schedule({"interval_s": 30}) == ScheduleDeclaration(30, SCHEDULE_DEFAULT_JITTER)
    assert parse_schedule({"interval_s": 600, "jitter": 0.5}) == ScheduleDeclaration(600, 0.5)
    assert parse_schedule({"interval_s": 86400, "jitter": 0}) == ScheduleDeclaration(86400, 0.0)
    assert _parse({"interval_s": 120}).schedule == ScheduleDeclaration(120, 0.2)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "raw",
    [
        "every minute",
        [],
        {},
        {"interval_s": 29},
        {"interval_s": 86401},
        {"interval_s": 30.5},
        {"interval_s": True},
        {"interval_s": "60"},
        {"interval_s": 60, "jitter": 0.51},
        {"interval_s": 60, "jitter": -0.1},
        {"interval_s": 60, "jitter": True},
        {"interval_s": 60, "jitter": float("nan")},
        {"interval_s": 60, "jitter": "0.1"},
        {"interval_s": 60, "typo": 1},
    ],
)
def test_invalid_schedule_is_rejected(raw: object) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(raw)
    assert exc.value.reason == "invalid_schedule"
