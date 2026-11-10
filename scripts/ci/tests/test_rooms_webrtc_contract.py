"""Gate: the rooms app and the webrtc service agree on every consumed tool.

`rooms` (app) may only call tools the `webrtc` (service) bundle exports, with
arguments its input schema accepts. Two files pin the contract:

* `bundles/rust/rooms/tests/contract_calls.json` -- the exact (tool, args) shapes
  rooms sends (the rooms unit test `consumed_tool_calls_match_the_contract_fixture`
  proves the code still sends exactly these);
* `bundles/rust/webrtc/hub-manifest.yaml` -- `exported_tools` input schemas.

Also proves rooms' `requested_tools` / `tool.call:` permissions name only exported
tools, and that webrtc's two manifests agree. Denominators asserted: zero tools or
zero calls examined FAILS.

Run: python3 -m pytest scripts/ci/tests/test_rooms_webrtc_contract.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
WEBRTC = ROOT / "bundles" / "rust" / "webrtc"
ROOMS = ROOT / "bundles" / "rust" / "rooms"
SERVICE = "waddles.core.service.webrtc"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _check(schema: dict[str, Any], value: Any, where: str) -> None:
    """Minimal JSON-schema subset check (type/enum/format/min/max/required/additionalProperties)."""
    if "enum" in schema:
        assert value in schema["enum"], f"{where}: {value!r} not in enum"
        return
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else (kinds or [])
    py = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    if kinds:
        assert any(isinstance(value, py[k]) and not (k == "integer" and isinstance(value, bool)) for k in kinds), (
            f"{where}: wrong type {type(value).__name__} for {kinds}"
        )
    if schema.get("format") == "uuid":
        assert isinstance(value, str) and UUID_RE.match(value), f"{where}: not a lowercase uuid"
    if isinstance(value, str) and "maxLength" in schema:
        assert len(value) <= schema["maxLength"], f"{where}: too long"
    if isinstance(value, int) and not isinstance(value, bool):
        assert value >= schema.get("minimum", value) and value <= schema.get("maximum", value), f"{where}: out of range"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            assert req in value, f"{where}: missing required {req}"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{where}: unknown arguments {sorted(extra)}"
        for k, v in value.items():
            if k in props:
                _check(props[k], v, f"{where}.{k}")


def _manifest(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def test_every_consumed_call_matches_a_webrtc_input_schema() -> None:
    tools = {t["name"]: t for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    calls = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    assert len(tools) >= 12, f"only {len(tools)} webrtc tools examined"
    assert calls, "zero consumed calls examined"
    for name, args in calls.items():
        assert name in tools, f"rooms calls {name}, which webrtc does not export"
        _check(tools[name]["input_schema"], args, name)


def test_rooms_requested_tools_and_permissions_name_only_exported_tools() -> None:
    exported = {t["name"] for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    rooms = _manifest(ROOMS / "hub-manifest.yaml")
    requested = [r for r in rooms["requested_tools"] if r["target_app_id"] == SERVICE]
    assert requested, "rooms requests no webrtc tools"
    for r in requested:
        assert r["tool"] in exported, r
    perm_tools = {
        p["id"].split(":", 2)[2]
        for p in rooms["permissions"]
        if p["id"].startswith(f"tool.call:{SERVICE}:")
    }
    assert perm_tools == {r["tool"] for r in requested}, "tool.call permissions drifted from requested_tools"
    calls = json.loads((ROOMS / "tests" / "contract_calls.json").read_text())
    assert set(calls) <= {r["tool"] for r in requested}, "rooms sends a tool it does not request"


def test_privileged_tools_carry_the_admin_scope_rooms_relies_on() -> None:
    tools = {t["name"]: t for t in _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]}
    for name in ("room.create", "room.close", "token.revoke_all", "admin.verify"):
        assert tools[name].get("required_scope") == "community:admin", name


def test_webrtc_bundle_and_hub_manifests_agree_on_tools() -> None:
    a = _manifest(WEBRTC / "hub-manifest.yaml")["exported_tools"]
    b = _manifest(WEBRTC / "bundle.yaml")["exported_tools"]
    assert a == b, "exported_tools drifted between hub-manifest.yaml and bundle.yaml"
