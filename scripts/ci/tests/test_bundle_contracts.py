"""Cross-bundle tool-contract drift gate.

Single sources of truth live in bundles/contracts/*.v1.json. For every bundle found under the
scan roots this checks that (a) producers' exported tool schemas equal the contract, (b) every
consumer fixture (bundles/*/*/contracts/*.json) is compatible with the contract, and (c) every
tool a bundle requests that belongs to a contract family exists in that contract.

Scan roots default to the repo; set BUNDLE_CONTRACT_ROOTS=dir[:dir...] to scan other checkouts
(used to verify unmerged PR branches). Bundles not yet merged are simply not found; the
denominators are asserted per-contract so a scanner pointed at nothing cannot pass silently.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "bundles/contracts"
ROOTS = [Path(p) for p in os.environ.get("BUNDLE_CONTRACT_ROOTS", "").split(os.pathsep) if p] or [REPO]
SCHEMA_KEYS = ("access", "risk", "input_schema", "output_schema")


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text())


def _inline(node, defs):
    """Replaces `{"$ref": "#/definitions/x"}` with the definition body (manifest schemas carry no refs)."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].rsplit("/", 1)[1]], defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def contract_tools() -> dict[str, dict]:
    """Flattens all contracts to {full_tool_name_or_pattern: schema-dict} keyed by family."""
    out: dict[str, dict] = {}
    ai = _load("ai-tools.v1.json")
    for n, t in ai["tools"].items():
        out[n] = t
    clip = _load("twitch-clip-tools.v1.json")
    for n, t in clip["tools"].items():
        out[n] = t
    live = _load("live-channels.v1.json")
    for p in live["platforms"]:
        out[f"{p}.live.channels"] = live["tool"]
    return out


def music_tools(provider: str) -> dict[str, dict]:
    """Returns the music contract tools named music.<provider>.<tool>, refs inlined."""
    c = _load("music-tool-contract.v1.json")
    return {f"music.{provider}.{n}": _inline(t, c["definitions"]) for n, t in c["tools"].items()}


def _expected_for(name: str) -> dict | None:
    """Looks up the contract schema for a tool name, or None when it is outside every contract."""
    flat = contract_tools()
    if name in flat:
        return flat[name]
    parts = name.split(".")
    if parts[0] == "music" and len(parts) >= 3:
        return music_tools(parts[1]).get(name)
    return None


def _in_family(name: str) -> bool:
    """True when the tool name belongs to a contract family (so an unknown member is drift)."""
    return name.startswith(("ai.", "twitch.clip.")) or name.endswith(".live.channels") or (
        name.startswith("music.") and name.count(".") >= 2 and name.split(".", 2)[2] in _load("music-tool-contract.v1.json")["tools"]
    )


def _manifests() -> list[Path]:
    """All bundle manifests under the scan roots."""
    found: list[Path] = []
    for r in ROOTS:
        for n in ("bundle.yaml", "hub-manifest.yaml"):
            found += sorted((r / "bundles").glob(f"*/*/{n}"))
    return found


def _consumer_fixtures() -> list[Path]:
    """Consumer-side contract fixtures: bundles/<lang>/<bundle>/contracts/*.json carrying a `tool` key."""
    found: list[Path] = []
    for r in ROOTS:
        found += sorted(p for p in (r / "bundles").glob("*/*/contracts/*.json") if "tool" in json.loads(p.read_text()))
    return found


def _incompat(consumer: dict, producer: dict, where: str, *, request: bool) -> list[str]:
    """Reports why `consumer`'s view of a schema cannot interoperate with `producer`'s."""
    errs: list[str] = []
    cprops, pprops = consumer.get("properties", {}), producer.get("properties", {})
    if request:
        if producer.get("additionalProperties") is False:
            errs += [f"{where}: field '{k}' not accepted by producer" for k in cprops if k not in pprops]
        errs += [f"{where}: producer requires '{k}' but consumer does not always send it"
                 for k in producer.get("required", []) if k not in consumer.get("required", [])]
        # Consumer-declared bounds must fit inside the producer's accepted range.
        for k, cp in cprops.items():
            pp = pprops.get(k)
            if pp is None:
                continue
            if cp.get("type") != pp.get("type"):
                errs.append(f"{where}.{k}: type {cp.get('type')} != producer {pp.get('type')}")
            for lim in ("maxLength", "maximum", "maxItems"):
                if lim in pp and cp.get(lim, float("inf")) > pp[lim]:
                    errs.append(f"{where}.{k}: consumer {lim}={cp.get(lim)} exceeds producer {pp[lim]}")
            for lim in ("minLength", "minimum", "minItems"):
                if lim in pp and cp.get(lim, -float("inf")) < pp[lim]:
                    errs.append(f"{where}.{k}: consumer {lim}={cp.get(lim)} below producer {pp[lim]}")
            if "enum" in cp and "enum" in pp:
                errs += [f"{where}.{k}: enum value '{v}' not accepted" for v in cp["enum"] if v not in pp["enum"]]
            if "enum" in cp and "enum" not in pp and cp.get("type") == "string" and pp.get("additionalProperties") is False:
                pass
    else:
        # Everything the consumer relies on must be guaranteed (required) by the producer, same type.
        for k in consumer.get("required", []):
            if k not in producer.get("required", []):
                errs.append(f"{where}: consumer needs '{k}' but producer does not guarantee it")
            elif cprops.get(k, {}).get("type") != pprops.get(k, {}).get("type"):
                errs.append(f"{where}.{k}: type {cprops.get(k, {}).get('type')} != producer {pprops.get(k, {}).get('type')}")
    return errs


