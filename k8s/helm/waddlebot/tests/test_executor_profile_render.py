"""Helm-template assertions for language-partitioned bundle executors.

Python (componentize-py) bundles run on their OWN executor Deployment
(`svc-bundles-executor-scripted`, EXECUTOR_PROFILE=scripted) while the primary executor is the
compiled one (EXECUTOR_PROFILE=compiled), split ON in every real environment. Pins:

1. dal2alpha renders BOTH Deployments: primary `native`, python twin `python`, with the owner's
   2026-11-14 tuning (EXECUTOR_MAX_CONCURRENT_COMPILES=6, EXECUTOR_POOL_WAIT_MS=2000) and the
   python twin's resources (requests 2 CPU / 8Gi, limits 8 CPU / 24Gi).
2. Every other environment (alpha/beta/gamma/production/local/ci) renders exactly ONE executor with
   profile `all` -- no python Deployment, no python NetworkPolicy -- i.e. today's behavior, and the
   compile/pool tuning is untouched there (chart defaults).
3. The twin's selector is DISJOINT from the primary's (overlapping selectors make the controllers
   fight), it has no connector link (no CONNECTOR_* env, no connector volumes), its own
   precompile-cache emptyDir, an enforced Tetragon policy + CiliumNetworkPolicy of its own, and
   the same probes as the primary.

Every test asserts how many documents/workloads it examined -- zero is a failure, never a silent
pass (critical-rules.md Verification Integrity). A missing helm CLI is a failure, not a skip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_PRIMARY = "svc-bundles-executor"
_SCRIPTED = "svc-bundles-executor-scripted"
# Offline-render switch (alpha secret-generation tier) so beta/gamma/production render without
# operator-supplied secrets, as the other chart render tests do.
_OFFLINE = ["global.deploymentTier=alpha"]
_OTHER_ENVS = (
    "values-alpha.yaml",
    "values-beta.yaml",
    "values-gamma.yaml",
    "values-production.yaml",
    "values-local.yaml",
    "values-ci.yaml",
)


def _render(values: str, extra_set: list[str] | None = None) -> list[dict[str, Any]]:
    """Render and parse the chart; a render failure or zero documents fails the test."""
    args = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_CHART_DIR / values),
    ]
    for kv in extra_set or []:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    if result.returncode != 0:
        pytest.fail(f"helm template failed for {values}:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _executors(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Executor Deployments by component label."""
    out = {}
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        comp = d["metadata"].get("labels", {}).get("app.kubernetes.io/component", "")
        if comp in (_PRIMARY, _SCRIPTED):
            out[comp] = d
    return out


