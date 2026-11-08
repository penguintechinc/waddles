"""Wave-3 SuperPenguinTV utility ports: manifests agree, grants match the real service catalogs, no secrets in the guest."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
NAMES = (
    "lastseen",
    "deathcounter",
    "dailycounter",
    "quotes",
    "raidtracker",
    "top",
    "alias",
    "timers",
    "weather",
    "viewers",
)
SERVICES = {
    "waddles.core.service.economy": ROOT / "bundles/rust/economy/bundle.yaml",
    "waddles.core.service.reputation": ROOT / "bundles/rust/reputation/bundle.yaml",
}


def _load(name: str, file: str) -> dict:
    return yaml.safe_load(
        (ROOT / "bundles/csharp" / f"superpenguin-{name}" / file).read_text()
    )


@pytest.mark.parametrize("name", NAMES)
def test_manifests_agree_and_carry_attribution_and_the_verbatim_mit_notice(name):
    manifest, install = _load(name, "bundle.yaml"), _load(name, "hub-manifest.yaml")
    assert (
        manifest["app_id"]
        == install["app_id"]
        == f"waddles.integrations.superpenguin.{name}"
    )
    assert manifest["language"] == install["language"] == "csharp"
    assert manifest["author"] == install["author"] == "github/psychoboy"
    assert manifest["collection"] == install["collection"] == "superpenguintv"
    assert manifest["license"] == "MIT"
    assert manifest.get("kind", "app") != "service"
    assert manifest["permissions"] == install["permissions"]
    assert manifest["requested_tools"] == install["requested_tools"]
    assert (
        manifest["stages"]["process"]["consumes"]
        == install["stages"]["process"]["consumes"]
    )
    upstream = (
        (ROOT / "bundles/csharp/superpenguin-first/LICENSE").read_text().strip("\n")
    )
    assert upstream in manifest["notice"]
    assert manifest["notice"] == install["notice"]
    project = ET.parse(
        ROOT / "bundles/csharp" / f"superpenguin-{name}" / f"superpenguin-{name}.csproj"
    )
    assert project.find(".//Wit").attrib["World"] == "stage-next-tools"


@pytest.mark.parametrize("name", NAMES)
def test_every_tool_request_matches_a_real_service_export_and_has_its_grant(name):
    manifest = _load(name, "bundle.yaml")
    grants = {p["id"]: p for p in manifest["permissions"]}
    for request in manifest["requested_tools"]:
        service = yaml.safe_load(SERVICES[request["target_app_id"]].read_text())
        exported = {t["name"]: t for t in service["exported_tools"]}
        assert exported[request["tool"]]["access"] == request["access"]
        grant = grants[f"tool.call:{request['target_app_id']}:{request['tool']}"]
        assert grant["params"]["write"] == (request["access"] == "write")
    assert not any(k.startswith("economy.") for k in grants), (
        "utility ports never touch the host ledger"
    )


def test_top_reads_only_service_leaderboards():
    manifest = _load("top", "bundle.yaml")
    assert {(r["target_app_id"], r["tool"]) for r in manifest["requested_tools"]} == {
        ("waddles.core.service.economy", "points_leaders"),
        ("waddles.core.service.reputation", "leaderboard"),
    }
    assert all(r["access"] == "read" for r in manifest["requested_tools"])


def test_weather_egress_is_one_declared_fqdn_with_the_key_as_a_secret_reference_only():
    manifest = _load("weather", "bundle.yaml")
    assert manifest["egress"] == [{"host": "api.weatherapi.com", "methods": ["GET"]}]
    fqdn = [p for p in manifest["permissions"] if p["id"].startswith("net.http.fqdn:")]
    assert [p["id"] for p in fqdn] == ["net.http.fqdn:api.weatherapi.com"]
    directory = ROOT / "bundles/csharp/superpenguin-weather"
    source = (directory / "WeatherLogic.cs").read_text()
    assert 'KeySecretRef = "WEATHER_API_KEY"' in source
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("///")
    )
    assert "key=" not in code, "the API key must never be built into the URL"


@pytest.mark.parametrize("name", NAMES)
def test_each_bundle_gates_on_its_own_default_off_flag_and_keys_have_no_colon(name):
    directory = ROOT / "bundles/csharp" / f"superpenguin-{name}"
    logic = next(directory.glob("*Logic.cs")).read_text()
    assert f'"waddles.command-superpenguin-{name}"' in logic
    assert "Flags.Enabled(FlagKey, false)" in logic
    for token in ('StateKey.Of("', 'Key = "'):
        for line in logic.splitlines():
            if token in line and ":" in line.split(token, 1)[1].split('"', 1)[0]:
                pytest.fail(f"kv key with ':' in {name}: {line.strip()}")


def test_passive_trackers_declare_no_command_prefix_filter():
    for name in ("lastseen", "viewers", "timers", "alias"):
        for entry in _load(name, "bundle.yaml")["stages"]["process"]["consumes"]:
            assert "filters" not in entry, f"{name} must receive every chat message"
