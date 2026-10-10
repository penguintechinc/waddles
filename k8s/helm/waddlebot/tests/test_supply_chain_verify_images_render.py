"""Helm-template assertions for the supply-chain Kyverno verifyImages policy.

Pins the admission side of docs/SUPPLY_CHAIN.md:

1. Off by default -- the policy never renders unless supplyChain.verifyImages.enabled.
2. Enabled outside production renders Audit, with digest mutation off (Kyverno rejects
   mutateDigest=true under Audit, which broke the first render).
3. Production values render Enforce with digest mutation on, for both the signature rule
   and the SPDX SBOM attestation rule.
4. The keyless subjectRegExp admits this repo's release workflow and rejects forks, other
   workflows and pull-request refs (real identity strings, not synthetic ones).
5. The chart regex equals the default identity regex in scripts/ci/supply-chain.sh, so the
   signer and the verifier can never drift apart silently.

Every test reports how many items it examined; zero rendered documents is a failure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SIGN_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "supply-chain.sh"
_POLICY_NAME = "waddles-verify-image-signatures"

# Real identities the keyless verifier must see, and look-alikes it must refuse.
_ADMITTED = [
    "https://github.com/penguintechinc/waddles/.github/workflows/containers.yml@refs/heads/release/v3.0.X",
    "https://github.com/penguintechinc/waddles/.github/workflows/containers.yml@refs/heads/main",
    "https://github.com/penguintechinc/waddles/.github/workflows/containers.yml@refs/tags/v3.0.1",
]
_REFUSED = [
    "https://github.com/attacker/waddles/.github/workflows/containers.yml@refs/heads/main",
    "https://github.com/penguintechinc/waddles/.github/workflows/security.yml@refs/heads/release/v3.0.X",
    "https://github.com/penguintechinc/waddles/.github/workflows/containers.yml@refs/pull/123/merge",
    "https://github.com/penguintechinc/waddles-fork/.github/workflows/containers.yml@refs/heads/main",
]


def _require_helm() -> None:
    """Fail loudly when helm is absent -- a silently skipped admission test is not a pass."""
    if not shutil.which("helm"):
        pytest.fail("helm CLI is required for supply-chain render tests (install helm v3+/v4)")


def _helm_template(values_file: str | None, extra_set: list[str]) -> list[dict[str, Any]]:
    """Render the chart with an optional values file plus --set overrides; return parsed docs."""
    _require_helm()
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    if values_file is not None:
        args += ["--values", str(_CHART_DIR / values_file)]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell, test-only
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _policies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every rendered ClusterPolicy with the supply-chain policy name."""
    return [d for d in docs if d.get("kind") == "ClusterPolicy" and d.get("metadata", {}).get("name") == _POLICY_NAME]


def _verify_entries(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every verifyImages entry across the policy's rules (both signature and SBOM)."""
    entries: list[dict[str, Any]] = []
    for rule in policy["spec"]["rules"]:
        entries.extend(rule.get("verifyImages", []))
    return entries


def _subject_regexp(policy: dict[str, Any]) -> str:
    """Return the keyless subjectRegExp that the rendered policy enforces."""
    entry = _verify_entries(policy)[0]["attestors"][0]["entries"][0]["keyless"]
    return str(entry["subjectRegExp"])


@pytest.fixture(scope="module")
def production_enforce_docs() -> list[dict[str, Any]]:
    """Render production values with the existing render-test tier override (seaweedfs gate)."""
    return _helm_template("values-production.yaml", ["global.deploymentTier=alpha"])


class TestSupplyChainVerifyImagesRender:
    """Render-level assertions for the Kyverno verifyImages ClusterPolicy."""

    def test_disabled_by_default_renders_no_policy(self) -> None:
        docs = _helm_template("values-alpha.yaml", [])
        policies = _policies(docs)
        print(f"examined {len(docs)} rendered documents; supply-chain policies: {len(policies)}")
        assert len(policies) == 0, "verifyImages must be off unless explicitly enabled"

    def test_enabled_outside_production_is_audit_without_digest_mutation(self) -> None:
        docs = _helm_template("values-alpha.yaml", ["supplyChain.verifyImages.enabled=true"])
        policies = _policies(docs)
        assert len(policies) == 1, f"expected exactly one policy, got {len(policies)}"
        policy = policies[0]
        entries = _verify_entries(policy)
        print(f"examined {len(policy['spec']['rules'])} rules, {len(entries)} verifyImages entries")
        assert policy["spec"]["validationFailureAction"] == "Audit"
        assert len(policy["spec"]["rules"]) == 2, "signature rule + SBOM attestation rule"
        assert entries, "no verifyImages entries rendered"
        for entry in entries:
            assert entry["mutateDigest"] is False, "Kyverno rejects mutateDigest=true under Audit"
            assert entry["verifyDigest"] is True
            assert entry["required"] is True

    def test_production_enforces_with_digest_mutation(self, production_enforce_docs: list[dict[str, Any]]) -> None:
        policies = _policies(production_enforce_docs)
        assert len(policies) == 1, f"expected exactly one production policy, got {len(policies)}"
        policy = policies[0]
        entries = _verify_entries(policy)
        print(f"examined {len(policy['spec']['rules'])} rules, {len(entries)} verifyImages entries")
        assert policy["spec"]["validationFailureAction"] == "Enforce"
        assert entries, "no verifyImages entries rendered"
        for entry in entries:
            assert entry["mutateDigest"] is True
            assert entry["verifyDigest"] is True
            assert entry["required"] is True

    def test_sbom_rule_requires_spdx_attestation(self) -> None:
        docs = _helm_template("values-alpha.yaml", ["supplyChain.verifyImages.enabled=true"])
        policy = _policies(docs)[0]
        sbom_rule = next(r for r in policy["spec"]["rules"] if r["name"] == "verify-spdx-sbom-attestation")
        attestations = sbom_rule["verifyImages"][0]["attestations"]
        print(f"examined {len(attestations)} attestation requirements")
        assert [a["type"] for a in attestations] == ["https://spdx.dev/Document"]

    def test_identity_regexp_admits_release_and_refuses_lookalikes(self) -> None:
        docs = _helm_template("values-alpha.yaml", ["supplyChain.verifyImages.enabled=true"])
        pattern = re.compile(_subject_regexp(_policies(docs)[0]))
        admitted = sum(1 for ident in _ADMITTED if pattern.search(ident))
        refused = sum(1 for ident in _REFUSED if not pattern.search(ident))
        print(f"identities admitted {admitted}/{len(_ADMITTED)}; look-alikes refused {refused}/{len(_REFUSED)}")
        assert admitted == len(_ADMITTED), "a real release identity was refused"
        assert refused == len(_REFUSED), "a look-alike identity was admitted"

    def test_chart_identity_matches_signing_script_default(self) -> None:
        docs = _helm_template("values-alpha.yaml", ["supplyChain.verifyImages.enabled=true"])
        chart_re = _subject_regexp(_policies(docs)[0])
        script = _SIGN_SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'IDENTITY_RE="\$\{SUPPLY_CHAIN_IDENTITY_REGEXP:-(.+?)\}"', script)
        assert match, f"could not locate IDENTITY_RE default in {_SIGN_SCRIPT}"
        script_re = match.group(1).replace("\\\\", "\\")
        print(f"chart regexp == script regexp: {chart_re == script_re}")
        assert chart_re == script_re, "signer identity and admission identity have drifted"
