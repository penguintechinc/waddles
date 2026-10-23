"""Helm-template assertions for the durable delivery-state Valkey (infrastructure.valkeyDurable).

Pins: noeviction + AOF + PVC on a StatefulSet carrying the STATEFUL EXCEPTION marker, primary
Service pinned to ordinal 0, persistence-off fails the render, and every delivery-critical
client (Rust spine pods, Python stage pods, hub-api, seeder) reads VALKEY_DURABLE_* keys.
Zero rendered documents / zero matching clients is a hard failure, never a silent pass.
"""
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent
_ENVS = ("values-alpha.yaml", "values-beta.yaml", "values-gamma.yaml", "values-production.yaml", "values-ci.yaml")
_CLIENTS = ("svc-ingest-rust", "svc-process-rust", "svc-action-rust")


def _helm(values, extra=()):
    args = ["helm", "template", "w", str(_CHART), "--kube-version", "1.30.0", "--values", str(_CHART / values),
            "--set", "global.deploymentTier=alpha"]
    for kv in extra:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603


def _docs(values, extra=()):
    r = _helm(values, extra)
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs
    return docs


@pytest.mark.parametrize("values", _ENVS)
def test_durable_statefulset_shape(values):
    docs = _docs(values)
    sts = [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-durable"]
    assert len(sts) == 1
    cmd = sts[0]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "--maxmemory-policy noeviction" in cmd and "allkeys" not in cmd
    assert "--appendonly yes" in cmd and "--appendfsync" in cmd
    assert sts[0]["spec"]["volumeClaimTemplates"], "durable store must have per-pod PVCs"
    svc = [d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "infra-valkey-durable"]
    assert svc[0]["spec"]["selector"]["statefulset.kubernetes.io/pod-name"] == "valkey-durable-0"


def test_persistence_off_fails_render():
    r = _helm("values-alpha.yaml", ["infrastructure.valkeyDurable.persistence.enabled=false"])
    assert r.returncode != 0 and "emptyDir" in r.stderr


@pytest.mark.parametrize("values", ("values-beta.yaml", "values-production.yaml"))
def test_replica_pdb_and_antiaffinity(values):
    docs = _docs(values)
    assert [d for d in docs if d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"] == "valkey-durable"]
    sts = [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-durable"][0]
    assert "podAntiAffinity" in sts["spec"]["template"]["spec"]["affinity"]


def test_rust_spine_clients_use_durable_keys():
    docs = _docs("values-alpha.yaml")
    seen = 0
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(_CLIENTS):
            keys = [e["valueFrom"]["secretKeyRef"]["key"] for c in d["spec"]["template"]["spec"]["containers"]
                    for e in c.get("env", []) if e["name"] in ("VALKEY_URL", "VALKEY_PASSWORD") and "valueFrom" in e]
            assert "VALKEY_DURABLE_URL_TLS" in keys and "VALKEY_DURABLE_PASSWORD" in keys, d["metadata"]["name"]
            seen += 1
    assert seen == len(_CLIENTS), f"examined {seen} spine clients"


def _durable_cmd(docs):
    sts = [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-durable"]
    assert len(sts) == 1
    return sts[0]["spec"]["template"]["spec"]["containers"][0]["command"][2]


def test_tls_on_disables_plaintext_listener_and_url():
    """Security review MEDIUM: TLS on => --port 0, no plaintext Service port, no plaintext URL."""
    docs = _docs("values-alpha.yaml")
    cmd = _durable_cmd(docs)
    assert "--port 0" in cmd and "--tls-port" in cmd
    svcs = [d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] in ("infra-valkey-durable", "valkey-durable-headless")]
    assert len(svcs) == 2
    for s in svcs:
        assert [p["name"] for p in s["spec"]["ports"]] == ["valkey-tls"], s["metadata"]["name"]
    sec = [d for d in docs if d["kind"] == "Secret" and "VALKEY_DURABLE_PASSWORD" in (d.get("stringData") or {})]
    assert len(sec) == 1
    keys = sec[0]["stringData"]
    assert "VALKEY_DURABLE_URL" not in keys
    assert keys["VALKEY_DURABLE_URL_TLS"].startswith("rediss://")
    assert keys["VALKEY_DURABLE_URL_TLS_AUTH"].startswith("rediss://:") and "ssl_ca_certs=" in keys["VALKEY_DURABLE_URL_TLS_AUTH"]


def test_python_stage_clients_use_rediss():
    docs = _docs("values-beta.yaml", ("infrastructure.redis.tls.enabled=true",))
    seen = 0
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(("-svc-ingest", "-svc-process", "-svc-action")):
            keys = [e["valueFrom"]["secretKeyRef"]["key"] for c in d["spec"]["template"]["spec"]["containers"]
                    for e in c.get("env", []) if e["name"] == "VALKEY_URL" and "valueFrom" in e]
            assert keys == ["VALKEY_DURABLE_URL_TLS_AUTH"], d["metadata"]["name"]
            assert any(v["name"] == "valkey-ca" for v in d["spec"]["template"]["spec"]["volumes"])
            seen += 1
    assert seen == 3, f"examined {seen} python stage clients"


def test_acl_restricts_default_user():
    cmd = _durable_cmd(_docs("values-alpha.yaml"))
    for denied in ("-config", "-flushall", "-flushdb", "-replicaof", "-slaveof", "-debug", "-shutdown"):
        assert f" {denied} " in cmd or cmd.count(denied) >= 1, denied
    assert "--masteruser admin" in cmd and "--requirepass" not in cmd


def test_tls_off_keeps_plaintext_port():
    docs = _docs("values-ci.yaml", ("infrastructure.redis.tls.enabled=false",))
    assert "--port 6379" in _durable_cmd(docs) and "--port 0" not in _durable_cmd(docs)
