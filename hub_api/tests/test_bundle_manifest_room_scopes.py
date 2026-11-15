"""The dedicated room-management scopes are part of the closed tool-scope vocabulary.

A role is only a predefined bundle of scopes (`scopes_for_role` in `core/bundle_tool_call`):
manifests declare the specific scope a tool needs, and the webrtc service manifest must only
use scopes this validator accepts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from services.bundle_manifest_v2 import TOOL_REQUIRED_SCOPES

ROOM_SCOPES = ("rooms:create", "rooms:update", "rooms:close", "rooms:guests")
WEBRTC = Path(__file__).resolve().parents[2] / "bundles" / "rust" / "webrtc"


def test_room_scopes_are_in_the_closed_vocabulary() -> None:
    """Every dedicated room scope is accepted, alongside the existing community scopes."""
    for scope in ROOM_SCOPES:
        assert scope in TOOL_REQUIRED_SCOPES, scope
    assert {"community:member", "community:moderate", "community:admin"} <= TOOL_REQUIRED_SCOPES
    assert "rooms:*" not in TOOL_REQUIRED_SCOPES and "rooms:admin" not in TOOL_REQUIRED_SCOPES


def test_webrtc_manifests_only_require_known_scopes_and_room_tools_use_the_dedicated_ones() -> None:
    """Both webrtc manifests validate against the vocabulary; room mgmt uses dedicated scopes."""
    examined = 0
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        doc = yaml.safe_load((WEBRTC / name).read_text())
        tools = {t["name"]: t for t in doc["exported_tools"]}
        for tool in tools.values():
            scope = tool.get("required_scope")
            assert scope is None or scope in TOOL_REQUIRED_SCOPES, (name, tool["name"], scope)
            examined += 1
        assert tools["room.create"]["required_scope"] == "rooms:create"
        assert tools["room.update"]["required_scope"] == "rooms:update"
        assert tools["room.close"]["required_scope"] == "rooms:close"
        assert tools["room.update"]["risk"] == "dangerous"
        assert tools["room.create"]["risk"] == "dangerous"
    assert examined >= 60, f"only {examined} webrtc tools examined"
