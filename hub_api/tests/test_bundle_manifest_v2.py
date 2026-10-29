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
        trusted_source=bool(kwargs.get("trusted_source", False)),
    )


def _parse_trusted(overrides: dict) -> object:
    """Parse as a trusted install path (core seeder) would."""
    return _parse(overrides, trusted_source=True)


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
    assert _parse_trusted({"kind": "service"}).kind == "service"  # type: ignore[attr-defined]


def test_kind_defaults_to_app() -> None:
    assert _parse({}).kind == "app"  # type: ignore[attr-defined]


def test_kind_invalid() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "widget"})
    assert exc.value.reason == "invalid_kind"


@pytest.mark.parametrize(
    ("app_id", "module", "expected"),
    [
        ("waddles.core.auth", "auth", "app"),
        ("waddles.x.y", "core", "app"),
        ("waddles.x.y", "reputation", "app"),
        ("waddles.x.y", "economy", "app"),
        ("waddles.x.y", "connector", "app"),
        ("waddles.socials.music.default", "socials", "app"),
    ],
)
def test_infer_kind_never_grants_service(app_id: str, module: str, expected: str) -> None:
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
    manifest = _parse_trusted(
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
        _parse_trusted({"kind": "service", "exported_tools": [_tool(access="admin")]})
    assert exc.value.reason == "invalid_tool_access"


def test_exported_tools_bad_risk() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": [_tool(risk="spicy")]})
    assert exc.value.reason == "invalid_tool_risk"


def test_exported_tools_duplicate_name() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": [_tool("x"), _tool("x")]})
    assert exc.value.reason == "duplicate_exported_tool"


@pytest.mark.parametrize("missing", ["input_schema", "output_schema"])
def test_exported_tools_missing_schema(missing: str) -> None:
    entry = _tool()
    del entry[missing]
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": [entry]})
    assert exc.value.reason == "invalid_tool_schema"


def test_exported_tools_survive_reparse_trusted() -> None:
    from services.bundle_approval_service import _reparse_trusted

    raw = {**_VALID_MANIFEST, "kind": "service", "exported_tools": [_tool("a")]}
    assert [t.name for t in _reparse_trusted(raw).exported_tools] == ["a"]


def test_exported_tools_derive_capability() -> None:
    from services.bundle_approval_service import _derive_capabilities

    with_tools = _parse_trusted({"kind": "service", "exported_tools": [_tool()]})
    without = _parse_trusted({"kind": "service"})
    assert "mcp.tools" in _derive_capabilities(with_tools)  # type: ignore[arg-type]
    assert "mcp.tools" not in _derive_capabilities(without)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("missing", "reason"), [("access", "invalid_tool_access"), ("risk", "invalid_tool_risk")]
)
def test_exported_tools_access_risk_required(missing: str, reason: str) -> None:
    entry = _tool()
    del entry[missing]
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": [entry]})
    assert exc.value.reason == reason


@pytest.mark.parametrize("bad", ["Upper", "1abc", "has space", ""])
def test_exported_tools_bad_name(bad: str) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": [_tool(bad)]})
    assert exc.value.reason == "invalid_tool_name"


@pytest.mark.parametrize("bad", ["notalist", {"name": "x"}, ["notadict"]])
def test_exported_tools_invalid_shape(bad: object) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse_trusted({"kind": "service", "exported_tools": bad})
    assert exc.value.reason == "invalid_exported_tools"


_VENDOR = {"provider": "thirdparty", "author": "Vendor", "license": "MIT"}


@pytest.mark.parametrize(
    "overrides",
    [
        {**_VENDOR, "kind": "service"},
        {"kind": "service"},  # builtin provider forged in the manifest: still untrusted
        {
            "kind": "service",
            "module": "core",
            "app_id": "waddles.core.x.default",
            "feature": "waddles.core.x",
        },
    ],
)
def test_untrusted_caller_explicit_service_kind_rejected(overrides: dict) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(overrides)
    assert exc.value.reason == "untrusted_service_kind"


def test_untrusted_caller_with_tools_rejected_even_when_core_namespace() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service", "exported_tools": [_tool()]})
    assert exc.value.reason == "untrusted_service_kind"


def test_untrusted_inferred_service_grants_no_tools_or_capability() -> None:
    from services.bundle_approval_service import _derive_capabilities

    # Absent kind is always "app" (inference never grants service); no trust, no mcp.tools.
    m = _parse(
        {"module": "core", "feature": "waddles.core.music", "app_id": "waddles.core.music.default"}
    )
    assert m.kind == "app"  # type: ignore[attr-defined]
    assert (
        _parse(
            {
                "module": "integrations",
                "feature": "waddles.integrations.x",
                "app_id": "waddles.integrations.x.default",
            }
        ).kind
        == "app"
    )  # type: ignore[attr-defined]
    assert m.trusted_source is False  # type: ignore[attr-defined]
    assert "mcp.tools" not in _derive_capabilities(m)  # type: ignore[arg-type]
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            {
                "module": "core",
                "feature": "waddles.core.music",
                "app_id": "waddles.core.music.default",
                "exported_tools": [_tool()],
            }
        )
    assert exc.value.reason == "app_cannot_export_tools"


def test_trusted_service_kind_allowed_with_mcp_tools() -> None:
    from services.bundle_approval_service import _derive_capabilities

    m = _parse_trusted({"kind": "service", "exported_tools": [_tool()]})
    assert m.kind == "service"  # type: ignore[attr-defined]
    assert "mcp.tools" in _derive_capabilities(m)  # type: ignore[arg-type]


