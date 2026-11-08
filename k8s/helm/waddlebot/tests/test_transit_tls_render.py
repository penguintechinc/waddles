"""Helm-template assertions for in-cluster transit TLS (feature/transit-tls-core: F1 Postgres, F2 cache Valkey, F5 S3, F8 CA).

Pins, per environment values file (alpha + dal2alpha are the deployable ones; every tier is rendered with
global.deploymentTier=alpha so the self-generated CA material renders):
  * the transit CA + both server leaves are ECDSA P-256 / ecdsa-with-SHA256, chain-verify, and carry the FQDN SAN;
  * Postgres serves TLS only (ssl=on + a hostnossl-reject pg_hba) and every DB client dials the FQDN with
    PGSSLMODE=verify-full + a pinned PGSSLROOTCERT that is actually mounted;
  * the cache Valkey has no plaintext listener/Service port and every REDIS_URL/VALKEY_URL is rediss:// with the CA pinned;
  * every S3 client endpoint is https:// with the CA mounted; no plaintext S3/Postgres/cache URL survives anywhere.
Zero rendered documents / zero matching clients is a hard failure (a gate that cannot fail is not a gate).
"""

import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parent.parent
_ENVS = (
    "values-alpha.yaml",
    "values-dal2alpha.yaml",
    "values-beta.yaml",
    "values-gamma.yaml",
    "values-production.yaml",
)
_CA_PATH = "/etc/waddles/transit-ca/ca.crt"
_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "Job", "CronJob")


def _docs(values):
    """Render the chart for one values file and return the parsed documents."""
    args = [
        "helm", "template", "w", str(_CHART), "--kube-version", "1.30.0",
        "--values", str(_CHART / values), "--set", "global.deploymentTier=alpha",
    ]
    r = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs, "helm template rendered zero documents"
    return docs


def _svc(docs, selector_name):
    """Return the Service whose selector targets app.kubernetes.io/name=<selector_name>."""
    hits = [d for d in docs if d["kind"] == "Service" and d["spec"].get("selector", {}).get("app.kubernetes.io/name") == selector_name]
    assert len(hits) == 1, (selector_name, len(hits))
    return hits[0]


def _fqdn(docs, selector_name):
    svc = _svc(docs, selector_name)
    return f'{svc["metadata"]["name"]}.{svc["metadata"]["namespace"]}.svc.cluster.local'


