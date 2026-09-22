"""Tests for the bundle.yaml v2 pure-YAML validation subset hub-api checks pre-Job."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_VALID_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station Song Request",
    "version": "3.0.0",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "bundles.social_music_process:transform",
            "consumes": [
                {"platform": "twitch", "event_types": ["chat.message"]},
            ],
        },
    },
    "egress": [{"host": "api.spotify.com", "methods": ["GET", "POST"]}],
    "data": {"tables": ["music_queue"]},
    "limits": {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10},
}


def _parse(overrides: dict, **kwargs: object) -> object:
    manifest = {**_VALID_MANIFEST, **overrides}
    return parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=kwargs.get("known_custom_platforms", frozenset()),  # type: ignore[arg-type]
        allow_wildcard_consumes=kwargs.get("allow_wildcard_consumes", False),  # type: ignore[arg-type]
        allow_prebuilt=kwargs.get("allow_prebuilt", True),  # type: ignore[arg-type]
    )


def test_valid_manifest_parses() -> None:
    manifest = _parse({})
    assert manifest.app_id == "waddles.socials.music.default"  # type: ignore[attr-defined]
    assert manifest.consumes[0].platform == "twitch"  # type: ignore[attr-defined]


def test_wrong_schema_version_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"schema_version": 1})
    assert exc.value.reason == "unsupported_schema_version"


def test_ingest_stage_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "stages": {"ingest": {"entry": "x:y"}}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "ingest_not_pluggable"


def test_process_stage_without_consumes_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {"process": {"entry": "bundles.x:transform"}},
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "consumes_required"


def test_action_stage_with_consumes_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {
            "action": {
                "entry": "bundles.x:dispatch",
                "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
            }
        },
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "consumes_on_action_stage"


def test_wildcard_consumes_rejected_without_tenant_setting() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "*", "event_types": ["chat.message"]}],
            }
        },
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "wildcard_consumes_not_allowed"


def test_wildcard_consumes_allowed_with_tenant_setting() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "*", "event_types": ["chat.message"]}],
            }
        },
    }
    parsed = parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=True,
        allow_prebuilt=True,
    )
    assert parsed.consumes[0].platform == "*"


def test_unknown_custom_platform_rejected() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "custom:unregistered", "event_types": ["chat.message"]}],
            }
        },
    }
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset({"other"}),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "unknown_consumes_platform"


def test_registered_custom_platform_accepted() -> None:
    manifest = {
        **_VALID_MANIFEST,
        "stages": {
            "process": {
                "entry": "x:y",
                "consumes": [{"platform": "custom:mycrm", "event_types": ["ticket.created"]}],
            }
        },
    }
    parsed = parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset({"mycrm"}),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert parsed.consumes[0].platform == "custom:mycrm"


def test_prebuilt_rejected_when_global_setting_off() -> None:
    manifest = {**_VALID_MANIFEST, "artifact": "prebuilt", "language": "other"}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=False,
        )
    assert exc.value.reason == "prebuilt_not_allowed"


def test_reserved_data_table_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "data": {"tables": ["users"]}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "reserved_data_table"


def test_limit_out_of_range_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "limits": {"timeout_ms": 999999}}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "limit_out_of_range"


def test_invalid_egress_host_rejected() -> None:
    manifest = {**_VALID_MANIFEST, "egress": [{"host": "http://evil.example.com/path"}]}
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "invalid_egress_host"


def test_routes_to_is_parsed_through() -> None:
    manifest = {**_VALID_MANIFEST, "routes_to": ["waddles.community.forums.default"]}
    parsed = parse_bundle_manifest_v2(
        manifest,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
    )
    assert parsed.routes_to == ("waddles.community.forums.default",)


def test_missing_required_field_rejected() -> None:
    manifest = dict(_VALID_MANIFEST)
    del manifest["name"]
    with pytest.raises(ManifestV2Error) as exc:
        parse_bundle_manifest_v2(
            manifest,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
        )
    assert exc.value.reason == "missing_field"
