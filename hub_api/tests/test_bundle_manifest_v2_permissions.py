"""Tests for the structured `permissions:` block (spec Sec2.1/2.2/2.3)."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_VALID_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station",
    "version": "3.0.0",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        },
    },
}


def _parse(permissions: list[dict]) -> object:
    manifest = {**_VALID_MANIFEST, "permissions": permissions}
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )


def test_no_permissions_block_parses_empty() -> None:
    manifest = parse_bundle_manifest_v2(
        _VALID_MANIFEST,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert manifest.permission_declarations == ()  # type: ignore[attr-defined]


def test_legacy_string_list_permissions_parse_but_not_structured() -> None:
    manifest = _parse(["storage.kv"])  # type: ignore[list-item]
    assert manifest.permissions == ("storage.kv",)  # type: ignore[attr-defined]
    assert manifest.permission_declarations == ()  # type: ignore[attr-defined]


def test_valid_normal_permission_parses() -> None:
    manifest = _parse([{"id": "storage.kv", "justification": "Stores per-viewer cooldown state."}])
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert len(decls) == 1
    assert decls[0].id == "storage.kv"
    assert decls[0].risk == "normal"


def test_unknown_permission_id_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse([{"id": "storage.nonexistent", "justification": "x"}])
    assert exc.value.reason == "unknown_permission"


def test_dangerous_permission_requires_justification() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse([{"id": "ai.generate", "justification": ""}])
    assert exc.value.reason == "missing_justification"


def test_justification_too_long_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse([{"id": "storage.kv", "justification": "x" * 281}])
    assert exc.value.reason == "missing_justification"


def test_storage_tables_requires_schema_param() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse([{"id": "storage.tables", "justification": "Stores catches.", "params": {}}])
    assert exc.value.reason == "storage_tables_requires_schema"


def test_storage_tables_with_schema_parses() -> None:
    manifest = _parse(
        [
            {
                "id": "storage.tables",
                "justification": "Stores catches.",
                "params": {"schema": "data/schema.yaml"},
            }
        ]
    )
    assert manifest.permission_declarations[0].params["schema"] == "data/schema.yaml"  # type: ignore[attr-defined]


def test_overlay_media_requires_allowed_hosts_and_duration() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "overlay.media",
                    "justification": "Plays a clip.",
                    "params": {"allowed_hosts": [], "max_duration_seconds": 30},
                }
            ]
        )
    assert exc.value.reason == "invalid_overlay_hosts"


def test_overlay_media_duration_over_ceiling_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "overlay.media",
                    "justification": "Plays a clip.",
                    "params": {"allowed_hosts": ["youtube.com"], "max_duration_seconds": 31},
                }
            ]
        )
    assert exc.value.reason == "invalid_overlay_duration"


def test_reputation_write_delta_bounds_enforced() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "reputation.community.write",
                    "justification": "Rewards catches.",
                    "params": {"delta_min": -6, "delta_max": 1, "reason_codes": ["fishing.catch"]},
                }
            ]
        )
    assert exc.value.reason == "delta_out_of_bounds"


def test_reputation_write_requires_reason_codes() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "reputation.community.write",
                    "justification": "Rewards catches.",
                    "params": {"delta_min": -1, "delta_max": 1, "reason_codes": []},
                }
            ]
        )
    assert exc.value.reason == "missing_reason_codes"


def test_reputation_write_invalid_reason_code_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "reputation.community.write",
                    "justification": "Rewards catches.",
                    "params": {
                        "delta_min": -1,
                        "delta_max": 1,
                        "reason_codes": ["Fishing.Catch!"],
                    },
                }
            ]
        )
    assert exc.value.reason == "invalid_reason_code"


def test_reputation_write_valid_parses() -> None:
    manifest = _parse(
        [
            {
                "id": "reputation.community.write",
                "justification": "Rewards catches.",
                "params": {"delta_min": -1, "delta_max": 1, "reason_codes": ["fishing.legendary"]},
            }
        ]
    )
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert decls[0].risk == "dangerous"
