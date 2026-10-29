"""Tests for the bundle.yaml v2 pure-YAML validation subset hub-api checks pre-Job."""

from __future__ import annotations

import pytest

from services.bundle_manifest_v2 import (
    ManifestV2Error,
    _infer_kind,
    parse_bundle_manifest_v2,
)

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


def test_csharp_language_accepted() -> None:
    """`language: csharp` (bundles/csharp/csping, PR #404) must onboard like any Tier 1 language."""
    manifest = _parse({"language": "csharp"})
    assert manifest.language == "csharp"  # type: ignore[attr-defined]


def test_unsupported_language_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"language": "cobol"})
    assert exc.value.reason == "unsupported_language"


# ---------------------------------------------------------------------------
# Attribution/marketplace metadata (migration 0026): author/license/
# source_url/alternative_to/homepage_url/notice/category.
# ---------------------------------------------------------------------------


def test_vendor_bundle_without_author_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"provider": "thirdparty", "license": "MIT"})
    assert exc.value.reason == "vendor_author_required"


def test_vendor_bundle_without_license_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"provider": "thirdparty", "author": "Acme Corp"})
    assert exc.value.reason == "vendor_license_required"


def test_builtin_bundle_without_attribution_parses() -> None:
    manifest = _parse({})
    assert manifest.author is None  # type: ignore[attr-defined]
    assert manifest.license is None  # type: ignore[attr-defined]
    assert manifest.license_requires_review is False  # type: ignore[attr-defined]


def test_unknown_spdx_license_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"provider": "thirdparty", "author": "Acme Corp", "license": "Not-A-Real-License"})
    assert exc.value.reason == "unknown_spdx_license"


def test_allowlisted_spdx_license_does_not_require_review() -> None:
    manifest = _parse({"provider": "thirdparty", "author": "Acme Corp", "license": "Apache-2.0"})
    assert manifest.license_requires_review is False  # type: ignore[attr-defined]


def test_copyleft_spdx_license_flags_review_required() -> None:
    manifest = _parse({"provider": "thirdparty", "author": "Acme Corp", "license": "GPL-3.0-only"})
    assert manifest.license == "GPL-3.0-only"  # type: ignore[attr-defined]
    assert manifest.license_requires_review is True  # type: ignore[attr-defined]


def test_http_source_url_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"source_url": "http://example.com/repo"})
    assert exc.value.reason == "invalid_source_url"


def test_https_source_url_parses() -> None:
    manifest = _parse({"source_url": "https://github.com/example/repo"})
    assert manifest.source_url == "https://github.com/example/repo"  # type: ignore[attr-defined]


def test_http_homepage_url_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"homepage_url": "http://example.com"})
    assert exc.value.reason == "invalid_homepage_url"


def test_empty_alternative_to_entry_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"alternative_to": [""]})
    assert exc.value.reason == "invalid_alternative_to"


def test_alternative_to_list_parses_through() -> None:
    manifest = _parse({"alternative_to": ["waddles.socials.music.default", "Spotify Jukebox"]})
    assert manifest.alternative_to == (  # type: ignore[attr-defined]
        "waddles.socials.music.default",
        "Spotify Jukebox",
    )


def test_empty_notice_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"notice": ""})
    assert exc.value.reason == "invalid_notice"


def test_invalid_category_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"category": "not-a-real-category"})
    assert exc.value.reason == "invalid_category"


def test_alternatives_category_parses_through() -> None:
    manifest = _parse({"category": "alternatives"})
    assert manifest.category == "alternatives"  # type: ignore[attr-defined]


def test_supported_platforms_absent_means_all_platforms() -> None:
    manifest = _parse({})
    assert manifest.supported_platforms is None  # type: ignore[attr-defined]
    assert manifest.supports_platform("twitch")  # type: ignore[attr-defined]
    assert manifest.supports_platform("discord")  # type: ignore[attr-defined]


def test_supported_platforms_gates_platform() -> None:
    manifest = _parse(
        {
            "supported_platforms": ["discord", "slack"],
            "stages": {
                "process": {
                    "entry": "x:y",
                    "consumes": [{"platform": "discord", "event_types": ["chat.message"]}],
                }
            },
        }
    )
    assert manifest.supported_platforms == ("discord", "slack")  # type: ignore[attr-defined]
    assert manifest.supports_platform("discord")  # type: ignore[attr-defined]
    assert not manifest.supports_platform("twitch")  # type: ignore[attr-defined]


