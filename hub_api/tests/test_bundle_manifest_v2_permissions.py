"""Tests for the structured `permissions:` block (spec Sec2.1/2.2/2.3)."""

from __future__ import annotations

import pathlib

import pytest
import yaml

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


def test_interaction_pii_receive_parses_as_dangerous() -> None:
    """No `params` shape required -- same as any other non-parameterized dangerous entry."""
    manifest = _parse(
        [{"id": "interaction.pii.receive", "justification": "Reads a viewer-typed email."}]
    )
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert len(decls) == 1
    assert decls[0].id == "interaction.pii.receive"
    assert decls[0].risk == "dangerous"


def test_interaction_pii_receive_requires_justification() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse([{"id": "interaction.pii.receive", "justification": ""}])
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


def test_net_http_fqdn_requires_valid_methods() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.fqdn:api.example.com",
                    "justification": "Calls the example API.",
                    "params": {"methods": ["TRACE"]},
                }
            ]
        )
    assert exc.value.reason == "invalid_net_http_method"


def test_net_http_fqdn_requires_methods_present() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.fqdn:api.example.com",
                    "justification": "Calls the example API.",
                    "params": {},
                }
            ]
        )
    assert exc.value.reason == "invalid_net_http_method"


def test_net_http_fqdn_valid_methods_parses() -> None:
    manifest = _parse(
        [
            {
                "id": "net.http.fqdn:api.example.com",
                "justification": "Calls the example API.",
                "params": {"methods": ["GET", "POST"]},
            }
        ]
    )
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert decls[0].id == "net.http.fqdn:api.example.com"
    assert decls[0].risk == "normal"
    assert decls[0].params["methods"] == ["GET", "POST"]


def test_net_http_public_ip_is_dangerous_and_requires_methods() -> None:
    manifest = _parse(
        [
            {
                "id": "net.http.public-ip:93.184.216.34",
                "justification": "Calls a vendor API with no stable hostname.",
                "params": {"methods": ["GET"]},
            }
        ]
    )
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert decls[0].risk == "dangerous"


def test_net_http_public_ip_rejects_cidr() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.public-ip:93.184.216.0/24",
                    "justification": "x",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert exc.value.reason == "unknown_permission"


def test_net_http_private_ip_is_dangerous_within_prefix_bound() -> None:
    manifest = _parse(
        [
            {
                "id": "net.http.private-ip:10.20.0.0/16",
                "justification": "Calls an internal partner appliance.",
                "params": {"methods": ["GET", "POST"]},
            }
        ]
    )
    decls = manifest.permission_declarations  # type: ignore[attr-defined]
    assert decls[0].risk == "dangerous"


def test_net_http_private_ip_rejects_coarser_than_bound() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.private-ip:10.0.0.0/8",
                    "justification": "x",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert exc.value.reason == "unknown_permission"


def test_net_http_private_ip_rejects_loopback() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.private-ip:127.0.0.1",
                    "justification": "x",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert exc.value.reason == "unknown_permission"


def test_net_http_private_ip_rejects_metadata_address() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "net.http.private-ip:169.254.169.254",
                    "justification": "x",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert exc.value.reason == "unknown_permission"


def test_net_http_ip_family_emits_advisory_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="services.bundle_manifest_v2"):
        _parse(
            [
                {
                    "id": "net.http.public-ip:93.184.216.34",
                    "justification": "Calls a vendor API with no stable hostname.",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert any("net.http.fqdn" in record.message for record in caplog.records)


def test_net_http_fqdn_family_no_advisory_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="services.bundle_manifest_v2"):
        _parse(
            [
                {
                    "id": "net.http.fqdn:api.example.com",
                    "justification": "Calls the example API.",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert caplog.records == []


def test_methods_param_rejected_on_non_net_http_permission() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "storage.kv",
                    "justification": "x",
                    "params": {"methods": ["GET"]},
                }
            ]
        )
    assert exc.value.reason == "invalid_net_http_method"


def test_overlay_media_wildcard_host_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "overlay.media",
                    "justification": "Plays a clip.",
                    "params": {"allowed_hosts": ["*.evil.com"], "max_duration_seconds": 10},
                }
            ]
        )
    assert exc.value.reason == "invalid_overlay_hosts"


def test_overlay_media_garbage_host_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse(
            [
                {
                    "id": "overlay.media",
                    "justification": "Plays a clip.",
                    "params": {
                        "allowed_hosts": ["javascript://alert(1)"],
                        "max_duration_seconds": 10,
                    },
                }
            ]
        )
    assert exc.value.reason == "invalid_overlay_hosts"


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


def _core_hub_manifests() -> list[pathlib.Path]:
    root = pathlib.Path(__file__).resolve().parents[2] / "bundles"
    return sorted(root.glob("*/*/hub-manifest.yaml"))


def test_core_hub_manifests_have_no_bare_string_permissions() -> None:
    """Regression: bare-string `permissions:` parse to zero grants (dead shape)."""
    paths = _core_hub_manifests()
    assert len(paths) >= 30, f"only {len(paths)} core hub manifests examined"
    bare = []
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        if any(isinstance(e, str) for e in raw.get("permissions") or []):
            bare.append(str(path))
    assert bare == []


def test_core_kv_manifests_parse_to_structured_storage_kv_declaration() -> None:
    """Every converted core bundle yields >=1 structured declaration => grant rows."""
    structured = 0
    for path in _core_hub_manifests():
        raw = yaml.safe_load(path.read_text())
        if not raw.get("permissions"):
            continue
        manifest = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=True,
            allow_prebuilt=True,
        )
        ids = [d.id for d in manifest.permission_declarations]  # type: ignore[attr-defined]
        assert ids, f"{path}: zero structured permission declarations"
        assert manifest.permissions == (), f"{path}: leftover bare strings"  # type: ignore[attr-defined]
        structured += 1
    assert structured >= 30


@pytest.mark.parametrize(
    "name",
    ["chat", "choose", "eightball", "poll", "roll", "slap", "wave", "hug", "boop", "highfive"],
)
def test_flag_gated_python_bundles_declare_flags_read(name: str) -> None:
    """Bundles calling `feature_enabled` must declare `flags.read` (host gate fails closed)."""
    root = pathlib.Path(__file__).resolve().parents[2] / "bundles" / "python" / name
    assert "feature_enabled(" in (root / "src" / "app.py").read_text()
    for fname in ("bundle.yaml", "hub-manifest.yaml"):
        raw = yaml.safe_load((root / fname).read_text())
        ids = [e["id"] for e in raw["permissions"]]
        assert "flags.read" in ids, f"{root / fname}: missing flags.read"


def test_flag_reading_core_bundles_declare_flags_read() -> None:
    """Every core bundle calling `feature_enabled` (with permissions) declares flags.read."""
    examined = 0
    missing = []
    for path in _core_hub_manifests():
        raw = yaml.safe_load(path.read_text())
        if not raw.get("permissions"):
            continue
        src = path.parent / "src"
        if not any("await feature_enabled(" in f.read_text() for f in src.rglob("*.py")):
            continue
        examined += 1
        ids = {e["id"] for e in raw["permissions"] if isinstance(e, dict)}
        if "flags.read" not in ids:
            missing.append(str(path))
    assert examined >= 30, f"only {examined} flag-reading bundles examined"
    assert missing == []
