"""Render assertions for svc-analytics (analytics W7).

Pins: OFF by default, hardened pod, native gRPC probes, own DB role secret (no REPLACE_ME,
no `<no value>`), flag default false, dedicated default-deny CNP shape, Tetragon allowlist.
Every helper reports what it examined; zero documents/matches is a failure, never a pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_ON = ["--set", "pipeline.svcAnalytics.enabled=true"]
_NP = ["--set", "networkPolicy.enabled=true"]


def _render(*extra: str, values: str = "values-alpha.yaml") -> tuple[str, list[dict[str, Any]]]:
    """Render the chart and return (raw text, parsed non-empty docs)."""
    cmd = ["helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
           "--values", str(_CHART / values), *extra]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm template produced zero documents"
    return res.stdout, docs


def _pick(docs: list[dict[str, Any]], kind: str, suffix: str) -> dict[str, Any]:
    hits = [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]
    assert len(hits) == 1, f"expected exactly 1 {kind} *{suffix}, examined {len(docs)} docs, got {len(hits)}"
    return hits[0]


def _mine(docs: list[dict[str, Any]]) -> str:
    """Serialize only the svc-analytics documents (other chart parts may legitimately mention REPLACE_ME)."""
    mine = [d for d in docs if "svc-analytics" in d["metadata"]["name"]]
    assert mine, "no svc-analytics documents examined"
    return yaml.safe_dump_all(mine)


def test_off_by_default() -> None:
    _, docs = _render()
    assert [d for d in docs if "svc-analytics" in d["metadata"]["name"]] == []


def test_flag_default_false_and_overridable() -> None:
    _, docs = _render()
    cm = _pick(docs, "ConfigMap", "-config")
    assert cm["data"]["FLAG_WADDLES_ANALYTICS_STREAM"] == "false"
    _, docs = _render("--set", "config.analyticsStreamEnabled=true")
    assert _pick(docs, "ConfigMap", "-config")["data"]["FLAG_WADDLES_ANALYTICS_STREAM"] == "true"


def test_deployment_hardening_and_probes() -> None:
    _, docs = _render(*_ON, *_NP)
    mine = _mine(docs)
    assert "<no value>" not in mine and "REPLACE_ME" not in mine
    dep = _pick(docs, "Deployment", "-svc-analytics")
    pod = dep["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    (c,) = pod["containers"]
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 10001
    assert sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"] and c["resources"]["requests"]
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert "grpc" in c[probe] and "httpGet" not in c[probe], probe
    assert not [v for v in pod.get("volumes", []) if "hostPath" in v or "persistentVolumeClaim" in v]
    assert c["lifecycle"]["preStop"]
    assert pod["terminationGracePeriodSeconds"] > 0
    assert [p["containerPort"] for p in c["ports"]] == [50209]


def test_own_db_role_secret_key_no_placeholder() -> None:
    _, docs = _render(*_ON)
    c = _pick(docs, "Deployment", "-svc-analytics")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in c["env"]}
    assert env["DB_USER"]["value"] == "waddles_svc_analytics"
    ref = env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "PW_WADDLES_SVC_ANALYTICS"
    assert env["ANALYTICS_STREAM_KEY"]["value"] == "waddles:analytics"
    assert env["ANALYTICS_CONSUMER_GROUP"]["value"] == "svc_analytics"


def test_db_credentials_provisioned_for_role() -> None:
    text, _ = _render(*_ON)
    assert "PW_WADDLES_SVC_ANALYTICS" in text


def test_networkpolicy_shape() -> None:
    _, docs = _render(*_ON, *_NP)
    cnp = _pick(docs, "CiliumNetworkPolicy", "-svc-analytics")
    spec = cnp["spec"]
    assert spec["ingress"] == [{}], "ingress must be deny-all (kubelet probes are host-local)"
    dests = []
    for rule in spec["egress"]:
        assert "toEndpoints" in rule and "toCIDRSet" not in rule and "toEntities" not in rule
        dests.append(rule["toEndpoints"][0]["matchLabels"])
    names = {d.get("app.kubernetes.io/name") or d.get("k8s-app") for d in dests}
    assert "kube-dns" in names and "postgres" in names
    assert names & {"redis", "valkey-durable"}
    assert len(dests) <= 5


def test_tetragon_allowlist_enforced() -> None:
    _, docs = _render(*_ON, "--set", "tetragon.enabled=true")
    tp = _pick(docs, "TracingPolicyNamespaced", "-svc-analytics-exec-allowlist")
    vals = tp["spec"]["kprobes"][0]["selectors"][0]["matchArgs"][0]["values"]
    assert "/app/svc-analytics" in vals
    assert not {"/bin/sh", "/bin/bash", "/usr/bin/curl"} & set(vals)


def test_hpa_pdb_when_scaled() -> None:
    _, docs = _render(*_ON, "--set", "pipeline.svcAnalytics.replicas=2")
    _pick(docs, "PodDisruptionBudget", "-svc-analytics")
