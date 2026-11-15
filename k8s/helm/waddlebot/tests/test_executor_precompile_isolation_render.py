"""Helm-template assertions for the executor-precompile isolation hardening (security review M1/M2/L1).

Pins, for the CronJob AND the backfill Job pod specs:

M1  compile and sign are separate containers: compile has NO key volume mount / key env, sign mounts the
    key secret 0400; an enforced Tetragon exec allowlist selects the pods; runtimeClassName is absent by
    default and present only when set.
M2  no envFrom on any precompile container; dedicated ServiceAccount with automountServiceAccountToken
    false (pod + SA).
L1  CiliumNetworkPolicy: no ingress, egress only to kube-dns, Postgres, SeaweedFS S3 and the OTLP collector.

Every test reports/asserts how many pod specs / containers it examined -- zero is a failure, never a
pass (critical-rules.md Verification Integrity). A missing helm CLI is a failure, not a skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ENVS = ("values-beta.yaml", "values-gamma.yaml", "values-production.yaml", "values-dal2alpha.yaml")
_FLAG = "waddlebot.io/workload"


def _render(values: str, extra_set: list[str] | None = None) -> list[dict[str, Any]]:
    """helm-template one values file; a render failure or zero documents fails the test."""
    args = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART_DIR / values),
        "--set",
        "global.deploymentTier=alpha",
        "--set",
        "tetragon.enabled=true",
    ]
    for kv in extra_set or []:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        pytest.fail(f"helm template failed for {values}:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, f"helm template produced zero documents for {values}"
    return docs


def _pod_specs(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(kind, podSpec) for the precompile CronJob and backfill Job."""
    out: list[tuple[str, dict[str, Any]]] = []
    for d in docs:
        name = d.get("metadata", {}).get("name", "")
        if d["kind"] == "CronJob" and name.endswith("-executor-precompile"):
            out.append(("CronJob", d["spec"]["jobTemplate"]["spec"]["template"]["spec"]))
        elif d["kind"] == "Job" and name.endswith("-executor-precompile-backfill"):
            out.append(("Job", d["spec"]["template"]["spec"]))
    return out


def _containers(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in spec.get("initContainers", []) + spec.get("containers", [])}


