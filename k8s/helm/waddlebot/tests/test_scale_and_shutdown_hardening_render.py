"""Helm-template assertions for fix/chart-scale-and-shutdown-hardening (release-audit ops HIGHs).

Pins the chart's availability/scale posture:

1. svc-streaming is a stateful singleton -> `strategy.type: Recreate`, replicas 1.
2. svc-presentation's overlay fan-out is in-process -> alpha renders exactly 1 replica, and the
   chart REFUSES >1 replica unless `pipeline.svcPresentation.sharedFanout.enabled` is set.
3. Every Deployment carries `terminationGracePeriodSeconds` + a `preStop` sleep; the grace
   period must exceed the sleep (render fails otherwise); older clusters get the exec fallback.
4. startupProbe on hub-api, the Rust data-plane stages, svc-streaming, svc-presentation.
5. HPA/PDB are real (not dead config): HPA renders for registered stateless services only when
   enabled, the Deployment then omits `spec.replicas`; PDB renders only for >1 effective
   replicas; singletons (svc-streaming, svc-presentation, stateful infra) never get either.

Every test reports/asserts the number of documents or workloads it examined -- zero matches is a
failure, never a silent pass (critical-rules.md Verification Integrity). A missing helm CLI is
a failure, not a skip.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_SINGLETONS = ("svc-streaming", "svc-presentation", "postgres", "redis", "seaweedfs")
_STARTUP_WORKLOADS = (
    "hub-api-v3",
    "svc-action-rust",
    "svc-ingest-rust",
    "svc-process-rust",
    "svc-streaming",
    "svc-presentation",
)


def _helm(values: str, extra_set: list[str], kube: str = "1.30.0") -> subprocess.CompletedProcess[str]:
    """Run `helm template` for one values file plus --set overrides; return the raw result."""
    args = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", kube,
        "--values", str(_CHART_DIR / values),
    ]
    for kv in extra_set:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell, test-only


def _render(values: str, extra_set: list[str] | None = None, kube: str = "1.30.0") -> list[dict[str, Any]]:
    """Render and parse the chart; a render failure or zero documents fails the test."""
    result = _helm(values, extra_set or [], kube)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _of_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == kind]


def _by_suffix(docs: list[dict[str, Any]], kind: str, suffix: str) -> dict[str, Any]:
    matches = [d for d in _of_kind(docs, kind) if d["metadata"]["name"].endswith(suffix)]
    assert len(matches) == 1, f"expected exactly 1 {kind} ending {suffix!r}, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.fail("helm CLI not available -- this test cannot be skipped silently")


@pytest.fixture(scope="module")
def alpha_docs() -> list[dict[str, Any]]:
    """Alpha render (the env that runs svc-streaming, svc-presentation and the Rust stages)."""
    return _render("values-alpha.yaml")


@pytest.fixture(scope="module")
def base_docs() -> list[dict[str, Any]]:
    """Base values.yaml render (alpha secret-generation tier so it renders offline)."""
    return _render("values.yaml", ["global.deploymentTier=alpha"])


class TestSingletons:
    """svc-streaming / svc-presentation are single-replica, never rolling-updated in parallel."""

    @pytest.mark.parametrize("fixture", ["alpha_docs", "base_docs"])
    def test_streaming_is_recreate_single_replica(self, fixture: str, request: pytest.FixtureRequest) -> None:
        dep = _by_suffix(request.getfixturevalue(fixture), "Deployment", "svc-streaming")
        assert dep["spec"]["strategy"] == {"type": "Recreate"}
        assert dep["spec"]["replicas"] == 1

    @pytest.mark.parametrize("fixture", ["alpha_docs", "base_docs"])
    def test_presentation_is_single_replica(self, fixture: str, request: pytest.FixtureRequest) -> None:
        dep = _by_suffix(request.getfixturevalue(fixture), "Deployment", "svc-presentation")
        assert dep["spec"]["replicas"] == 1

    def test_presentation_multi_replica_refused(self) -> None:
        result = _helm("values-alpha.yaml", ["pipeline.svcPresentation.replicas=2"])
        assert result.returncode != 0, "replicas=2 must fail the render (in-process fan-out)"
        assert "sharedFanout" in result.stderr + result.stdout

    @pytest.mark.parametrize("rust", ["true", "false"])
    def test_presentation_multi_replica_needs_shared_fanout(self, rust: str) -> None:
        """Both the Rust and Python svc-presentation templates carry the guard."""
        refused = _helm("values-alpha.yaml", ["pipeline.svcPresentation.replicas=2", f"presentation.rust.enabled={rust}"])
        assert refused.returncode != 0
        ok = _helm(
            "values-alpha.yaml",
            ["pipeline.svcPresentation.replicas=2", "pipeline.svcPresentation.sharedFanout.enabled=true",
             f"presentation.rust.enabled={rust}"],
        )
        assert ok.returncode == 0, ok.stderr


class TestGracefulShutdown:
    """Every Deployment drains before SIGKILL."""

    @pytest.mark.parametrize("fixture", ["alpha_docs", "base_docs"])
    def test_grace_and_prestop_on_every_deployment(self, fixture: str, request: pytest.FixtureRequest) -> None:
        deployments = _of_kind(request.getfixturevalue(fixture), "Deployment")
        assert len(deployments) >= 14, f"examined only {len(deployments)} Deployments"
        for dep in deployments:
            pod = dep["spec"]["template"]["spec"]
            name = dep["metadata"]["name"]
            assert pod.get("terminationGracePeriodSeconds", 0) > 5, f"{name}: no terminationGracePeriodSeconds"
            for c in pod["containers"]:
                assert c["lifecycle"]["preStop"]["sleep"]["seconds"] == 5, f"{name}/{c['name']}: no preStop sleep"

    def test_streaming_has_longer_grace(self, alpha_docs: list[dict[str, Any]]) -> None:
        dep = _by_suffix(alpha_docs, "Deployment", "svc-streaming")
        assert dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 60

    def test_old_cluster_falls_back_to_exec_sleep(self) -> None:
        deployments = _of_kind(_render("values-alpha.yaml", kube="1.28.0"), "Deployment")
        assert deployments
        for dep in deployments:
            pre = dep["spec"]["template"]["spec"]["containers"][0]["lifecycle"]["preStop"]
            assert "sleep" not in pre
            assert pre["exec"]["command"] == ["sleep", "5"]

    def test_prestop_zero_disables_hook(self) -> None:
        deployments = _of_kind(_render("values-alpha.yaml", ["shutdown.preStopSleepSeconds=0"]), "Deployment")
        assert deployments
        assert all("lifecycle" not in c for d in deployments for c in d["spec"]["template"]["spec"]["containers"])

    def test_grace_must_exceed_prestop(self) -> None:
        result = _helm("values-alpha.yaml", ["shutdown.terminationGracePeriodSeconds=5"])
        assert result.returncode != 0
        assert "must exceed" in result.stderr + result.stdout


class TestStartupProbe:
    """Slow-start workloads carry a startupProbe; existing liveness/readiness are kept."""

    def test_alpha_slow_start_workloads(self, alpha_docs: list[dict[str, Any]]) -> None:
        for suffix in _STARTUP_WORKLOADS:
            c = _by_suffix(alpha_docs, "Deployment", suffix)["spec"]["template"]["spec"]["containers"][0]
            assert c["startupProbe"]["failureThreshold"] == 30, suffix
            assert "livenessProbe" in c and "readinessProbe" in c, suffix


class TestAutoscalingAndPdb:
    """HPA/PDB are consumed config, gated per service, never on singletons."""

    def test_off_by_default_in_alpha(self, alpha_docs: list[dict[str, Any]]) -> None:
        assert not _of_kind(alpha_docs, "HorizontalPodAutoscaler")
        assert not _of_kind(alpha_docs, "PodDisruptionBudget")  # all single-replica

    def test_hpa_and_pdb_render_for_scalable_service(self) -> None:
        docs = _render("values-alpha.yaml", ["autoscaling.services.hubApi.enabled=true"])
        hpa = _by_suffix(docs, "HorizontalPodAutoscaler", "hub-api-v3")
        assert hpa["spec"]["scaleTargetRef"]["name"] == hpa["metadata"]["name"]
        assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 10)
        assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 70
        pdb = _by_suffix(docs, "PodDisruptionBudget", "hub-api-v3")
        assert pdb["spec"]["minAvailable"] == 1
        assert pdb["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "hub-api"
        # HPA owns the replica count -> Deployment must not pin it.
        assert "replicas" not in _by_suffix(docs, "Deployment", "hub-api-v3")["spec"]
        # only the opted-in service
        assert len(_of_kind(docs, "HorizontalPodAutoscaler")) == 1

    def test_pdb_not_rendered_for_single_replica(self) -> None:
        docs = _render("values-alpha.yaml", ["autoscaling.services.hubApi.enabled=true",
                                             "autoscaling.defaults.minReplicas=1"])
        assert _of_kind(docs, "HorizontalPodAutoscaler")
        assert not _of_kind(docs, "PodDisruptionBudget")

    def test_pdb_disable_flag(self) -> None:
        docs = _render("values-alpha.yaml", ["autoscaling.services.hubApi.enabled=true",
                                             "podDisruptionBudget.enabled=false"])
        assert _of_kind(docs, "HorizontalPodAutoscaler")
        assert not _of_kind(docs, "PodDisruptionBudget")

    def test_singletons_never_autoscaled_even_with_everything_on(self) -> None:
        docs = _render("values-alpha.yaml", ["autoscaling.defaults.enabled=true"])
        hpas = _of_kind(docs, "HorizontalPodAutoscaler")
        pdbs = _of_kind(docs, "PodDisruptionBudget")
        assert len(hpas) >= 5, f"expected the Rust stages + hub-api HPAs, got {len(hpas)}"
        for doc in hpas + pdbs:
            name = doc["metadata"]["name"]
            assert not any(name.endswith(s) for s in _SINGLETONS), f"{doc['kind']} on singleton {name}"
        assert not any(d["metadata"]["name"].endswith("svc-ingest-rust") for d in hpas)  # lease holder: no HPA
        # the Rust data plane is covered
        for suffix in ("svc-action-rust", "svc-process-rust", "bundle-executor"):
            _by_suffix(docs, "HorizontalPodAutoscaler", suffix)

    def test_production_values_enable_hpa(self) -> None:
        docs = _render("values-production.yaml", ["global.deploymentTier=alpha"])
        names = {d["metadata"]["name"] for d in _of_kind(docs, "HorizontalPodAutoscaler")}
        assert any(n.endswith("hub-api-v3") for n in names), names
        pdbs = _of_kind(docs, "PodDisruptionBudget")
        assert pdbs, "production must render PodDisruptionBudgets"
        assert not any(d["metadata"]["name"].endswith(_SINGLETONS) for d in pdbs)

    def test_hpa_needs_a_metric(self) -> None:
        result = _helm("values-alpha.yaml", ["autoscaling.services.hubApi.enabled=true",
                                             "autoscaling.defaults.targetCPUUtilizationPercentage="])
        assert result.returncode != 0