@pytest.mark.parametrize("bad", [[], "discord", ["myspace"], [1]])
def test_supported_platforms_invalid_rejected(bad: object) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"supported_platforms": bad})
    assert exc.value.reason == "invalid_supported_platforms"


def test_consumes_platform_outside_supported_platforms_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"supported_platforms": ["discord"]})  # base manifest consumes twitch
    assert exc.value.reason == "consumes_platform_unsupported"


def test_kind_explicit_service() -> None:
    assert _parse({"kind": "service"}).kind == "service"  # type: ignore[attr-defined]


def test_kind_defaults_inferred() -> None:
    expected = _infer_kind(_VALID_MANIFEST["app_id"], _VALID_MANIFEST["module"])
    assert _parse({}).kind == expected  # type: ignore[attr-defined]


def test_kind_invalid() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "widget"})
    assert exc.value.reason == "invalid_kind"


@pytest.mark.parametrize(
    ("app_id", "module", "expected"),
    [
        ("waddles.core.auth", "auth", "service"),
        ("waddles.x.y", "reputation", "service"),
        ("waddles.x.y", "economy", "service"),
        ("waddles.x.y", "connector", "service"),
        ("waddles.socials.music.default", "socials", "app"),
    ],
)
def test_infer_kind_heuristic(app_id: str, module: str, expected: str) -> None:
    assert _infer_kind(app_id, module) == expected


def test_kind_survives_reparse_trusted() -> None:
    from services.bundle_approval_service import _reparse_trusted

    raw = {**_VALID_MANIFEST, "kind": "service"}
    assert _reparse_trusted(raw).kind == "service"
    assert _reparse_trusted(dict(_VALID_MANIFEST)).kind == "app"


_SCHEMA = {"type": "object", "properties": {}}


def _tool(name: str = "lookup", **over: object) -> dict:
    return {
        "name": name,
        "input_schema": _SCHEMA,
        "output_schema": _SCHEMA,
        "access": "read",
        "risk": "normal",
        **over,
    }


def test_exported_tools_service_parses() -> None:
    manifest = _parse(
        {
            "kind": "service",
            "exported_tools": [_tool("a"), _tool("b", access="write", risk="dangerous")],
        }
    )
    tools = manifest.exported_tools  # type: ignore[attr-defined]
    assert [t.name for t in tools] == ["a", "b"]
    assert (tools[1].access, tools[1].risk) == ("write", "dangerous")
    assert tools[0].input_schema == _SCHEMA


def test_exported_tools_default_empty() -> None:
    assert _parse({}).exported_tools == ()  # type: ignore[attr-defined]


def test_app_cannot_export_tools() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "app", "exported_tools": [_tool()]})
    assert exc.value.reason == "app_cannot_export_tools"


def test_exported_tools_bad_access() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service", "exported_tools": [_tool(access="admin")]})
    assert exc.value.reason == "invalid_tool_access"


def test_exported_tools_bad_risk() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service", "exported_tools": [_tool(risk="spicy")]})
    assert exc.value.reason == "invalid_tool_risk"


def test_exported_tools_duplicate_name() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service", "exported_tools": [_tool("x"), _tool("x")]})
    assert exc.value.reason == "duplicate_exported_tool"


@pytest.mark.parametrize("missing", ["input_schema", "output_schema"])
def test_exported_tools_missing_schema(missing: str) -> None:
    entry = _tool()
    del entry[missing]
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service", "exported_tools": [entry]})
    assert exc.value.reason == "invalid_tool_schema"


def test_exported_tools_survive_reparse_trusted() -> None:
    from services.bundle_approval_service import _reparse_trusted

    raw = {**_VALID_MANIFEST, "kind": "service", "exported_tools": [_tool("a")]}
    assert [t.name for t in _reparse_trusted(raw).exported_tools] == ["a"]


def test_exported_tools_derive_capability() -> None:
    from services.bundle_approval_service import _derive_capabilities

    with_tools = _parse({"kind": "service", "exported_tools": [_tool()]})
    without = _parse({"kind": "service"})
    assert "mcp.tools" in _derive_capabilities(with_tools)  # type: ignore[arg-type]
    assert "mcp.tools" not in _derive_capabilities(without)  # type: ignore[arg-type]