@pytest.mark.parametrize("values", _ENVS)
class TestPrecompileIsolation:
    """Per-environment isolation checks on both precompile pod specs."""

    def test_pod_specs_rendered(self, values: str) -> None:
        specs = _pod_specs(_render(values))
        assert sorted(k for k, _ in specs) == ["CronJob", "Job"], f"examined {len(specs)} pod specs"

    def test_compile_has_no_key_and_sign_mounts_key_0400(self, values: str) -> None:
        specs = _pod_specs(_render(values))
        assert len(specs) == 2
        for kind, spec in specs:
            cs = _containers(spec)
            assert {"export", "compile", "sign", "ack"} <= set(cs), f"{kind}: {sorted(cs)}"
            key_volumes = {
                v["name"] for v in spec["volumes"] if "secret" in v and v["name"] == "precompile-signing"
            }
            assert key_volumes == {"precompile-signing"}
            secret_vol = next(v for v in spec["volumes"] if v["name"] == "precompile-signing")
            assert secret_vol["secret"]["defaultMode"] == 0o400
            # Only the sign container may mount the key volume.
            for name, c in cs.items():
                mounted = {m["name"] for m in c.get("volumeMounts", [])}
                assert ("precompile-signing" in mounted) == (name == "sign"), f"{kind}/{name}"
            compile_c = cs["compile"]
            assert compile_c["args"][0] == "--precompile-compile-only"
            env_names = {e["name"] for e in compile_c["env"]}
            assert not any(n.startswith("PRECOMPILE_SIGNING_KEY") for n in env_names)
            sign_c = cs["sign"]
            assert sign_c["args"][0] == "--precompile-sign"
            sign_env = {e["name"] for e in sign_c["env"]}
            assert {"PRECOMPILE_SIGNING_KEY_FILE", "PRECOMPILE_SIGNING_KEY_ID"} <= sign_env
            # Compile reads work (manifest) read-only; sign reads staged output read-only.
            assert next(m for m in compile_c["volumeMounts"] if m["name"] == "work")["readOnly"]
            assert next(m for m in sign_c["volumeMounts"] if m["name"] == "staged")["readOnly"]
            # Compile uses the read-only executor S3 identity, sign the write-scoped precompiler one.
            def s3_key(c: dict[str, Any]) -> str:
                e = next(x for x in c["env"] if x["name"] == "BUNDLE_BUCKET_ACCESS_KEY_ID")
                return e["valueFrom"]["secretKeyRef"]["key"]

            assert s3_key(compile_c) == "DATAPLANE_S3_ACCESS_KEY_ID"
            assert s3_key(sign_c) == "PRECOMPILER_S3_ACCESS_KEY_ID"

    def test_no_envfrom_and_explicit_secret_refs_only(self, values: str) -> None:
        specs = _pod_specs(_render(values))
        examined = 0
        for kind, spec in specs:
            for name, c in _containers(spec).items():
                examined += 1
                assert "envFrom" not in c, f"{kind}/{name} still uses envFrom"
                for e in c.get("env", []):
                    ref = e.get("valueFrom", {}).get("secretKeyRef")
                    if ref:
                        assert ref["key"], f"{kind}/{name} {e['name']}"
        assert examined == 8, f"examined {examined} containers"

    def test_dedicated_service_account_without_token(self, values: str) -> None:
        docs = _render(values)
        specs = _pod_specs(docs)
        sas = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ServiceAccount"}
        assert len(specs) == 2
        for kind, spec in specs:
            assert spec["automountServiceAccountToken"] is False
            sa_name = spec["serviceAccountName"]
            assert sa_name.endswith("-executor-precompile"), f"{kind}: {sa_name}"
            assert sa_name != "waddlebot"
            assert sas[sa_name]["automountServiceAccountToken"] is False

    def test_cnp_default_deny_with_only_expected_egress(self, values: str) -> None:
        docs = _render(values, ["otel.collector.enabled=true"])
        cnps = [
            d
            for d in docs
            if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-executor-precompile")
        ]
        assert len(cnps) == 1, f"examined {len(cnps)} CNPs"
        spec = cnps[0]["spec"]
        assert spec["endpointSelector"]["matchLabels"] == {_FLAG: "executor-precompile"}
        assert spec["ingress"] == []
        egress = spec["egress"]
        assert len(egress) == 4, f"egress rules: {len(egress)}"
        targets = []
        for rule in egress:
            labels = rule["toEndpoints"][0]["matchLabels"]
            comp = labels.get("app.kubernetes.io/component")
            targets.append(
                labels.get("k8s-app")
                or (comp if comp == "otel-collector" else labels.get("app.kubernetes.io/name"))
            )
            assert "toCIDR" not in rule and "toCIDRSet" not in rule and "toEntities" not in rule
        assert targets == ["kube-dns", "postgres", "seaweedfs", "otel-collector"]
        # Pod templates carry the label the CNP and Tetragon policy select on.
        for kind, spec_ in _pod_specs(docs):
            assert kind in ("CronJob", "Job")
        for d in docs:
            if d["kind"] == "CronJob" and d["metadata"]["name"].endswith("-executor-precompile"):
                tpl = d["spec"]["jobTemplate"]["spec"]["template"]
                assert tpl["metadata"]["labels"][_FLAG] == "executor-precompile"
            if d["kind"] == "Job" and d["metadata"]["name"].endswith("-executor-precompile-backfill"):
                assert d["spec"]["template"]["metadata"]["labels"][_FLAG] == "executor-precompile"

    def test_tracing_policy_enforced_and_allows_executor_binary(self, values: str) -> None:
        docs = _render(values)
        pols = [
            d
            for d in docs
            if d["kind"] == "TracingPolicyNamespaced" and d["metadata"]["name"].endswith("-executor-precompile-exec-allowlist")
        ]
        assert len(pols) == 1, f"examined {len(pols)} tracing policies"
        pol = pols[0]["spec"]
        assert pol["podSelector"]["matchLabels"] == {_FLAG: "executor-precompile"}
        selectors = pol["kprobes"][0]["selectors"]
        assert selectors[0]["matchActions"][0]["action"] == "Sigkill"
        allowed = selectors[0]["matchArgs"][0]["values"]
        assert "/app/bundle-executor" in allowed
        assert "/bin/sh" not in allowed
        # runc-init guard (kubectl exec keeps working): second selector kills non-runc /proc/self/fd execs.
        assert len(selectors) == 2

    def test_runtime_class_off_by_default_and_set_on_demand(self, values: str) -> None:
        for kind, spec in _pod_specs(_render(values)):
            assert "runtimeClassName" not in spec, kind
        on = _pod_specs(_render(values, ["pipeline.hubApi.executorPrecompile.runtimeClassName=kata-clh"]))
        assert len(on) == 2
        for _, spec in on:
            assert spec["runtimeClassName"] == "kata-clh"
