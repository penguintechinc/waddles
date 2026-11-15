"""Wix tool contract: published `wix.event.*` schemas and the Discord tools the Wix app bundle consumes.

Producer side: `bundles/rust/wix/tests/fixtures/wix_event_tools.json` must equal `exported_tools` in
both Rust manifests (the Rust `tests/contract.rs` ties the same fixture to the sender).
Consumer side: the args `bundles/python/wix` sends to the Discord service tools must satisfy the
Discord manifest's `input_schema` (required keys present, no unknown keys), and every consumed tool
must be declared as a `tool.call` permission AND a `requested_tools` entry in both Python manifests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RUST_WIX = ROOT / "bundles/rust/wix"
PY_WIX = ROOT / "bundles/python/wix"
DISCORD = ROOT / "bundles/rust/discord/bundle.yaml"
EV = {
    "name": "n",
    "scheduled_start_time": "t",
    "scheduled_end_time": "t",
    "location": "l",
}
#: Exactly the args `event_sync` sends (see their `toolcall.call` sites).
CONSUMED: dict[str, dict[str, str]] = {
    "discord.scheduled_event.create": dict(EV),
    "discord.scheduled_event.update": {"event_id": "1", **EV},
    "discord.scheduled_event.cancel": {"event_id": "1"},
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("manifest", ["bundle.yaml", "hub-manifest.yaml"])
def test_rust_manifest_exports_exactly_the_fixture(manifest: str) -> None:
    fixture = json.loads((RUST_WIX / "tests/fixtures/wix_event_tools.json").read_text())["tools"]
    exported = _load(RUST_WIX / manifest)["exported_tools"]
    assert len(fixture) == 5
    assert exported == fixture
    assert {t["name"] for t in exported} == {
        "wix.event.list", "wix.event.get", "wix.event.create", "wix.event.update", "wix.event.cancel",
    }
    for t in exported:
        assert t["risk"] == ("dangerous" if t["access"] == "write" else "normal"), t["name"]


def test_consumed_discord_args_satisfy_the_discord_schemas() -> None:
    tools = {t["name"]: t for t in _load(DISCORD)["exported_tools"]}
    checked = 0
    for name, args in CONSUMED.items():
        schema = tools[name]["input_schema"]
        assert set(schema["required"]) <= set(args), name
        assert set(args) <= set(schema["properties"]), name
        assert schema.get("additionalProperties") is False
        checked += 1
    assert checked == 3


@pytest.mark.parametrize("manifest", ["bundle.yaml", "hub-manifest.yaml"])
def test_python_manifests_declare_every_consumed_tool(manifest: str) -> None:
    doc = _load(PY_WIX / manifest)
    svc = "waddles.core.service.discord"
    perms = {p["id"] for p in doc["permissions"]}
    requested = {(r["tool"], r["target_app_id"], r["access"]) for r in doc["requested_tools"]}
    for name in CONSUMED:
        assert f"tool.call:{svc}:{name}" in perms, name
        assert (name, svc, "write") in requested, name
    wix = [c for c in doc["stages"]["process"]["consumes"] if c["platform"] == "wix"]
    assert len(wix) == 1
    assert set(wix[0]["event_types"]) == {"event.created", "event.updated", "event.canceled"}
    assert not any(n.startswith("discord.member.roles") for n, _s, _a in requested)
