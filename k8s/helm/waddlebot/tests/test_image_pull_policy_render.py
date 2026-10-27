"""Helm-template assertions for fix/image-pull-policy-local.

# regression: hardcoded `imagePullPolicy: Always` on hub-api + the Rust data-plane
# Deployments hung the #480 kind upgrade-path job (and would break a local alpha deploy)

hub-api, svc-{action,ingest,process}-rust, svc-presentation and both bundle-executor
Deployments used to hardcode `imagePullPolicy: Always`. Every deploy now carries a unique
per-deploy `global.imageTag`, so `Always` buys nothing -- and it is fatal for
locally-loaded images (kind e2e, alpha local builds): the node already has the image,
`Always` forces a registry pull that fails, and the pod never becomes Ready.

These tests pin the fix:

1. Under every environment's values file the named workloads render `IfNotPresent`
   (the `global.imagePullPolicy` default) -- never `Always`.
2. The policy is genuinely values-driven: `--set global.imagePullPolicy=Never` (a value
   no template hardcodes) reaches every one of them -- a re-hardcoded literal, `Always`
   or otherwise, fails this.
3. An empty `global.imagePullPolicy` still falls back to `IfNotPresent`, never to an
   empty/invalid field.

Zero rendered documents or zero matching containers is a hard failure here, never a
silent pass (critical-rules.md Verification Integrity) -- each assertion reports the
number of containers it examined.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

# (rendered Deployment metadata.name suffix, container name) -- the seven sites that used
# to hardcode `Always`. Matched by suffix so the release-name prefix never matters.
_FORMERLY_HARDCODED = {
    ("hub-api-v3", "hub-api"),
    ("svc-process-rust", "svc-process-rust"),
    ("svc-ingest-rust", "svc-ingest-rust"),
    ("svc-action-rust", "svc-action-rust"),
    ("svc-presentation", "svc-presentation"),
    ("bundle-executor", "bundle-executor"),
    ("bundle-executor-action", "bundle-executor"),
}

# Env values files. beta/gamma/production pin a deploymentTier that needs real secrets an
# offline `helm template` cannot supply, so (like .github/workflows/pr-validation.yml's
# beta-topology steps) they render under the alpha secret-generation tier -- the same
# topology and, crucially, the same image-pull-policy resolution.
_ENV_VALUES = {
    "alpha": ("values-alpha.yaml", []),
    "beta": ("values-beta.yaml", ["global.deploymentTier=alpha"]),
    "gamma": ("values-gamma.yaml", ["global.deploymentTier=alpha"]),
    "production": ("values-production.yaml", ["global.deploymentTier=alpha"]),
}


def _helm_template(values_file: str, extra_set: list[str]) -> list[dict[str, Any]]:
    """Render the chart for one env values file and return the parsed documents."""
    args = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_CHART_DIR / values_file),
    ]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell, test-only
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the pod spec of a workload doc (handles CronJob's nested jobTemplate)."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    return spec["template"]["spec"]


def _containers(docs: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """Return (workload name, container name, container) for every main + init container."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for doc in docs:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        pod = _pod_spec(doc)
        for key in ("containers", "initContainers"):
            for container in pod.get(key) or []:
                found.append((doc["metadata"]["name"], container["name"], container))
    return found


def _formerly_hardcoded(
    docs: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Map each formerly-hardcoded (suffix, container) site that rendered to its container."""
    matched: dict[tuple[str, str], dict[str, Any]] = {}
    for workload, name, container in _containers(docs):
        for suffix, cname in _FORMERLY_HARDCODED:
            if name == cname and workload.endswith(suffix):
                # bundle-executor-action also ends with "-action"; exact-suffix match above
                # keeps "bundle-executor" and "bundle-executor-action" distinct because the
                # shorter suffix never ends the longer workload name.
                matched[(suffix, cname)] = container
    return matched


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


@pytest.fixture(scope="module")
def alpha_docs() -> list[dict[str, Any]]:
    """Alpha render -- the only env where every formerly-hardcoded workload is enabled."""
    values, extra = _ENV_VALUES["alpha"]
    return _helm_template(values, extra)


class TestFormerlyHardcodedWorkloads:
    """The seven formerly-`Always` containers resolve their policy from values."""

    def test_all_seven_sites_render_in_alpha(self, alpha_docs: list[dict[str, Any]]) -> None:
        """Denominator guard: all seven sites must be present, or the next tests prove nothing."""
        matched = _formerly_hardcoded(alpha_docs)
        assert set(matched) == _FORMERLY_HARDCODED, (
            f"examined {len(matched)}/{len(_FORMERLY_HARDCODED)} sites; "
            f"missing {sorted(_FORMERLY_HARDCODED - set(matched))}"
        )

    def test_alpha_default_is_if_not_present(self, alpha_docs: list[dict[str, Any]]) -> None:
        matched = _formerly_hardcoded(alpha_docs)
        assert len(matched) == len(_FORMERLY_HARDCODED), "not all sites rendered"
        bad = {k: c.get("imagePullPolicy") for k, c in matched.items() if c.get("imagePullPolicy") != "IfNotPresent"}
        assert not bad, f"expected IfNotPresent on all {len(matched)} sites, got: {bad}"

    def test_policy_is_values_driven_not_hardcoded(self) -> None:
        """`--set global.imagePullPolicy=Never` must reach all seven (no re-hardcoded literal)."""
        values, extra = _ENV_VALUES["alpha"]
        docs = _helm_template(values, [*extra, "global.imagePullPolicy=Never"])
        matched = _formerly_hardcoded(docs)
        assert len(matched) == len(_FORMERLY_HARDCODED), "not all sites rendered"
        bad = {k: c.get("imagePullPolicy") for k, c in matched.items() if c.get("imagePullPolicy") != "Never"}
        assert not bad, f"global.imagePullPolicy=Never did not reach {len(matched)} sites, stuck: {bad}"

    def test_empty_policy_falls_back_to_if_not_present(self) -> None:
        """An empty global.imagePullPolicy must not render an empty/invalid field."""
        values, extra = _ENV_VALUES["alpha"]
        docs = _helm_template(values, [*extra, "global.imagePullPolicy="])
        matched = _formerly_hardcoded(docs)
        assert len(matched) == len(_FORMERLY_HARDCODED), "not all sites rendered"
        bad = {k: c.get("imagePullPolicy") for k, c in matched.items() if c.get("imagePullPolicy") != "IfNotPresent"}
        assert not bad, f"empty policy did not fall back to IfNotPresent on {len(matched)} sites: {bad}"


class TestEveryEnvironment:
    """No container in any env's render forces `Always`; hub-api resolves IfNotPresent."""

    @pytest.mark.parametrize("env", sorted(_ENV_VALUES))
    def test_no_always_and_hub_api_if_not_present(self, env: str) -> None:
        values, extra = _ENV_VALUES[env]
        docs = _helm_template(values, extra)
        containers = _containers(docs)
        assert containers, f"{env}: zero containers examined -- cannot be a pass"

        always = [(w, n) for w, n, c in containers if c.get("imagePullPolicy") == "Always"]
        assert not always, f"{env}: {len(always)}/{len(containers)} containers force Always: {always}"

        hub_api = [c for w, n, c in containers if w.endswith("hub-api-v3") and n == "hub-api"]
        assert len(hub_api) == 1, f"{env}: expected exactly 1 hub-api container, found {len(hub_api)}"
        assert hub_api[0].get("imagePullPolicy") == "IfNotPresent", (
            f"{env}: hub-api imagePullPolicy={hub_api[0].get('imagePullPolicy')!r}"
        )
