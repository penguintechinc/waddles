"""The C# ports keep game rules in apps and consume existing service contracts."""

import json
from pathlib import Path

import pytest
import yaml
from defusedxml import ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
GAMES = ("roll", "slots", "gamble", "defuse", "duel", "first", "roulette", "ffa", "heist")


@pytest.mark.parametrize("game", GAMES)
def test_csharp_app_and_its_service_grants_match_the_real_catalog(game):
    """An app cannot silently request a nonexistent or differently classified tool."""
    directory = ROOT / "bundles" / "csharp" / f"superpenguin-{game}"
    manifest = yaml.safe_load((directory / "bundle.yaml").read_text())
    install = yaml.safe_load((directory / "hub-manifest.yaml").read_text())
    assert manifest["language"] == install["language"] == "csharp"
    assert manifest.get("kind", "app") != "service"
    assert manifest["author"] == install["author"] == "github/psychoboy"
    assert manifest["collection"] == install["collection"] == "superpenguintv"
    assert "Copyright (c) 2023 Psychoboy" in manifest["notice"]
    assert manifest["license"] == "MIT"
    assert manifest["permissions"] == install["permissions"]
    assert manifest["requested_tools"] == install["requested_tools"]
    project = ET.parse(directory / f"superpenguin-{game}.csproj")
    assert project.find(".//Wit").attrib["World"] == "stage-next-tools"
    service = yaml.safe_load((ROOT / "bundles/rust/economy/bundle.yaml").read_text())
    exported = {tool["name"]: tool for tool in service["exported_tools"]}
    grants = {permission["id"]: permission for permission in manifest["permissions"]}
    assert manifest["requested_tools"]
    for request in manifest["requested_tools"]:
        assert request["target_app_id"] == "waddles.core.service.economy"
        assert exported[request["tool"]]["access"] == request["access"]
        grant = grants[f"tool.call:{request['target_app_id']}:{request['tool']}"]
        assert grant["params"]["write"] == (request["access"] == "write")
    assert not any(
        key.startswith("economy.") and key != "economy.wager" for key in grants
    )


def test_game_operations_never_become_core_economy_tools():
    """Wagering/max-bet belong to C# apps, not the shared economy service catalog."""
    for name in ("bundle.yaml", "hub-manifest.yaml"):
        manifest = yaml.safe_load((ROOT / "bundles/rust/economy" / name).read_text())
        assert {tool["name"] for tool in manifest["exported_tools"]}.isdisjoint(
            {"wager", "max_bet", "max-bet"}
        )
    for game in ("roll", "slots", "gamble", "defuse", "roulette"):
        directory = ROOT / "bundles/csharp" / f"superpenguin-{game}"
        source = "\n".join(path.read_text() for path in directory.glob("*Logic.cs"))
        assert "host.Economy.Wager(" in source


def test_steal_requests_only_the_transfer_from_contract_and_never_a_private_ledger():
    """Steal's victim debit goes through the economy `transfer_from` tool, coded to a contract fixture."""
    directory = ROOT / "bundles/csharp/superpenguin-steal"
    manifest = yaml.safe_load((directory / "bundle.yaml").read_text())
    install = yaml.safe_load((directory / "hub-manifest.yaml").read_text())
    fixture = json.loads((directory / "contract/transfer_from.json").read_text())
    assert manifest["permissions"] == install["permissions"]
    assert manifest["requested_tools"] == install["requested_tools"]
    assert manifest["author"] == "github/psychoboy"
    assert manifest["collection"] == "superpenguintv"
    assert "Copyright (c) 2023 Psychoboy" in manifest["notice"]
    requests = {r["tool"]: r for r in manifest["requested_tools"]}
    assert requests["transfer_from"]["access"] == fixture["access"] == "write"
    assert set(requests) == {"points_view", "get_balance", "transfer_from"}
    grants = {p["id"]: p for p in manifest["permissions"]}
    assert not any(key.startswith("economy.") for key in grants)
    service = yaml.safe_load((ROOT / "bundles/rust/economy/bundle.yaml").read_text())
    exported = {tool["name"]: tool for tool in service["exported_tools"]}
    if "transfer_from" in exported:  # once the economy service ships it, it must match the fixture
        tool = exported["transfer_from"]
        assert tool["access"] == fixture["access"]
        assert tool.get("risk") == fixture["risk"]
        assert set(fixture["args"]) <= set(tool["input_schema"]["properties"])
