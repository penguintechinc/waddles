"""Manifest + host-capability tripwires for `chatguard`.

The manifest must request exactly the least-privilege set, and the platforms the code tries to
`chat.delete` on must be exactly the platforms the manifest holds `chat.delete:<platform>` for.
The host-capability pins read the Rust/WIT sources: they FAIL (never skip) when the host gains a
timeout op or Twitch delete, because chatguard's documented gap must then be revisited.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import app

BUNDLE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BUNDLE_DIR.parents[2]
MANIFESTS = ("bundle.yaml", "hub-manifest.yaml")
EXPECTED_PERMISSIONS = {
    "storage.kv",
    "identity.resolve",
    "chat.delete:discord",
    "chat.send:discord",
    "chat.send:twitch",
}


def _load(manifest: str) -> dict[str, object]:
    raw = yaml.safe_load((BUNDLE_DIR / manifest).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _permission_ids(manifest: str) -> list[str]:
    perms = _load(manifest)["permissions"]
    assert isinstance(perms, list)
    return [str(p["id"]) for p in perms]


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_permissions_are_exactly_the_least_privilege_set(manifest: str) -> None:
    ids = _permission_ids(manifest)
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_PERMISSIONS


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_no_invented_moderation_capability(manifest: str) -> None:
    for perm_id in _permission_ids(manifest):
        assert "timeout" not in perm_id and "ban" not in perm_id
        assert not perm_id.startswith(("moderation.", "dm.send", "economy."))


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_delete_permissions_match_the_code(manifest: str) -> None:
    declared = {
        i.partition(":")[2] for i in _permission_ids(manifest) if i.startswith("chat.delete:")
    }
    assert declared == set(app.DELETE_PLATFORMS)
    sends = {i.partition(":")[2] for i in _permission_ids(manifest) if i.startswith("chat.send:")}
    assert sends == {"discord", "twitch"}


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_justifications_fit_the_parser_limit(manifest: str) -> None:
    perms = _load(manifest)["permissions"]
    assert isinstance(perms, list)
    for perm in perms:
        assert 0 < len(str(perm["justification"])) <= 280


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_limits_are_inside_the_parser_ranges(manifest: str) -> None:
    limits = _load(manifest)["limits"]
    assert isinstance(limits, dict)
    assert 50 <= limits["timeout_ms"] <= 10_000
    assert 8 <= limits["memory_mb"] <= 256
    assert 1 <= limits["egress_rps"] <= 10


def test_both_manifest_files_agree_on_identity_and_version() -> None:
    a, b = (_load(m) for m in MANIFESTS)
    for field in ("app_id", "version", "name", "feature", "module", "provider"):
        assert a[field] == b[field], field
    assert a["app_id"] == "waddles.core.example.chatguard"


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_every_chat_line_is_consumed_on_both_platforms_with_no_prefix_filter(
    manifest: str,
) -> None:
    stages = _load(manifest)["stages"]
    assert isinstance(stages, dict)
    consumes = stages["process"]["consumes"]
    assert {c["platform"] for c in consumes} == {"twitch", "discord"}
    for c in consumes:
        assert c["event_types"] == ["chat.message"]
        assert "filters" not in c  # ordinary chat lines must reach the filters


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_no_feature_flag_activation_gated_only(manifest: str) -> None:
    assert "flags.read" not in _permission_ids(manifest)
    source = (BUNDLE_DIR / "src" / "app.py").read_text(encoding="utf-8")
    assert "feature_enabled" not in source and "FLAG_KEY" not in source


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing -- these tripwires need the full repo checkout"
    return path.read_text(encoding="utf-8")


def test_host_relay_ops_are_exactly_send_delete_dm() -> None:
    """No `chat.timeout` relay op exists -- if one appears, wire it and delete this tripwire."""
    source = _read("core/svc_action/src/capabilities.rs")
    enum = re.search(r"enum RelayOp \{(?P<body>.*?)\n\}", source, re.DOTALL)
    assert enum is not None, "RelayOp enum moved -- re-verify chatguard's moderation capabilities"
    variants = set(re.findall(r"^\s+(\w+),", enum.group("body"), re.MULTILINE))
    assert variants == {"ChatSend", "ChatDelete", "DmSend"}, (
        f"the host relay ops changed to {sorted(variants)}: chatguard can now use a timeout/"
        "ban op (add it, its permission and tests) -- see the module docstring's capability note"
    )


def test_host_only_implements_chat_delete_on_discord() -> None:
    """Twitch delete is authorized-then-refused: chatguard must stay warn-only there."""
    source = _read("core/svc_action/src/capabilities.rs")
    table = re.search(
        r"fn relay_op_supported\(.*?\) -> bool \{(?P<body>.*?)\n\}", source, re.DOTALL
    )
    assert table is not None, "relay_op_supported moved -- re-verify chatguard's platforms"
    body = table.group("body")
    assert '"discord"' in body and "RelayOp::ChatDelete" in body
    assert not re.search(r'"twitch",\s*RelayOp::ChatDelete', body), (
        "the host now deletes on Twitch: add chat.delete:twitch to the manifests and "
        "DELETE_PLATFORMS, and extend the tests"
    )
    assert app.DELETE_PLATFORMS == {"discord"}


def test_the_wit_world_has_no_moderation_import() -> None:
    """The gate catalog lists `moderation.<platform>`, but no WIT import carries it yet."""
    wit = _read("wit/waddle-bundle/stage.wit")
    imports = set(re.findall(r"^\s*import\s+%?([\w-]+);", wit, re.MULTILINE))
    assert {"kv", "relay", "identity"} <= imports, "the WIT import regex matched nothing useful"
    assert "moderation" not in imports, (
        "the stage WIT now imports `moderation`: chatguard's timeout gap can be closed"
    )