def _secrets_doc(docs):
    hits = [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-secrets") and "REDIS_URL" in d.get("stringData", {})]
    assert len(hits) == 1
    return hits[0]


def _pod_spec(doc):
    if doc["kind"] == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return doc["spec"]["template"]["spec"]


def _containers(docs):
    """Yield (workload-name, container, pod-spec) for every container incl. init containers."""
    n = 0
    for d in docs:
        if d["kind"] not in _WORKLOAD_KINDS:
            continue
        spec = _pod_spec(d)
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            n += 1
            yield d["metadata"]["name"], c, spec
    assert n, "no workload containers rendered"


def _env(c):
    return {e["name"]: e for e in c.get("env", [])}


def _mounts_ca(c, spec):
    mounts = {m["name"]: m for m in c.get("volumeMounts", [])}
    vols = {v["name"]: v for v in spec.get("volumes", [])}
    return (
        "transit-ca" in mounts
        and mounts["transit-ca"]["mountPath"] == "/etc/waddles/transit-ca"
        and vols.get("transit-ca", {}).get("secret", {}).get("items") == [{"key": "ca.crt", "path": "ca.crt"}]
    )


def _openssl_text(pem: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.pem"
        p.write_text(pem)
        return subprocess.run(  # noqa: S603
            ["openssl", "x509", "-in", str(p), "-noout", "-text"],  # noqa: S607
            capture_output=True, text=True, check=True,
        ).stdout


@pytest.mark.parametrize("values", _ENVS)
def test_transit_secret_is_ecdsa_p256_sha256_and_chains(values):
    docs = _docs(values)
    secs = [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-transit-tls")]
    assert len(secs) == 1, "exactly one transit-tls Secret expected"
    data = secs[0]["stringData"]
    assert sorted(data) == ["ca.crt", "postgres.crt", "postgres.key", "seaweedfs.crt", "seaweedfs.key"]
    ec_marker = "BEGIN EC " + "PRIVATE" + " KEY"  # split so secret scanners do not flag the test source
    assert ec_marker in data["postgres.key"] and ec_marker in data["seaweedfs.key"]
    assert "".join(data.values()).count(ec_marker) == 2, "only the two server keys may be stored (never the CA key)"
    ca_txt = _openssl_text(data["ca.crt"])
    assert "CA:TRUE" in ca_txt and "ecdsa-with-SHA256" in ca_txt and "prime256v1" in ca_txt
    for leaf, fqdn in (("postgres.crt", _fqdn(docs, "postgres")), ("seaweedfs.crt", _fqdn(docs, "seaweedfs"))):
        txt = _openssl_text(data[leaf])
        assert "ecdsa-with-SHA256" in txt and "prime256v1" in txt, leaf
        assert f"DNS:{fqdn}" in txt, (leaf, txt)
        assert "TLS Web Server Authentication" in txt
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ca.pem").write_text(data["ca.crt"])
            (Path(d) / "leaf.pem").write_text(data[leaf])
            r = subprocess.run(  # noqa: S603
                ["openssl", "verify", "-CAfile", str(Path(d) / "ca.pem"), str(Path(d) / "leaf.pem")],  # noqa: S607
                capture_output=True, text=True, check=False,
            )
            assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("values", _ENVS)
def test_secret_is_a_pre_hook_so_migration_hooks_can_mount_it(values):
    sec = next(d for d in _docs(values) if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-transit-tls"))
    ann = sec["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "pre-install,pre-upgrade"


@pytest.mark.parametrize("values", _ENVS)
def test_postgres_is_tls_only(values):
    docs = _docs(values)
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "postgres")
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    for want in ("ssl=on", "ssl_cert_file=/etc/postgres-tls/tls.crt", "ssl_key_file=/etc/postgres-tls/tls.key",
                 "ssl_min_protocol_version=TLSv1.2", "hba_file=/etc/postgres-hba/pg_hba.conf"):
        assert want in args, want
    hba = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "postgres-hba")
    lines = [ln.split() for ln in hba["data"]["pg_hba.conf"].splitlines() if ln.strip()]
    tcp = [ln for ln in lines if ln[0] != "local"]
    assert tcp, "no TCP pg_hba rules"
    assert all(ln[0] in ("hostssl", "hostnossl") for ln in tcp), "a plain `host` rule would accept plaintext"
    assert all(ln[-1] == "reject" for ln in tcp if ln[0] == "hostnossl")
    assert not any(ln[0] == "host" for ln in lines)


@pytest.mark.parametrize("values", _ENVS)
def test_every_db_client_is_verify_full_with_mounted_ca(values):
    n = 0
    docs = _docs(values)
    fqdn = _fqdn(docs, "postgres")
    for name, c, spec in _containers(docs):
        env = _env(c)
        if "DATABASE_URL" not in env and "DB_PASSWORD" not in env:
            continue
        n += 1
        assert env["PGSSLMODE"]["value"] == "verify-full", name
        assert env["PGSSLROOTCERT"]["value"] == _CA_PATH, name
        assert _mounts_ca(c, spec), name
        url = env.get("DATABASE_URL", {}).get("value")
        if url:
            assert f"@{fqdn}:" in url, (name, url)
    assert n >= 8, f"only {n} DB-consuming containers found"


@pytest.mark.parametrize("values", _ENVS)
def test_no_plaintext_postgres_dsn_or_host_anywhere(values):
    docs = _docs(values)
    cfg = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "waddlebot-config")
    assert cfg["data"]["DB_HOST"].endswith(".svc.cluster.local")
    assert cfg["data"]["DATABASE_HOST"].endswith(".svc.cluster.local")
    blob = yaml.dump(docs)
    short = _svc(docs, "postgres")["metadata"]["name"]
    assert not re.search(rf"@{re.escape(short)}:", blob), "short-name (SAN-less for the internal CA) Postgres DSN survived"
    mig = next(d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"].endswith("-db-migrate-secret"))
    url = mig["stringData"]["DATABASE_URL"]
    assert "sslmode=verify-full" in url and f"sslrootcert={_CA_PATH}" in url


@pytest.mark.parametrize("values", _ENVS)
def test_cache_valkey_has_no_plaintext_listener_or_port(values):
    docs = _docs(values)
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "redis")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    cmd = c["command"]
    assert cmd[cmd.index("--port") + 1] == "0", "plaintext listener must be disabled (--port 0)"
    assert "--tls-port" in cmd and "--tls-auth-clients" in cmd
    assert cmd[cmd.index("--tls-protocols") + 1] == "TLSv1.2 TLSv1.3"
    assert cmd[cmd.index("--tls-session-caching") + 1] == "yes"
    assert [p["name"] for p in c["ports"]] == ["valkey-tls"], "no plaintext containerPort may be declared"
    svc = _svc(docs, "redis")
    assert [p["port"] for p in svc["spec"]["ports"]] == [6380], "cache Service must expose the TLS port only"
    for probe in ("livenessProbe", "readinessProbe"):
        assert "--tls" in " ".join(c[probe]["exec"]["command"])


@pytest.mark.parametrize("values", _ENVS)
def test_every_cache_url_is_rediss_with_ca_pinned(values):
    docs = _docs(values)
    sec = _secrets_doc(docs)
    urls = {k: v for k, v in sec["stringData"].items() if k.startswith(("REDIS_URL", "VALKEY_URL", "VALKEY_DURABLE_URL", "VALKEY_KV_URL"))}
    assert {"REDIS_URL", "VALKEY_URL"} <= set(urls)
    for k, v in urls.items():
        assert v.startswith("rediss://"), (k, "plaintext redis:// URL")
        if "@" in v:  # self-contained (Python) URLs must pin the CA and check the hostname
            assert "ssl_cert_reqs=required" in v and "ssl_ca_certs=/etc/waddles/ca/valkey-ca.crt" in v, k
            assert "ssl_check_hostname=true" in v, k
    cfg = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "waddlebot-config")
    assert cfg["data"]["REDIS_PORT"] == "6380"


@pytest.mark.parametrize("values", _ENVS)
def test_python_redis_clients_mount_the_valkey_ca(values):
    n = 0
    for name, c, spec in _containers(_docs(values)):
        refs = [r.get("secretRef", {}).get("name") for r in c.get("envFrom", [])]
        if not any(r and r.endswith("-secrets") for r in refs):
            continue
        n += 1
        vols = {v["name"] for v in spec.get("volumes", [])}
        mounts = {m["name"] for m in c.get("volumeMounts", [])}
        assert "valkey-ca" in vols and "valkey-ca" in mounts, f"{name} reads rediss://...ca=/etc/waddles/ca but never mounts it"
    assert n >= 8


@pytest.mark.parametrize("values", _ENVS)
def test_every_s3_client_is_https_with_ca(values):
    docs = _docs(values)
    cfg = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "waddlebot-config")
    for k in ("S3_ENDPOINT_URL", "MINIO_ENDPOINT"):
        assert cfg["data"][k] == f'https://{_fqdn(docs, "seaweedfs")}:8333', k
    n = 0
    ep_names = ("BUNDLE_BUCKET_ENDPOINT", "BUNDLE_STORAGE_ENDPOINT", "S3_ENDPOINT", "IMAGE_BUCKET_ENDPOINT")
    for name, c, spec in _containers(docs):
        env = _env(c)
        for ep in ep_names:
            if ep in env and "seaweedfs" in env[ep].get("value", ""):
                n += 1
                assert env[ep]["value"].startswith("https://"), (name, ep, env[ep]["value"])
                assert _mounts_ca(c, spec), name
                if ep == "BUNDLE_BUCKET_ENDPOINT":
                    assert env["BUNDLE_BUCKET_CA_FILE"]["value"] == _CA_PATH, name
                else:
                    assert env["S3_CA_FILE"]["value"] == _CA_PATH and env["AWS_CA_BUNDLE"]["value"] == _CA_PATH, name
    assert n >= 4, f"only {n} S3 client endpoints found"
    blob = yaml.dump(docs)
    assert not re.findall(r"http://[^\s\"']*seaweedfs[^\s\"']*:8333", blob), "plaintext S3 URL survived"


@pytest.mark.parametrize("values", _ENVS)
def test_seaweedfs_serves_s3_over_tls_and_probes_verify(values):
    docs = _docs(values)
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "seaweedfs")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    script = c["command"][-1]
    assert "-s3.cert.file=/etc/seaweedfs-tls/tls.crt" in script and "-s3.key.file=/etc/seaweedfs-tls/tls.key" in script
    for probe in ("livenessProbe", "readinessProbe"):
        cmd = " ".join(c[probe]["exec"]["command"])
        assert "https://" in cmd and "--cacert /etc/seaweedfs-tls/ca.crt" in cmd and "http://" not in cmd
        assert " -k" not in cmd and "--insecure" not in cmd
    job = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "seaweedfs-bucket-init")
    jc = job["spec"]["template"]["spec"]["containers"][0]
    assert 'ENDPOINT="https://' in jc["command"][-1]
    assert _mounts_ca(jc, job["spec"]["template"]["spec"])