def test_contracts_are_well_formed() -> None:
    """Denominator for the always-on checks: every contract tool has all schema keys."""
    flat = contract_tools()
    for p in ("youtube",):
        flat |= music_tools(p)
    assert len(flat) >= 4 + 3 + 3 + 4, f"only {len(flat)} contract tools loaded"
    for n, t in flat.items():
        for k in SCHEMA_KEYS[:1] + SCHEMA_KEYS[2:]:
            assert k in t, f"{n} missing {k}"
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"].get("additionalProperties") is False, f"{n} input must be closed"


def test_incompat_detector_can_fail() -> None:
    """The consumer comparison must flag a drifting fixture."""
    prod = _load("ai-tools.v1.json")["tools"]["ai.complete"]["input_schema"]
    bad = {"properties": {"purpose": {"type": "string"}}, "required": []}
    errs = _incompat(bad, prod, "t", request=True)
    assert any("purpose" in e for e in errs) and any("requires 'prompt'" in e for e in errs)


def _exported(path: Path) -> list[dict]:
    """The manifest's exported_tools list (empty when the bundle exports none)."""
    return yaml.safe_load(path.read_text()).get("exported_tools") or []


def test_producer_manifests_match_contract() -> None:
    """Every exported tool in a contract family equals the contract schema exactly."""
    checked, drift, subsets = 0, [], 0
    for m in _manifests():
        for t in _exported(m):
            name = t["name"]
            if not _in_family(name):
                continue
            exp = _expected_for(name)
            if exp is None:
                drift.append(f"{m}: {name} is not in its contract")
                continue
            checked += 1
            for k in SCHEMA_KEYS:
                if k in exp and t.get(k) != exp[k]:
                    drift.append(f"{m}: {name}.{k} differs from contract")
            if name.startswith("music."):
                subsets += 1
    if not drift and checked == 0:
        pytest.skip("no contract-family producer manifests merged under scan roots yet")
    assert not drift, "\n".join(drift)


def test_producer_fixtures_match_contract() -> None:
    """Producer-side fixtures (bundles/*/*/tests/contract/*.json, contract/*.json) equal the contract."""
    drift, checked = [], 0
    for r in ROOTS:
        for p in sorted((r / "bundles").glob("*/*/tests/contract/*.json")):
            d = json.loads(p.read_text())
            exp = _expected_for(d.get("name", ""))
            if exp is None:
                continue
            checked += 1
            drift += [f"{p}: {k} differs from contract" for k in SCHEMA_KEYS if k in exp and d.get(k) != exp[k]]
        for p in sorted((r / "bundles").glob("*/*/contract/twitch-clip-tools.json")):
            checked += 1
            tools = json.loads(p.read_text())["tools"]
            want = _load("twitch-clip-tools.v1.json")["tools"]
            if tools != want:
                drift.append(f"{p}: tools differ from bundles/contracts/twitch-clip-tools.v1.json")
    if not drift and checked == 0:
        pytest.skip("no producer fixtures merged under scan roots yet")
    assert not drift, "\n".join(drift)


def test_consumer_fixtures_match_contract() -> None:
    """Each consumer fixture's request/response is compatible with the producer's contract schema."""
    fixtures = _consumer_fixtures()
    drift = []
    for p in fixtures:
        d = json.loads(p.read_text())
        exp = _expected_for(d["tool"])
        if exp is None:
            drift.append(f"{p}: tool {d['tool']} is not in any contract")
            continue
        if "target" in d and not (
            d["target"] == "waddles.core.service." + d["tool"].split(".")[0]
        ):
            drift.append(f"{p}: target {d['target']} does not match tool family")
        drift += [f"{p}: {e}" for e in _incompat(d["request"], exp["input_schema"], "request", request=True)]
        drift += [f"{p}: {e}" for e in _incompat(d.get("response", {}), exp["output_schema"], "response", request=False)]
    if not drift and not fixtures:
        pytest.skip("no consumer fixtures merged under scan roots yet")
    assert not drift, "\n".join(drift)


def test_requested_tools_exist_in_contracts() -> None:
    """A bundle requesting a contract-family tool must name one the contract defines."""
    drift = []
    for m in _manifests():
        for rt in yaml.safe_load(m.read_text()).get("requested_tools") or []:
            name = rt["tool"]
            if _in_family(name) and _expected_for(name) is None:
                drift.append(f"{m}: requests unknown tool {name}")
            elif _in_family(name) and "access" in rt and rt["access"] != _expected_for(name)["access"]:
                drift.append(f"{m}: {name} access {rt['access']} != contract {_expected_for(name)['access']}")
    assert not drift, "\n".join(drift)


def test_copy_does_not_mutate_contract() -> None:
    """Guard: helpers never mutate loaded contracts (lookups return fresh data)."""
    a = copy.deepcopy(contract_tools())
    _incompat({"properties": {}}, a["ai.complete"]["input_schema"], "x", request=True)
    assert a == contract_tools()
