"""Helm-template assertions for the shared OTLP wiring (`global.otel`, templates/_otel.tpl).

Sending logs/metrics/traces to the in-cluster SigNoz collector is ONE values block rendered
through ONE helper into every emitting workload, plus one egress rule in the policies that
already default-deny those workloads:

1. Disabled (the base default) => no OTEL_* / K8S_POD_NAME env anywhere (the one pre-existing
   exception, svc-presentation's historical OTEL_SERVICE_NAME + OTEL_RESOURCE_ATTRIBUTES, is pinned
   to its exact legacy values) and no collector egress rule in any CiliumNetworkPolicy.
2. Enabled => every emitting workload container carries EXACTLY ONE OTEL_SERVICE_NAME equal to the
   workload (component) name, the endpoint comes from values, no env name is duplicated.
3. The egress rule lands only in the policies of emitting workloads (never postgres/redis/
   seaweedfs/valkey/webui), on the collector ports only, and only when enabled.
4. Misconfiguration fails the render loudly (empty endpoint, ratio outside 0..1).

Zero rendered documents / zero matching containers is a hard failure, never a silent pass
(critical-rules.md Verification Integrity); every assertion reports how many it examined.
Runs under helm v3 and v4 (set HELM_BIN to pick one; default is `helm` on PATH).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

# rendered workload metadata.name suffix -> OTEL_SERVICE_NAME (the component label).
_EMITTERS = {
    "hub-api-v3": "hub-api",
    "svc-ingest-rust": "svc-ingest-rust",
    "svc-process-rust": "svc-process-rust",
    "svc-action-rust": "svc-action-rust",
    "svc-bundles-executor": "svc-bundles-executor",
    "svc-presentation": "svc-presentation",
    "svc-streaming": "svc-streaming",
    "egress-proxy": "egress-proxy",
}

# CNP name suffix of every policy that must carry the collector rule when enabled (dal2alpha).
_CNPS_WITH_RULE = {
    "dataplane-egress-deny",
    "egress-proxy-egress",
    "hub-api-policy",
    "svc-bundles-executor",
    "svc-presentation-policy",
    "svc-streaming-policy",
}
# ... and the ones that must NEVER get it.
_CNPS_WITHOUT_RULE = {
    "postgres-policy",
    "redis-policy",
    "seaweedfs-policy",
    "valkey-durable-policy",
    "valkey-kv-policy",
    "hub-webui-v3-policy",
}

_ENDPOINT = "http://signoz-otel-collector.signoz.svc.cluster.local:4317"
_ENV_FILES = {
    "alpha": ("values-alpha.yaml", []),
    "dal2alpha": ("values-dal2alpha.yaml", []),
    "beta": ("values-beta.yaml", ["global.deploymentTier=alpha"]),
    "gamma": ("values-gamma.yaml", ["global.deploymentTier=alpha"]),
    "production": ("values-production.yaml", ["global.deploymentTier=alpha"]),
    "local": ("values-local.yaml", []),
}


def _helm() -> str:
    """Resolves the helm binary under test (HELM_BIN override), skipping when absent."""
    h = os.environ.get("HELM_BIN") or shutil.which("helm")
    if not h:
        pytest.skip("helm CLI not available in this environment")
    return h


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Runs `helm template` with fixed argv (no shell); --kube-version keeps helm v4 happy."""
    cmd = [_helm(), "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.31.0", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only


def _render(env: str, sets: list[str] | None = None, files: list[str] | None = None) -> list[dict[str, Any]]:
    """Renders one environment's values (+ optional --set overrides) to non-Secret documents."""
    values, base_sets = _ENV_FILES[env]
    args = ["-f", str(_CHART_DIR / values)]
    for f in files or []:
        args += ["-f", str(_CHART_DIR / f)]
    for s in [*base_sets, *(sets or [])]:
        args += ["--set", s]
    res = _run(args)
    if res.returncode != 0:
        pytest.fail(f"helm template failed ({env}):\n{res.stderr[-2000:]}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """All containers (main + init) of a workload document, CronJob-aware."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = spec["template"]["spec"]
    return [*(pod.get("initContainers") or []), *(pod.get("containers") or [])]


def _workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Workload documents only."""
    return [d for d in docs if d.get("kind") in _WORKLOAD_KINDS]


def _emitter(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    """The single workload whose name ends with the suffix (release prefix ignored)."""
    hits = [d for d in _workloads(docs) if d["metadata"]["name"].endswith("-" + suffix)]
    assert len(hits) == 1, f"expected exactly one workload *-{suffix}, got {len(hits)}"
    return hits[0]


def _env(container: dict[str, Any]) -> list[dict[str, Any]]:
    """The container's env list (empty when unset)."""
    return container.get("env") or []


def _collector_rules(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Egress rules of a CNP that target the signoz namespace."""
    out = []
    for r in doc["spec"].get("egress") or []:
        for ep in r.get("toEndpoints") or []:
            if ep.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "signoz":
                out.append(r)
    return out


def _cnps(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """CiliumNetworkPolicies keyed by name with the release prefix stripped."""
    return {d["metadata"]["name"].removeprefix("waddlebot-"): d for d in docs if d.get("kind") == "CiliumNetworkPolicy"}


# ---------------------------------------------------------------- disabled


@pytest.mark.parametrize("env", sorted(_ENV_FILES))
def test_disabled_renders_no_otel_env_and_no_egress_rule(env: str) -> None:
    """Base default (alpha/beta/gamma/prod/local, dal2alpha forced off): zero OTEL_* env, zero rules."""
    docs = _render(env, ["global.otel.enabled=false"])
    examined = 0
    for w in _workloads(docs):
        for c in _containers(w):
            examined += 1
            names = [e["name"] for e in _env(c)]
            assert "K8S_POD_NAME" not in names, (w["metadata"]["name"], c["name"])
            otel = [e for e in _env(c) if e["name"].startswith("OTEL_")]
            if w["metadata"]["name"].endswith("-svc-presentation") and c["name"] == "svc-presentation":
                # pre-existing legacy behaviour, preserved byte-for-byte
                assert {e["name"] for e in otel} == {"OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES"}
                assert not any(e["name"] == "OTEL_EXPORTER_OTLP_ENDPOINT" for e in otel)
            else:
                assert otel == [], (w["metadata"]["name"], c["name"], otel)
    assert examined > 10, f"only {examined} containers examined"
    cnps = _cnps(docs)
    assert len(cnps) >= 5, f"only {len(cnps)} CNPs examined"
    for name, cnp in cnps.items():
        assert _collector_rules(cnp) == [], f"{name} carries a collector rule while otel is disabled"


def test_base_default_is_disabled_for_every_env_except_dal2alpha() -> None:
    """global.otel.enabled defaults false in base values; only values-dal2alpha opts in."""
    base = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())["global"]["otel"]
    assert base["enabled"] is False
    for env, (fname, _) in _ENV_FILES.items():
        v = yaml.safe_load((_CHART_DIR / fname).read_text())
        enabled = ((v.get("global") or {}).get("otel") or {}).get("enabled", False)
        assert enabled is (env == "dal2alpha"), f"{fname}: otel.enabled={enabled}"


# ---------------------------------------------------------------- enabled


@pytest.mark.parametrize("env", ["dal2alpha", "alpha"])
def test_enabled_exactly_one_service_name_per_emitter(env: str) -> None:
    """Every emitter container: one OTEL_SERVICE_NAME == component name, values endpoint, no dup names."""
    docs = _render(env, ["global.otel.enabled=true"])
    examined = 0
    for suffix, svc in _EMITTERS.items():
        w = _emitter(docs, suffix)
        main = [c for c in _containers(w) if c["name"] == svc or c["name"] == suffix]
        assert len(main) == 1, (suffix, [c["name"] for c in _containers(w)])
        env_list = _env(main[0])
        names = [e["name"] for e in env_list]
        dups = {n for n in names if names.count(n) > 1}
        assert not dups, f"{suffix}: duplicate env names {dups}"
        by = {e["name"]: e for e in env_list}
        assert [n for n in names if n == "OTEL_SERVICE_NAME"] == ["OTEL_SERVICE_NAME"]
        assert by["OTEL_SERVICE_NAME"]["value"] == svc
        assert by["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == _ENDPOINT
        assert by["OTEL_EXPORTER_OTLP_PROTOCOL"]["value"] == "grpc"
        assert by["OTEL_TRACES_SAMPLER"]["value"] == "parentbased_traceidratio"
        assert by["OTEL_TRACES_SAMPLER_ARG"]["value"] == "1.0"
        for sig in ("TRACES", "METRICS", "LOGS"):
            assert by[f"OTEL_{sig}_EXPORTER"]["value"] == "otlp"
        assert by["K8S_POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
        # $(K8S_POD_NAME) only expands when K8S_POD_NAME precedes it in the list
        assert names.index("K8S_POD_NAME") < names.index("OTEL_RESOURCE_ATTRIBUTES")
        attrs = dict(kv.split("=", 1) for kv in by["OTEL_RESOURCE_ATTRIBUTES"]["value"].split(","))
        assert attrs["service.namespace"] == "waddlebot"
        assert attrs["deployment.environment"] == "alpha"
        assert attrs["k8s.pod.name"] == "$(K8S_POD_NAME)"
        # init containers never carry the exporter env
        for c in _containers(w):
            if c is not main[0]:
                assert not [e for e in _env(c) if e["name"].startswith("OTEL_")]
        examined += 1
    assert examined == len(_EMITTERS) == 8


def test_non_emitters_never_get_otel_env_when_enabled() -> None:
    """Databases, caches, webui, jobs and cronjobs stay free of OTEL_* env even when enabled."""
    docs = _render("dal2alpha")
    emitter_names = {"waddlebot-" + s for s in _EMITTERS}
    examined = 0
    for w in _workloads(docs):
        if w["metadata"]["name"] in emitter_names:
            continue
        for c in _containers(w):
            examined += 1
            assert not [e for e in _env(c) if e["name"].startswith("OTEL_")], (w["metadata"]["name"], c["name"])
    assert examined > 5


def test_values_drive_endpoint_protocol_sampler_and_signals() -> None:
    """Endpoint/protocol/sampler/ratio/exporters/environment/attributes all come from values."""
    docs = _render(
        "dal2alpha",
        [
            "global.otel.endpoint=http://collector.example:4318",
            "global.otel.protocol=http/protobuf",
            "global.otel.sampler=always_on",
            "global.otel.samplerRatio=0.25",
            "global.otel.exporters.logs=none",
            "global.otel.environment=staging",
            "global.otel.resourceAttributes.team=platform",
        ],
    )
    c = _containers(_emitter(docs, "hub-api-v3"))[-1]
    by = {e["name"]: e.get("value") for e in _env(c)}
    assert by["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.example:4318"
    assert by["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert by["OTEL_TRACES_SAMPLER"] == "always_on"
    assert by["OTEL_TRACES_SAMPLER_ARG"] == "0.25"
    assert by["OTEL_LOGS_EXPORTER"] == "none"
    attrs = dict(kv.split("=", 1) for kv in by["OTEL_RESOURCE_ATTRIBUTES"].split(","))
    assert attrs["deployment.environment"] == "staging"
    assert attrs["team"] == "platform"


def test_presentation_own_endpoint_override_wins_and_is_single() -> None:
    """presentation.rust.otlpEndpoint (pre-existing override) beats the global endpoint, once."""
    docs = _render("dal2alpha", ["presentation.rust.otlpEndpoint=http://own.example:4317"])
    c = [c for c in _containers(_emitter(docs, "svc-presentation")) if c["name"] == "svc-presentation"][0]
    eps = [e["value"] for e in _env(c) if e["name"] == "OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert eps == ["http://own.example:4317"]
    assert [e["name"] for e in _env(c)].count("OTEL_SERVICE_NAME") == 1


def test_presentation_legacy_override_still_works_when_disabled() -> None:
    """With global.otel off, presentation.rust.otlpEndpoint still renders (historical behaviour)."""
    docs = _render("alpha", ["presentation.rust.otlpEndpoint=http://own.example:4317"])
    c = [c for c in _containers(_emitter(docs, "svc-presentation")) if c["name"] == "svc-presentation"][0]
    by = {e["name"]: e.get("value") for e in _env(c)}
    assert by["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://own.example:4317"
    assert by["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"


# ---------------------------------------------------------------- policy


def test_egress_rule_only_in_emitter_policies_on_collector_port() -> None:
    """Enabled: exactly one namespace+label rule on TCP 4317 per emitter policy; none elsewhere."""
    docs = _render("dal2alpha")
    cnps = _cnps(docs)
    examined = 0
    for name in _CNPS_WITH_RULE:
        assert name in cnps, f"missing CNP {name}"
        rules = _collector_rules(cnps[name])
        assert len(rules) == 1, (name, len(rules))
        sel = rules[0]["toEndpoints"][0]["matchLabels"]
        assert sel == {
            "io.kubernetes.pod.namespace": "signoz",
            "app.kubernetes.io/name": "signoz",
            "app.kubernetes.io/instance": "signoz",
            "app.kubernetes.io/component": "otel-collector",
        }
        assert rules[0]["toPorts"] == [{"ports": [{"port": "4317", "protocol": "TCP"}]}]
        examined += 1
    for name in _CNPS_WITHOUT_RULE:
        if name in cnps:
            assert _collector_rules(cnps[name]) == [], f"{name} must not reach the collector"
            examined += 1
    assert examined >= len(_CNPS_WITH_RULE) + 3


def test_egress_rule_can_be_switched_off_independently() -> None:
    """global.otel.egress.enabled=false keeps the env but renders no collector rule."""
    docs = _render("dal2alpha", ["global.otel.egress.enabled=false"])
    cnps = _cnps(docs)
    assert len(cnps) >= 5
    assert all(_collector_rules(c) == [] for c in cnps.values())
    c = _containers(_emitter(docs, "hub-api-v3"))[-1]
    assert any(e["name"] == "OTEL_SERVICE_NAME" for e in _env(c))


def test_collector_ports_follow_values() -> None:
    """Switching to HTTP only needs collector.ports to gain 4318 (no template edit)."""
    docs = _render("dal2alpha", ["global.otel.collector.ports={4317,4318}"])
    rule = _collector_rules(_cnps(docs)["hub-api-policy"])[0]
    assert [p["port"] for p in rule["toPorts"][0]["ports"]] == ["4317", "4318"]


def test_no_standalone_otel_policy_that_could_flip_a_pod_to_default_deny() -> None:
    """The allow is inside existing egress policies; no CNP exists solely for OTLP."""
    docs = _render("dal2alpha")
    assert not [n for n in _cnps(docs) if "otel" in n or "otlp" in n]


# ---------------------------------------------------------------- fail loud


def test_enabled_without_endpoint_fails_the_render() -> None:
    """An enabled block with no endpoint is a chart error, never a silent no-op."""
    values, base_sets = _ENV_FILES["dal2alpha"]
    res = _run(["-f", str(_CHART_DIR / values), "--set", "global.otel.endpoint="])
    assert res.returncode != 0
    assert "global.otel.endpoint" in res.stderr


@pytest.mark.parametrize("ratio", ["1.5", "-0.1"])
def test_ratio_outside_unit_interval_fails_the_render(ratio: str) -> None:
    """The head-sampling ratio is validated to 0..1."""
    values, _ = _ENV_FILES["dal2alpha"]
    res = _run(["-f", str(_CHART_DIR / values), "--set", f"global.otel.samplerRatio={ratio}"])
    assert res.returncode != 0
    assert "samplerRatio" in res.stderr


def test_comma_in_resource_attribute_fails_the_render() -> None:
    """A comma would corrupt OTEL_RESOURCE_ATTRIBUTES; refused at render time."""
    values, _ = _ENV_FILES["dal2alpha"]
    res = _run(["-f", str(_CHART_DIR / values), "--set-string", "global.otel.resourceAttributes.bad=a\\,b"])
    assert res.returncode != 0
    assert "commas" in res.stderr


def test_ci_overlay_renders_with_otel_disabled() -> None:
    """values-alpha + values-ci (the helm-chart-e2e install) renders and carries no collector rule."""
    res = _run(["-f", str(_CHART_DIR / "values-alpha.yaml"), "-f", str(_CHART_DIR / "values-ci.yaml")])
    assert res.returncode == 0, res.stderr[-1500:]
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert len(docs) > 30
    assert all(_collector_rules(c) == [] for c in _cnps(docs).values())
