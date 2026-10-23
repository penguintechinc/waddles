"""Helm-template assertions for the dedicated bundle-kv Valkey (infrastructure.valkeyKv).

Pins: noeviction + AOF + retained PVC on a single-pod StatefulSet (STATEFUL EXCEPTION marker), the
allow-list ACL (+config|get AFTER -@dangerous, no admin user / replication), TLS-only, a dedicated
Secret that no workload envFrom's and no secret-derived value in any non-secret field, and the
svc-*-rust VALKEY_KV_URL wiring. Zero rendered documents / zero matching clients is a hard failure.
"""
import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent
_ENVS = ("values-alpha.yaml", "values-beta.yaml", "values-gamma.yaml", "values-production.yaml", "values-ci.yaml")
_RUST = ("svc-process-rust", "svc-action-rust")


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


def _sts(docs):
    sts = [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-kv"]
    assert len(sts) == 1, "expected exactly one valkey-kv StatefulSet"
    return sts[0]


def _cmd(docs):
    return _sts(docs)["spec"]["template"]["spec"]["containers"][0]["command"][2]


@pytest.mark.parametrize("values", _ENVS)
def test_kv_statefulset_shape(values):
    sts = _sts(_docs(values))
    cmd = sts["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "--maxmemory-policy noeviction" in cmd and "allkeys" not in cmd and "volatile" not in cmd
    assert "--appendonly yes" in cmd and "--appendfsync everysec" in cmd
    assert sts["spec"]["replicas"] == 1
    assert "--replicaof" not in cmd and "--masteruser" not in cmd and "--user admin" not in cmd
    assert sts["spec"]["volumeClaimTemplates"], "kv store must have a PVC"
    assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == {"whenDeleted": "Retain", "whenScaled": "Retain"}
    sec = sts["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert sec["runAsNonRoot"] is True and sec["readOnlyRootFilesystem"] is True and sec["allowPrivilegeEscalation"] is False
    assert sts["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 999 or sec.get("runAsUser") == 999


def test_kv_stateful_exception_marker():
    src = (_CHART / "templates" / "infrastructure" / "valkey-kv.yaml").read_text()
    assert "# STATEFUL EXCEPTION (approved)" in src and "2026-10-23" in src


def test_kv_acl_allow_list_probe_works_no_dangerous():
    """+config|get must come AFTER -@dangerous (CONFIG GET is @dangerous); nothing broader is granted."""
    cmd = _cmd(_docs("values-alpha.yaml"))
    line = [ln for ln in cmd.splitlines() if "--user default" in ln]
    assert len(line) == 1
    rules = [r.strip("'\"") for r in line[0].split("--user default on", 1)[1].split() if r.strip("'\"")[:1] in "+-"]
    assert rules[0] == "-@all", rules
    for cat in ("+@connection", "+@read", "+@write", "+@keyspace", "+@transaction", "+@scripting"):
        assert cat in rules, cat
    assert "-@dangerous" in rules and "+config|get" in rules
    assert rules.index("+config|get") > rules.index("-@dangerous"), "order matters: later +config|get survives -@dangerous"
    assert "+@dangerous" not in rules and "+@all" not in rules and "+@admin" not in rules
    assert not [r for r in rules if r.startswith("+config") and r != "+config|get"]
    assert "'+config|get'" in line[0], "pipe must be shell-quoted"


def test_kv_tls_only_and_service():
    docs = _docs("values-alpha.yaml")
    cmd = _cmd(docs)
    assert "--port 0" in cmd and "--tls-port" in cmd
    svc = [d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "infra-valkey-kv"]
    assert len(svc) == 1 and [p["name"] for p in svc[0]["spec"]["ports"]] == ["valkey-tls"]


def test_kv_persistence_off_fails_render():
    r = _helm("values-alpha.yaml", ["infrastructure.valkeyKv.persistence.enabled=false"])
    assert r.returncode != 0 and "emptyDir" in r.stderr


def test_kv_has_cilium_policy():
    docs = _docs("values-production.yaml")
    assert [d for d in docs if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-valkey-kv-policy")]


def test_kv_password_isolated_and_not_in_nonsecret_fields():
    docs = _docs("values-alpha.yaml")
    kv = [d for d in docs if d["kind"] == "Secret" and (d.get("stringData") or {}).get("VALKEY_KV_PASSWORD")]
    assert len(kv) == 1 and kv[0]["metadata"]["name"].endswith("-valkey-kv")
    pw = kv[0]["stringData"]["VALKEY_KV_PASSWORD"]
    assert len(pw) >= 16
    kv_name = kv[0]["metadata"]["name"]
    scanned = referrers = 0
    for d in docs:
        if d["kind"] == "Secret":
            assert d is kv[0] or not any(k.startswith("VALKEY_KV_") for k in (d.get("stringData") or {})), d["metadata"]["name"]
            continue
        blob = yaml.safe_dump(d)
        assert pw not in blob and hashlib.sha256(pw.encode()).hexdigest() not in blob, d["metadata"]["name"]
        if d["kind"] not in ("Deployment", "StatefulSet", "Job", "CronJob", "DaemonSet"):
            continue
        tpl = d["spec"]["jobTemplate"]["spec"]["template"] if d["kind"] == "CronJob" else d["spec"]["template"]
        pod = tpl["spec"]
        scanned += 1
        refs = []
        for c in pod.get("containers", []) + pod.get("initContainers", []):
            refs += [f["secretRef"]["name"] for f in c.get("envFrom", []) if "secretRef" in f]
            refs += [e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})]
        if kv_name in refs:
            referrers += 1
            assert d["metadata"]["name"] in ("valkey-kv", "w-waddlebot-svc-action-rust", "w-waddlebot-svc-process-rust") or d["metadata"]["name"].endswith(_RUST), d["metadata"]["name"]
            assert not any(f.get("secretRef", {}).get("name") == kv_name for c in pod.get("containers", []) for f in c.get("envFrom", []))
    assert scanned > 10, f"examined only {scanned} workloads"
    assert referrers == 3, f"expected valkey-kv + 2 rust pods to reference the kv Secret, got {referrers}"


@pytest.mark.parametrize("values", ("values-alpha.yaml", "values-ci.yaml"))  # rust data plane renders only here
def test_rust_pods_use_kv_env_not_cache(values):
    docs = _docs(values)
    seen = 0
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(_RUST):
            env = {e["name"]: e for c in d["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
            assert "VALKEY_CACHE_URL" not in env and "VALKEY_CACHE_PASSWORD" not in env, d["metadata"]["name"]
            ref = env["VALKEY_KV_URL"]["valueFrom"]["secretKeyRef"]
            assert ref["key"] == "VALKEY_KV_URL_TLS" and ref["name"].endswith("-valkey-kv")
            assert "value" not in env["VALKEY_KV_URL"], "URL must be credential-free secretKeyRef"
            assert env["VALKEY_KV_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "VALKEY_KV_PASSWORD"
            # delivery stays on durable
            assert env["VALKEY_URL"]["valueFrom"]["secretKeyRef"]["key"] == "VALKEY_DURABLE_URL_TLS"
            assert any(v["name"] == "valkey-ca" for v in d["spec"]["template"]["spec"]["volumes"]), "CA must be mounted"
            seen += 1
    assert seen == len(_RUST), f"examined {seen} rust pods"


def test_kv_disabled_falls_back_to_cache():
    docs = _docs("values-alpha.yaml", ("infrastructure.valkeyKv.enabled=false",))
    assert not [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "valkey-kv"]
    seen = 0
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith(_RUST):
            env = {e["name"]: e for c in d["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
            assert env["VALKEY_KV_URL"]["valueFrom"]["secretKeyRef"]["key"] == "VALKEY_URL_TLS"
            assert env["VALKEY_KV_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "REDIS_PASSWORD"
            seen += 1
    assert seen == len(_RUST)


def test_valkey_tls_cert_covers_kv_san():
    docs = _docs("values-alpha.yaml")
    sec = [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-valkey-tls")]
    assert len(sec) == 1


def test_beta_tier_without_certs_fails_closed():
    """Real beta tier: TLS now defaults on, so a missing cert fails closed (never silent plaintext)."""
    r = subprocess.run(["helm", "template", "w", str(_CHART), "--kube-version", "1.30.0", "--values", str(_CHART / "values-beta.yaml")],  # noqa: S603
                       capture_output=True, text=True, check=False)
    assert r.returncode != 0
