"""Contract test: bundle.yaml / hub-manifest.yaml exported_tools == tests/contract/*.json.

The fixtures are the published tool contract (consumers: AI Teammates, DJ). Any drift
between a fixture and either manifest, or between the two manifests, fails loud.
Run: python3 -m pytest bundles/rust/ai/tests/test_manifest_contract.py
"""

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ["ai.complete", "ai.classify", "ai.rag.query", "ai.rag.ingest"]
FIELDS = ("access", "risk", "required_scope", "input_schema", "output_schema")


def _fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "contract" / f"{name}.json").read_text())


def _tools(manifest: str) -> dict:
    data = yaml.safe_load((ROOT / manifest).read_text())
    return {t["name"]: t for t in data["exported_tools"]}


def test_manifests_match_fixtures() -> None:
    examined = 0
    for manifest in ("bundle.yaml", "hub-manifest.yaml"):
        tools = _tools(manifest)
        assert list(tools) == TOOLS, manifest
        for name in TOOLS:
            fx = _fixture(name)
            for f in FIELDS:
                assert tools[name].get(f) == fx.get(f), f"{manifest} {name}.{f}"
                examined += 1
    assert examined == 2 * len(TOOLS) * len(FIELDS)


def test_inputs_reject_unknown_fields_and_ingest_is_admin_only() -> None:
    for name in TOOLS:
        assert _fixture(name)["input_schema"]["additionalProperties"] is False
    ingest = _fixture("ai.rag.ingest")
    assert ingest["required_scope"] == "community:admin"
    assert ingest["risk"] == "dangerous"


def test_hub_manifest_requests_only_ai_generate_and_kv() -> None:
    perms = yaml.safe_load((ROOT / "hub-manifest.yaml").read_text())["permissions"]
    assert sorted(p["id"] for p in perms) == ["ai.generate", "storage.kv"]