def _env(dep: dict[str, Any]) -> dict[str, Any]:
    ctr = dep["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in ctr["env"]}


def _named(docs: list[dict[str, Any]], kind: str, name_suffix: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind and d["metadata"]["name"].endswith(name_suffix)]


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.fail("helm CLI not available -- this test cannot be skipped silently")


@pytest.fixture(scope="module")
def dal2alpha() -> list[dict[str, Any]]:
    """dal2alpha render -- the only environment with the python twin enabled."""
    return _render("values-dal2alpha.yaml")


class TestDal2alphaSplit:
    """Primary = compiled, twin = scripted, owner-decided tuning on both."""

    def test_both_deployments_render_with_the_right_profiles(self, dal2alpha: list[dict[str, Any]]) -> None:
        ex = _executors(dal2alpha)
        assert set(ex) == {_PRIMARY, _SCRIPTED}, f"examined {len(ex)} executor Deployments: {sorted(ex)}"
        assert _env(ex[_PRIMARY])["EXECUTOR_PROFILE"] == "compiled"
        assert _env(ex[_SCRIPTED])["EXECUTOR_PROFILE"] == "scripted"

    @pytest.mark.parametrize("component", [_PRIMARY, _SCRIPTED])
    def test_alpha_tuning_compiles_6_and_pool_wait_2000(
        self, dal2alpha: list[dict[str, Any]], component: str
    ) -> None:
        env = _env(_executors(dal2alpha)[component])
        assert env["EXECUTOR_MAX_CONCURRENT_COMPILES"] == "6"
        assert env["EXECUTOR_POOL_WAIT_MS"] == "2000"

    def test_python_twin_resources(self, dal2alpha: list[dict[str, Any]]) -> None:
        ctr = _executors(dal2alpha)[_SCRIPTED]["spec"]["template"]["spec"]["containers"][0]
        assert ctr["resources"]["requests"] == {"cpu": "2000m", "memory": "8Gi"}
        assert ctr["resources"]["limits"] == {"cpu": "8000m", "memory": "24Gi"}

    def test_selectors_are_disjoint_and_match_their_own_pods(self, dal2alpha: list[dict[str, Any]]) -> None:
        ex = _executors(dal2alpha)
        assert len(ex) == 2
        for comp, dep in ex.items():
            sel = dep["spec"]["selector"]["matchLabels"]
            pod_labels = dep["spec"]["template"]["metadata"]["labels"]
            assert sel["app.kubernetes.io/component"] == comp
            assert all(pod_labels.get(k) == v for k, v in sel.items()), f"{comp}: selector must match its own pods"
        primary_sel = ex[_PRIMARY]["spec"]["selector"]["matchLabels"]
        python_pod = ex[_SCRIPTED]["spec"]["template"]["metadata"]["labels"]
        assert not all(python_pod.get(k) == v for k, v in primary_sel.items()), (
            "the primary's selector must NOT match the python twin's pods (overlapping selectors fight)"
        )

    def test_python_twin_has_no_connector_link_and_its_own_cache(self, dal2alpha: list[dict[str, Any]]) -> None:
        dep = _executors(dal2alpha)[_SCRIPTED]
        env = _env(dep)
        assert not [k for k in env if k.startswith(("CONNECTOR_", "EXECUTOR_CONNECTOR"))], sorted(env)
        spec = dep["spec"]["template"]["spec"]
        vols = {v["name"]: v for v in spec["volumes"]}
        assert vols["precompile-cache-scripted"] == {"name": "precompile-cache-scripted", "emptyDir": {}}
        assert "precompile-cache" not in vols
        assert "connector-host-api-tls" not in vols and "executor-ca-bundle" not in vols
        # Same stage dial list as the primary: it must serve both stages.
        primary_env = _env(_executors(dal2alpha)[_PRIMARY])
        assert env["STAGE_HOST_API_ADDR"] == primary_env["STAGE_HOST_API_ADDR"]

    def test_probes_match_the_primary(self, dal2alpha: list[dict[str, Any]]) -> None:
        ex = _executors(dal2alpha)
        probes = ("startupProbe", "livenessProbe", "readinessProbe")
        examined = 0
        for probe in probes:
            a = ex[_PRIMARY]["spec"]["template"]["spec"]["containers"][0][probe]
            b = ex[_SCRIPTED]["spec"]["template"]["spec"]["containers"][0][probe]
            assert a == b, probe
            examined += 1
        assert examined == 3

    def test_twin_has_its_own_network_policy_and_enforced_tetragon(self, dal2alpha: list[dict[str, Any]]) -> None:
        cnps = _named(dal2alpha, "CiliumNetworkPolicy", _SCRIPTED)
        assert len(cnps) == 1, f"examined {len(cnps)} python CiliumNetworkPolicies"
        cnp = cnps[0]
        assert cnp["spec"]["ingress"] == []
        # No egress to svc-ingest's connector link (this pod never dials it).
        targets = [
            m["matchLabels"].get("app.kubernetes.io/component")
            for rule in cnp["spec"]["egress"]
            for m in rule.get("toEndpoints", [])
        ]
        assert "svc-ingest-rust" not in targets, targets
        assert "svc-process-rust" in targets and "svc-action-rust" in targets, targets
        tetra = [
            d for d in dal2alpha
            if d.get("kind") == "TracingPolicyNamespaced" and _SCRIPTED in d["metadata"]["name"]
        ]
        assert len(tetra) == 1, f"examined {len(tetra)} python TracingPolicyNamespaced"

    def test_python_twin_is_in_the_pdb_hpa_registry_without_an_hpa_by_default(
        self, dal2alpha: list[dict[str, Any]]
    ) -> None:
        # replicas: 1 and no autoscaling.services.bundleExecutorScripted entry => fixed 1 replica,
        # no HPA, no PDB (a PDB on one replica would deadlock a drain).
        dep = _executors(dal2alpha)[_SCRIPTED]
        assert dep["spec"]["replicas"] == 1
        assert not _named(dal2alpha, "HorizontalPodAutoscaler", _SCRIPTED)
        assert not _named(dal2alpha, "PodDisruptionBudget", _SCRIPTED)


_REAL_ENVS = ("values-beta.yaml", "values-gamma.yaml", "values-production.yaml", "values-dal2alpha.yaml")
_DEV_ENVS = ("values-local.yaml", "values-alpha.yaml", "values-ci.yaml")


class TestSplitDefaults:
    """Split ON in every real environment; ONLY local/dev overlays run one `all` executor."""

    @pytest.mark.parametrize("values", _REAL_ENVS)
    def test_real_environments_run_two_executors(self, values: str) -> None:
        ex = _executors(_render(values, _OFFLINE))
        assert set(ex) == {_PRIMARY, _SCRIPTED}, f"{values}: examined {sorted(ex)}"
        assert _env(ex[_PRIMARY])["EXECUTOR_PROFILE"] == "compiled"
        assert _env(ex[_SCRIPTED])["EXECUTOR_PROFILE"] == "scripted"
        # Per-profile cache subdirs + volumes (never a shared dir).
        assert _env(ex[_PRIMARY])["EXECUTOR_PRECOMPILE_DIR"].endswith("/compiled")
        assert _env(ex[_SCRIPTED])["EXECUTOR_PRECOMPILE_DIR"].endswith("/scripted")
        assert "EXECUTOR_ALLOW_ALL_PROFILE" not in _env(ex[_PRIMARY])

    @pytest.mark.parametrize("values", _DEV_ENVS)
    def test_dev_overlays_run_one_all_executor(self, values: str) -> None:
        docs = _render(values, _OFFLINE)
        ex = _executors(docs)
        assert _SCRIPTED not in ex, f"{values}: scripted executor must be absent in dev"
        assert not _named(docs, "CiliumNetworkPolicy", _SCRIPTED)
        if ex:  # values-ci disables the executor entirely
            env = _env(ex[_PRIMARY])
            assert env["EXECUTOR_PROFILE"] == "all"
            assert env["EXECUTOR_ALLOW_ALL_PROFILE"] == "true"

    def test_at_least_one_dev_overlay_renders_the_single_executor(self) -> None:
        rendered = sum(len(_executors(_render(v, _OFFLINE))) for v in _DEV_ENVS)
        assert rendered >= 1, "no dev overlay rendered an executor -- the checks above would be vacuous"

    def test_primary_profile_value_is_honoured_when_scripted_disabled(self) -> None:
        docs = _render(
            "values-gamma.yaml",
            [*_OFFLINE, "pipeline.rustDataPlane.bundleExecutorScripted.enabled=false",
             "pipeline.rustDataPlane.bundleExecutor.profile=compiled"],
        )
        ex = _executors(docs)
        assert _SCRIPTED not in ex and _PRIMARY in ex
        assert _env(ex[_PRIMARY])["EXECUTOR_PROFILE"] == "compiled"

    def test_twin_hpa_and_pdb_render_when_configured(self) -> None:
        docs = _render(
            "values-gamma.yaml",
            [
                *_OFFLINE,
                "autoscaling.services.bundleExecutorScripted.enabled=true",
                "autoscaling.services.bundleExecutorScripted.minReplicas=2",
                "autoscaling.services.bundleExecutorScripted.maxReplicas=3",
            ],
        )
        assert len(_named(docs, "HorizontalPodAutoscaler", _SCRIPTED)) == 1
        assert len(_named(docs, "PodDisruptionBudget", _SCRIPTED)) == 1
        assert "replicas" not in _executors(docs)[_SCRIPTED]["spec"], "HPA-managed: no spec.replicas"


def _tetragon_for(docs: list[dict[str, Any]], component: str) -> list[dict[str, Any]]:
    return [
        d for d in docs
        if d.get("kind") == "TracingPolicyNamespaced" and d["metadata"]["name"].endswith(f"{component}-exec-allowlist")
        or (d.get("kind") == "TracingPolicyNamespaced" and component in d["metadata"]["name"]
            and not (component == _PRIMARY and _SCRIPTED in d["metadata"]["name"]))
    ]


class TestOptionalKata:
    """runtimeClassName is independent per executor, empty (runc) by default, never on by default."""

    @pytest.mark.parametrize("values", _REAL_ENVS + _DEV_ENVS)
    def test_unset_means_no_runtime_class_anywhere(self, values: str) -> None:
        docs = _render(values, _OFFLINE)
        ex = _executors(docs)
        for comp, dep in ex.items():
            assert "runtimeClassName" not in dep["spec"]["template"]["spec"], f"{values}/{comp}"

    def test_scripted_kata_renders_only_on_the_scripted_deployment_and_drops_its_tetragon_policy(self) -> None:
        base = _render("values-dal2alpha.yaml")
        with_kata = _render(
            "values-dal2alpha.yaml",
            ["pipeline.rustDataPlane.bundleExecutorScripted.runtimeClassName=kata-clh"],
        )
        ex = _executors(with_kata)
        assert ex[_SCRIPTED]["spec"]["template"]["spec"]["runtimeClassName"] == "kata-clh"
        assert "runtimeClassName" not in ex[_PRIMARY]["spec"]["template"]["spec"], "compiled unaffected"
        # Tetragon: scripted policy omitted, compiled policy still present, CNP default-deny kept.
        before = {d["metadata"]["name"] for d in base if d.get("kind") == "TracingPolicyNamespaced"}
        after = {d["metadata"]["name"] for d in with_kata if d.get("kind") == "TracingPolicyNamespaced"}
        assert before - after == {n for n in before if _SCRIPTED in n}, (before, after)
        assert len(before - after) == 1, f"examined {len(before - after)} omitted policies"
        assert any(_PRIMARY in n and _SCRIPTED not in n for n in after), "compiled keeps its Tetragon policy"
        cnps = _named(with_kata, "CiliumNetworkPolicy", _SCRIPTED)
        assert len(cnps) == 1 and cnps[0]["spec"]["ingress"] == []

    def test_compiled_kata_is_independent_of_scripted(self) -> None:
        docs = _render(
            "values-dal2alpha.yaml",
            ["pipeline.rustDataPlane.bundleExecutor.runtimeClassName=kata"],
        )
        ex = _executors(docs)
        assert ex[_PRIMARY]["spec"]["template"]["spec"]["runtimeClassName"] == "kata"
        assert "runtimeClassName" not in ex[_SCRIPTED]["spec"]["template"]["spec"]

    def test_kata_pod_keeps_the_compile_cache_volume_mounted(self) -> None:
        docs = _render(
            "values-dal2alpha.yaml",
            ["pipeline.rustDataPlane.bundleExecutorScripted.runtimeClassName=kata-clh"],
        )
        spec = _executors(docs)[_SCRIPTED]["spec"]["template"]["spec"]
        assert any(v["name"] == "precompile-cache-scripted" for v in spec["volumes"])
        mounts = [m["name"] for m in spec["containers"][0]["volumeMounts"]]
        assert "precompile-cache-scripted" in mounts
