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


def _default_acl(cmd):
    line = [ln for ln in cmd.splitlines() if "--user default" in ln]
    assert len(line) == 1, "expected exactly one default-user ACL line"
    return line[0]


def test_acl_default_user_is_allow_list():
    """Default user = allow-list: -@all first, only the client categories, no admin/dangerous/config."""
    cmd = _durable_cmd(_docs("values-alpha.yaml"))
    acl = _default_acl(cmd)
    rules = acl.split("--user default on", 1)[1].split()
    rules = [r for r in rules if r[0] in "+-" and r != "\\"]
    assert rules[0] == "-@all", rules
    for cat in ("+@connection", "+@read", "+@write", "+@stream", "+@keyspace", "+@transaction", "+@scripting"):
        assert cat in rules, cat
    assert "+@all" not in acl and "+@dangerous" not in acl and "+@admin" not in acl and "+config" not in acl
    assert "-@dangerous" in rules and rules.index("-@dangerous") > rules.index("+@scripting")
    assert not [r for r in rules if r.startswith("-") and not r.startswith("-@")], "no per-command deny-list"
    assert "--masteruser admin" in cmd and "--requirepass" not in cmd


def test_admin_password_isolated_from_workloads():
    """The admin password Secret is read ONLY by the valkey-durable StatefulSet -- never envFrom'd/referenced elsewhere."""
    docs = _docs("values-alpha.yaml")
    admin = [d for d in docs if d["kind"] == "Secret" and "VALKEY_DURABLE_ADMIN_PASSWORD" in (d.get("stringData") or {})]
    assert len(admin) == 1, "admin password must live in exactly one dedicated Secret"
    assert admin[0]["metadata"]["name"].endswith("-valkey-durable-admin")
    assert len(admin[0]["stringData"]) == 1
    for d in docs:
        if d["kind"] == "Secret" and d is not admin[0]:
            assert "VALKEY_DURABLE_ADMIN_PASSWORD" not in (d.get("stringData") or {}), d["metadata"]["name"]
    admin_name = admin[0]["metadata"]["name"]
    scanned = referrers = 0
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet", "Job", "CronJob", "DaemonSet", "Pod"):
            continue
        tpl = d["spec"]["jobTemplate"]["spec"]["template"] if d["kind"] == "CronJob" else d["spec"]["template"] if d["kind"] != "Pod" else d
        pod = tpl["spec"]
        scanned += 1
        refs = []
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            refs += [f["secretRef"]["name"] for f in c.get("envFrom", []) if "secretRef" in f]
            refs += [e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})]
            for e in c.get("env", []):
                assert e.get("valueFrom", {}).get("secretKeyRef", {}).get("key") != "VALKEY_DURABLE_ADMIN_PASSWORD" or (
                    d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-durable"), d["metadata"]["name"]
        refs += [v["secret"]["secretName"] for v in pod.get("volumes", []) if "secret" in v]
        if admin_name in refs:
            referrers += 1
            assert d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-durable", d["metadata"]["name"]
    assert scanned > 10, f"examined only {scanned} workloads"
    assert referrers == 1, "valkey-durable StatefulSet must reference the admin Secret"


def test_tls_off_keeps_plaintext_port():
    docs = _docs("values-ci.yaml", ("infrastructure.redis.tls.enabled=false",))
    assert "--port 6379" in _durable_cmd(docs) and "--port 0" not in _durable_cmd(docs)