def test_vendor_explicit_app_kind_still_allowed() -> None:
    assert _parse({**_VENDOR, "kind": "app"}).kind == "app"  # type: ignore[attr-defined]


def test_derive_capability_requires_trusted_flag() -> None:
    import dataclasses

    from services.bundle_approval_service import _derive_capabilities

    m = _parse_trusted({"kind": "service", "exported_tools": [_tool()]})
    forged = dataclasses.replace(m, trusted_source=False)  # type: ignore[type-var]
    assert "mcp.tools" not in _derive_capabilities(forged)


def test_integrations_module_not_inferred_service() -> None:
    assert _infer_kind("waddles.integrations.x.default", "integrations") == "app"


def _tool_summary(*tools: dict) -> dict:
    from services.permission_summary_service import build_permission_summary

    m = _parse_trusted({"kind": "service", "exported_tools": list(tools)})
    return build_permission_summary(
        m,  # type: ignore[arg-type]
        grant_labels=[],
        component_capabilities=frozenset(),
        min_tier="free",
        flag_key="f",
        allow_private_hosts=False,
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ([_tool("a")], [_tool("a"), _tool("b")]),
        ([_tool("a")], [_tool("a", access="write")]),
        ([_tool("a")], [_tool("a", risk="dangerous")]),
    ],
)
def test_tool_change_changes_hash_and_widens(old: list, new: list) -> None:
    from services.bundle_approval_service import classify_diff
    from services.permission_summary_service import permission_hash

    old_s, new_s = _tool_summary(*old), _tool_summary(*new)
    assert permission_hash(old_s) != permission_hash(new_s)
    assert classify_diff(new_s, old_s) == "widened"


def test_tool_removal_narrows_and_order_is_hash_stable() -> None:
    from services.bundle_approval_service import classify_diff
    from services.permission_summary_service import permission_hash

    ab, ba = _tool_summary(_tool("a"), _tool("b")), _tool_summary(_tool("b"), _tool("a"))
    assert permission_hash(ab) == permission_hash(ba)
    assert classify_diff(_tool_summary(_tool("a")), ab) == "narrowed"


def _rt(target: str = "waddles.svc.economy.default", tool: str = "balance", **over: object) -> dict:
    return {"target_app_id": target, "tool": tool, "access": "read", **over}


def test_requested_tools_parse_two_for_app() -> None:
    m = _parse({"requested_tools": [_rt(), _rt(tool="spend", access="write")]})
    got = [(r.target_app_id, r.tool, r.access) for r in m.requested_tools]  # type: ignore[attr-defined]
    assert got == [
        ("waddles.svc.economy.default", "balance", "read"),
        ("waddles.svc.economy.default", "spend", "write"),
    ]


def test_requested_tools_default_empty() -> None:
    assert _parse({}).requested_tools == ()  # type: ignore[attr-defined]


def test_requested_tools_service_kind_may_request() -> None:
    m = _parse_trusted({"kind": "service", "requested_tools": [_rt()]})
    assert len(m.requested_tools) == 1  # type: ignore[attr-defined]


def test_requested_tool_permission_id_round_trips_dotted_target() -> None:
    from urllib.parse import unquote

    from services.bundle_manifest_v2 import RequestedTool, requested_tool_permission_id

    pid = requested_tool_permission_id(RequestedTool("a.b:c.d", "x.y", "read"))
    prefix, target, tool = pid.split(":")
    assert prefix == "tool.call"
    assert unquote(target) == "a.b:c.d"
    assert tool == "x.y"


def test_requested_tool_permission_id_no_collision() -> None:
    from services.bundle_manifest_v2 import RequestedTool, requested_tool_permission_id

    a = requested_tool_permission_id(RequestedTool("a.b", "c", "read"))
    b = requested_tool_permission_id(RequestedTool("a", "b.c", "read"))
    assert a != b


def test_explicit_service_trusted_stays_service_core_namespace() -> None:
    m = _parse_trusted(
        {
            "kind": "service",
            "module": "core",
            "feature": "waddles.core.music",
            "app_id": "waddles.core.music.default",
        }
    )
    assert m.kind == "service"  # type: ignore[attr-defined]


def test_untrusted_explicit_service_kind_rejected_untrusted_service_kind() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"kind": "service"})
    assert exc.value.reason == "untrusted_service_kind"


@pytest.mark.parametrize(
    ("target", "tool"),
    [("waddles.svc:evil.default", "balance"), ("waddles.svc.economy.default", "bal:ance")],
)
def test_requested_tools_colon_in_names_rejected(target: str, tool: str) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"requested_tools": [_rt(target=target, tool=tool)]})
    assert exc.value.reason == "invalid_requested_tools"


def test_tool_self_reference() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"requested_tools": [_rt(target=_VALID_MANIFEST["app_id"])]})
    assert exc.value.reason == "tool_self_reference"


def test_requested_tools_duplicate() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"requested_tools": [_rt(), _rt()]})
    assert exc.value.reason == "duplicate_requested_tool"


def test_requested_tools_bad_access() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"requested_tools": [_rt(access="admin")]})
    assert exc.value.reason == "invalid_tool_access"


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        {"a": 1},
        [1],
        [{"tool": "t", "access": "read"}],
        [_rt(tool="Bad Name")],
        [_rt(target="")],
    ],
)
def test_requested_tools_invalid_shape(bad: object) -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"requested_tools": bad})
    assert exc.value.reason == "invalid_requested_tools"


def test_requested_tools_survive_reparse_trusted() -> None:
    from services.bundle_approval_service import _reparse_trusted

    raw = {**_VALID_MANIFEST, "requested_tools": [_rt()]}
    assert [r.tool for r in _reparse_trusted(raw).requested_tools] == ["balance"]
