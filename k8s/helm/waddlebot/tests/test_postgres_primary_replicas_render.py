"""Helm-template assertions for the default PostgreSQL topology: PRIMARY + READ-REPLICAs.

Owner requirement: default Postgres = primary + read replicas, a Service load-balancing across the
replicas, and reads defaulting to the replicas wherever possible. For every values file, under both
Helm 3 and Helm 4, the chart must render:

* a primary StatefulSet `postgres` (replicas 1, RollingUpdate, existing PVC mounted by `claimName`,
  NO volumeClaimTemplates) -- never a Deployment, and never a RollingUpdate surge on a shared volume;
* the standalone `postgres-data` PVC carrying `helm.sh/resource-policy: keep` (alpha migration);
* a replica StatefulSet `postgres-replica` (count from values, volumeClaimTemplates, basebackup init,
  streaming/lag readiness) and, for count>=2, a PodDisruptionBudget;
* a `<service>-ro` Service selecting ONLY replica pods, with its FQDN in the server-cert SANs;
* DATABASE_REPLICA_URL / DATABASE_READ_REPLICA_URL / DB_READER_HOST pointing at `-ro`;
* a loud render failure when replicas are switched off without `readReplicas.optOut=true`.

Every test asserts how many values files/documents it examined -- zero is a failure, never a pass.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_HELM3 = str(Path.home() / ".local" / "bin" / "helm3")
_HELMS = {"helm4": "helm", "helm3": _HELM3}
# (values file, extra --set). values.yaml / values-ci.yaml need the alpha tier to render the (unrelated)
# SeaweedFS encryption guard; values-local runs an EXTERNAL database (no in-chart Postgres).
_ENVS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("values.yaml", ("global.deploymentTier=alpha",)),
    ("values-alpha.yaml", ()),
    ("values-beta.yaml", ()),
    ("values-gamma.yaml", ()),
    ("values-production.yaml", ()),
    ("values-dal2alpha.yaml", ()),
    ("values-ci.yaml", ("global.deploymentTier=alpha",)),
    ("values-local.yaml", ()),
)
_IN_CHART_PG = tuple(e for e in _ENVS if e[0] != "values-local.yaml")
_CASES = [(h, e) for h in _HELMS for e in _ENVS]
_PG_CASES = [(h, e) for h in _HELMS for e in _IN_CHART_PG]
_ids = lambda c: f"{c[0]}-{c[1][0]}"  # noqa: E731 -- pytest id callback


def _run(helm: str, values: str, extra: tuple[str, ...], more: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Run `helm template` for one values file; return the raw process result."""
    binary = _HELMS[helm]
    if not shutil.which(binary) and not Path(binary).exists():
        pytest.fail(f"{helm} binary not found ({binary})")
    argv = [binary, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    if values != "values.yaml":
        argv += ["--values", str(_CHART_DIR / values)]
    for item in (*extra, *more):
        argv += ["--set", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv


@functools.lru_cache(maxsize=None)
def _render(helm: str, values: str, extra: tuple[str, ...]) -> list[dict[str, Any]]:
    """Render and parse a values file; a failure or zero documents fails the test."""
    result = _run(helm, values, extra)
    if result.returncode != 0:
        pytest.fail(f"{helm} template {values} failed:\n{result.stderr[-2000:]}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, f"{helm} {values}: zero documents -- cannot be a pass"
    return docs


def _kind(docs: list[dict[str, Any]], kind: str, name: str | None = None) -> list[dict[str, Any]]:
    """Documents of `kind` (optionally one `metadata.name`)."""
    return [d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The single `kind`/`name` document."""
    found = _kind(docs, kind, name)
    assert len(found) == 1, f"expected exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Container env as {name: entry}."""
    return {e["name"]: e for e in container.get("env", []) or []}


def _by_suffix(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    """The single Deployment whose name ends with `-suffix`."""
    found = [d for d in _kind(docs, "Deployment") if d["metadata"]["name"].endswith(f"-{suffix}")]
    assert len(found) == 1, f"expected one {suffix} Deployment, got {len(found)}"
    return found[0]


def _expected_count(values: str) -> int:
    """readReplicas.count the values file (over the chart default) is expected to render."""
    base = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())["infrastructure"]["postgresql"]["readReplicas"]["count"]
    over = yaml.safe_load((_CHART_DIR / values).read_text()) or {}
    return int(((over.get("infrastructure") or {}).get("postgresql") or {}).get("readReplicas", {}).get("count", base))


def test_matrix_is_nonempty() -> None:
    """The (helm x values) matrix this module parametrises over is never empty."""
    assert len(_CASES) == 2 * len(_ENVS) >= 14


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_primary_is_single_writer_statefulset_on_existing_pvc(case) -> None:
    """Primary = StatefulSet replicas 1 on the standalone PVC by claimName; no Deployment, no surge."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    assert not _kind(docs, "Deployment", "postgres"), "postgres must not be a Deployment any more"
    sts = _one(docs, "StatefulSet", "postgres")
    spec = sts["spec"]
    assert spec["replicas"] == 1
    assert spec["updateStrategy"] == {"type": "RollingUpdate"}  # 1-replica STS: delete-then-create, never a surge
    assert "volumeClaimTemplates" not in spec, "primary must reuse the existing PVC, not template a new one"
    pod = spec["template"]["spec"]
    claim = next(v for v in pod["volumes"] if v["name"] == "postgres-data")
    assert claim["persistentVolumeClaim"]["claimName"] == "postgres-data"
    pvc = _one(docs, "PersistentVolumeClaim", "postgres-data")
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    assert spec["selector"]["matchLabels"]["app.kubernetes.io/role"] == "primary"
    # Exactly one postgres-data user per volume: no workload other than the primary mounts the primary PVC.
    mounting = [
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") in ("Deployment", "StatefulSet")
        and any(
            v.get("persistentVolumeClaim", {}).get("claimName") == "postgres-data"
            for v in d["spec"]["template"]["spec"].get("volumes", [])
        )
    ]
    assert mounting == ["postgres"], mounting
    args = pod["containers"][0]["args"]
    for want in ("wal_level=replica", "hot_standby=on"):
        assert want in args, want
    hba = _one(docs, "ConfigMap", "postgres-hba")["data"]["pg_hba.conf"]
    assert "hostssl replication waddles_replicator 0.0.0.0/0 scram-sha-256" in hba


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_replica_statefulset_and_pdb(case) -> None:
    """Replica STS count follows values, owns volumeClaimTemplates, basebackup init, lag-aware readiness."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    count = _expected_count(values)
    assert count >= 1
    sts = _one(docs, "StatefulSet", "postgres-replica")
    spec = sts["spec"]
    assert spec["replicas"] == count
    assert [t["metadata"]["name"] for t in spec["volumeClaimTemplates"]] == ["postgres-data"]
    pod = spec["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["name"] == "basebackup"
    assert init["image"] == pod["containers"][0]["image"]  # same digest-pinned image
    assert pod["affinity"]["podAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    env = {e["name"]: e for e in init["env"]}
    assert env["PGSSLMODE"]["value"] == "verify-full"
    assert "secretKeyRef" in env["REPL_PASSWORD"]["valueFrom"]
    probe = pod["containers"][0]["readinessProbe"]["exec"]["command"]
    assert probe == ["/bin/sh", "/scripts/ready.sh"]
    assert "startupProbe" in pod["containers"][0]
    scripts = _one(docs, "ConfigMap", "postgres-replication-scripts")["data"]
    assert "pg_stat_wal_receiver" in scripts["ready.sh"]
    assert "pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()" in scripts["ready.sh"]
    assert "[ -s \"$PGDATA/PG_VERSION\" ]" in scripts["basebackup.sh"]  # only when PGDATA is empty
    assert "-S \"replica_${ord}\"" in scripts["basebackup.sh"]
    pdbs = _kind(docs, "PodDisruptionBudget", "postgres-replica")
    if count >= 2:
        assert len(pdbs) == 1 and pdbs[0]["spec"]["minAvailable"] == 1
    else:
        assert not pdbs


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_ro_service_selects_only_replicas_and_is_in_sans(case) -> None:
    """`<service>-ro` selects replica pods only; the primary Service selects the primary only."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    ro = next(d for d in _kind(docs, "Service") if d["metadata"]["name"].endswith("-ro") and "postgres" in d["metadata"]["name"])
    primary = next(d for d in _kind(docs, "Service") if d["metadata"]["name"] == "infra-postgres")
    assert ro["spec"]["selector"]["app.kubernetes.io/role"] == "replica"
    assert primary["spec"]["selector"]["app.kubernetes.io/role"] == "primary"
    replica_labels = _one(docs, "StatefulSet", "postgres-replica")["spec"]["template"]["metadata"]["labels"]
    assert all(replica_labels.get(k) == v for k, v in ro["spec"]["selector"].items())
    primary_labels = _one(docs, "StatefulSet", "postgres")["spec"]["template"]["metadata"]["labels"]
    assert not all(primary_labels.get(k) == v for k, v in ro["spec"]["selector"].items()), "-ro must never select the primary"
    assert ro["metadata"]["name"] == "infra-postgres-ro"


def _leaf_sans(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """{'primary': [...], 'replica': [...]} server-cert SANs, from cert-manager Certificates or the provisioning spec."""
    certs = {d["metadata"]["name"]: d["spec"]["dnsNames"] for d in _kind(docs, "Certificate") if "dnsNames" in d["spec"]}
    primary = [v for k, v in certs.items() if k.endswith("-postgres-tls")]
    replica = [v for k, v in certs.items() if k.endswith("-postgres-replica-tls")]
    if primary:
        assert len(primary) == 1 and len(replica) == 1, (list(certs),)
        return {"primary": primary[0], "replica": replica[0]}
    out: dict[str, list[str]] = {}
    for d in _kind(docs, "ConfigMap"):
        spec = d.get("data", {}).get("spec.psv")
        if not spec:
            continue
        for line in spec.splitlines():
            parts = line.split("|")
            if parts[0] == "tls_leaf" and parts[2] == "postgres.crt":
                out["primary"] = [x for x in parts[5].split(",") if x]
            if parts[0] == "tls_leaf" and parts[2] == "postgres-replica.crt":
                out["replica"] = [x for x in parts[5].split(",") if x]
    assert set(out) == {"primary", "replica"}, f"postgres leaf specs not found: {list(out)}"
    return out


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_replica_cert_sans_are_separate_from_the_writer(case) -> None:
    """Replica cert: -ro/headless/pod names only. Primary cert: primary names only. Replicas mount only their own key."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    sans = _leaf_sans(docs)
    ns = "waddlebot"
    primary_fqdn = f"infra-postgres.{ns}.svc.cluster.local"
    ro_fqdn = f"infra-postgres-ro.{ns}.svc.cluster.local"
    hl_fqdn = f"infra-postgres-replica-hl.{ns}.svc.cluster.local"
    assert primary_fqdn in sans["primary"]
    assert not any("-ro" in n or "replica" in n for n in sans["primary"]), sans["primary"]
    assert ro_fqdn in sans["replica"] and hl_fqdn in sans["replica"]
    assert f"postgres-replica-0.{hl_fqdn}" in sans["replica"]
    assert primary_fqdn not in sans["replica"] and "infra-postgres" not in sans["replica"], "replica cert must not name the writer"
    assert not any("*" in n for n in sans["primary"] + sans["replica"])
    # Mounts: primary pod -> primary key items only; replica pod -> replica key items only.
    def tls_vol(sts: str) -> dict[str, Any]:
        vols = _one(docs, "StatefulSet", sts)["spec"]["template"]["spec"]["volumes"]
        return next(v for v in vols if v["name"] == "transit-tls")["secret"]
    prim, rep = tls_vol("postgres"), tls_vol("postgres-replica")
    prim_keys = {i["key"] for i in prim["items"]}
    rep_keys = {i["key"] for i in rep["items"]}
    assert not any("replica" in k for k in prim_keys), prim_keys
    assert rep_keys and all(("replica" in k) or k in ("tls.crt", "tls.key") for k in rep_keys), rep_keys
    if prim["secretName"] == rep["secretName"]:  # static mode: one Secret, disjoint projected keys
        assert prim_keys.isdisjoint(rep_keys), (prim_keys, rep_keys)
    else:  # cert-manager mode: a dedicated replica Secret
        assert rep["secretName"].endswith("-postgres-replica-tls")


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_replica_uses_a_passfile_not_a_password_in_conninfo(case) -> None:
    """Replica reads the password from a 0600 passfile on tmpfs; no PGPASSWORD env; the init script rewrites primary_conninfo."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    pod = _one(docs, "StatefulSet", "postgres-replica")["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    env = {e["name"]: e for e in init["env"]}
    assert "PGPASSWORD" not in env and env["PGPASSFILE"]["value"] == "/pgpass/pgpass"
    assert "secretKeyRef" in env["REPL_PASSWORD"]["valueFrom"]
    assert "PGPASSWORD" not in {e["name"] for e in pod["containers"][0]["env"]}
    vol = next(v for v in pod["volumes"] if v["name"] == "pgpass")
    assert vol["emptyDir"]["medium"] == "Memory"
    script = _one(docs, "ConfigMap", "postgres-replication-scripts")["data"]["basebackup.sh"]
    assert "passfile=" in script and "chmod 600" in script and "password" in script


def _run_basebackup(tmp_path: Path, script: str, preseed: str | None) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the rendered basebackup.sh against a fake pg_basebackup; returns (result, PGDATA, passfile)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "pg_basebackup"
    # The fake mimics `pg_basebackup -R` leaking the password into primary_conninfo (the behaviour to defend against).
    fake.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do case $1 in -D) d=$2; shift;; esac; shift; done\n"
        "mkdir -p \"$d\"; echo 16 > \"$d/PG_VERSION\"\n"
        "printf \"primary_conninfo = 'host=x user=u password=LEAKED-SECRET sslmode=verify-full'\\nprimary_slot_name = 'replica_0'\\n\" > \"$d/postgresql.auto.conf\"\n"
    )
    fake.chmod(0o755)
    pgdata = tmp_path / "data" / "pgdata"
    if preseed is not None:
        pgdata.mkdir(parents=True)
        (pgdata / "PG_VERSION").write_text("16\n")
        (pgdata / "postgresql.auto.conf").write_text(preseed)
    pgpass = tmp_path / "pgpass"
    pgpass.mkdir()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin", "HOSTNAME": "postgres-replica-0", "PGDATA": str(pgdata),
        "PRIMARY_HOST": "infra-postgres.waddlebot.svc.cluster.local", "PRIMARY_PORT": "5432",
        "REPL_USER": "waddles_replicator", "REPL_PASSWORD": "LEAKED-SECRET",
        "PGSSLROOTCERT": "/etc/waddles/transit-ca/ca.crt", "PGPASSFILE": str(pgpass / "pgpass"),
    }
    res = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env, check=False)  # noqa: S603,S607
    return res, pgdata, pgpass / "pgpass"


@pytest.mark.parametrize("preseed", [None, "primary_conninfo = 'host=old password=LEAKED-SECRET'\nprimary_slot_name = 'replica_0'\n"],
                         ids=["fresh-basebackup", "already-bootstrapped-with-leaked-password"])
def test_basebackup_script_strips_password_and_writes_passfile(tmp_path: Path, preseed: str | None) -> None:
    """Executes the rendered init script: conninfo ends up password-free with passfile=, passfile is 0600 and holds the secret."""
    docs = _render("helm4", "values-alpha.yaml", ())
    script = _one(docs, "ConfigMap", "postgres-replication-scripts")["data"]["basebackup.sh"]
    res, pgdata, passfile = _run_basebackup(tmp_path, script, preseed)
    assert res.returncode == 0, res.stderr
    conf = (pgdata / "postgresql.auto.conf").read_text()
    assert "LEAKED-SECRET" not in conf and "password=" not in conf.replace("passfile=", "")
    assert "passfile=" in conf and "primary_slot_name = 'replica_0'" in conf
    assert "sslmode=verify-full" in conf and conf.count("primary_conninfo") == 1
    assert oct(passfile.stat().st_mode & 0o777) == "0o600"
    assert passfile.read_text().strip().endswith(":replication:waddles_replicator:LEAKED-SECRET")
    assert "LEAKED-SECRET" not in res.stdout + res.stderr


@pytest.mark.parametrize("helm", list(_HELMS))
def test_handover_hook_scales_legacy_deployment_with_minimal_rbac(helm: str) -> None:
    """Pre-install/pre-upgrade Job scales Deployment/postgres to 0 and waits; RBAC is resourceName-scoped; no-op when absent."""
    docs = _render(helm, "values-alpha.yaml", ())
    job = next(d for d in _kind(docs, "Job") if d["metadata"]["name"].endswith("-postgres-handover"))
    ann = job["metadata"]["annotations"]
    assert ann["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert "before-hook-creation" in ann["helm.sh/hook-delete-policy"]
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] > 300
    role = next(d for d in _kind(docs, "Role") if d["metadata"]["name"].endswith("-postgres-handover"))
    by_res = {tuple(r["resources"]): r for r in role["rules"]}
    dep = by_res[("deployments",)]
    assert dep["resourceNames"] == ["postgres"] and set(dep["verbs"]) == {"get", "patch"}
    scale = by_res[("deployments/scale",)]
    assert scale["resourceNames"] == ["postgres"] and set(scale["verbs"]) == {"get", "patch", "update"}
    pods = by_res[("pods",)]
    assert set(pods["verbs"]) == {"get", "list", "watch"} and "resourceNames" not in pods
    assert not any("delete" in r["verbs"] or "*" in r["verbs"] for r in role["rules"])
    sa_hook = next(d for d in _kind(docs, "ServiceAccount") if d["metadata"]["name"].endswith("-postgres-handover"))
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == sa_hook["metadata"]["name"]
    script = next(d for d in _kind(docs, "ConfigMap") if d["metadata"]["name"].endswith("-postgres-handover"))["data"]["handover.sh"]
    assert "--ignore-not-found" in script and "scale deployment/postgres --replicas=0" in script
    assert "!app.kubernetes.io/role" in script and "exit 1" in script


def test_handover_script_noop_scales_waits_and_times_out(tmp_path: Path) -> None:
    """Runs the hook script against a fake kubectl: absent -> no-op; present -> scale + wait; stuck pods -> non-zero exit."""
    docs = _render("helm4", "values-alpha.yaml", ())
    script = next(d for d in _kind(docs, "ConfigMap") if d["metadata"]["name"].endswith("-postgres-handover"))["data"]["handover.sh"]
    sfile = tmp_path / "handover.sh"
    sfile.write_text(script)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "kubectl.log"
    (bindir / "kubectl").write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "case \"$*\" in\n"
        "  *'get deployment postgres'*) [ \"$DEP\" = present ] && echo deployment.apps/postgres; exit 0;;\n"
        "  *'get pods'*) [ \"$PODS\" = stuck ] && echo pod/postgres-abc; exit 0;;\n"
        "  *scale*) exit 0;;\n"
        "esac\n"
    )
    (bindir / "kubectl").chmod(0o755)
    base = {"PATH": f"{bindir}:/usr/bin:/bin", "NAMESPACE": "waddlebot", "TIMEOUT_SECONDS": "1"}
    run = lambda **kw: subprocess.run(["bash", str(sfile)], capture_output=True, text=True, env={**base, **kw}, check=False)  # noqa: E731,S603,S607
    absent = run(DEP="absent", PODS="none")
    assert absent.returncode == 0 and "nothing to do" in absent.stdout and "scale" not in log.read_text()
    ok = run(DEP="present", PODS="none")
    assert ok.returncode == 0 and "scale deployment/postgres --replicas=0" in log.read_text()
    stuck = run(DEP="present", PODS="stuck")
    assert stuck.returncode == 1 and "FAILED" in stuck.stderr


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_changelog_consumer_stays_on_primary_and_ingest_has_no_replica(case) -> None:
    """DB_WATERMARK_HOST (all changelog-consumer reads) is the primary FQDN, never -ro; svc-ingest has no replica DSN."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra + ("pipeline.rustDataPlane.enabled=true",))
    for suffix in ("svc-process-rust", "svc-action-rust"):
        env = _env(_by_suffix(docs, suffix)["spec"]["template"]["spec"]["containers"][0])
        wm = env["DB_WATERMARK_HOST"]["value"]
        assert wm == "infra-postgres.waddlebot.svc.cluster.local", (suffix, wm)
        assert "-ro" not in wm and wm != env["DB_READER_HOST"]["value"], suffix
        assert env["DB_READER_HOST"]["value"] == "infra-postgres-ro.waddlebot.svc.cluster.local"
    # svc-ingest display-name/detokenization stays on the PRIMARY (erasure safety): no replica DSN at all.
    ing = _env(_by_suffix(docs, "svc-ingest-rust")["spec"]["template"]["spec"]["containers"][0])
    assert "DATABASE_REPLICA_URL" not in ing and "DATABASE_READ_REPLICA_URL" not in ing
    assert "@infra-postgres.waddlebot.svc.cluster.local:5432/" in ing["DATABASE_URL"]["value"]
    assert "-ro" not in ing["DATABASE_URL"]["value"]


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_reads_point_at_ro(case) -> None:
    """hub-api, svc-process and svc-action read from the -ro FQDN; writes stay on the primary."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra + ("pipeline.rustDataPlane.enabled=true",))
    ro_host = "infra-postgres-ro.waddlebot.svc.cluster.local"
    primary_host = "infra-postgres.waddlebot.svc.cluster.local"
    hub = _env(_by_suffix(docs, "hub-api-v3")["spec"]["template"]["spec"]["containers"][0])
    assert f"@{ro_host}:5432/" in hub["DATABASE_READ_REPLICA_URL"]["value"]
    assert "$(DB_PASSWORD)" in hub["DATABASE_READ_REPLICA_URL"]["value"]  # never a literal password
    assert f"@{primary_host}:5432/" in hub["DATABASE_URL"]["value"]
    names = [e["name"] for e in _env_list(_by_suffix(docs, "hub-api-v3"))]
    assert names.index("DB_PASSWORD") < names.index("DATABASE_READ_REPLICA_URL")  # k8s only expands earlier entries
    for suffix in ("svc-process-rust", "svc-action-rust"):
        env = _env(_by_suffix(docs, suffix)["spec"]["template"]["spec"]["containers"][0])
        assert f"@{ro_host}:5432/" in env["DATABASE_REPLICA_URL"]["value"], suffix
        assert env["DB_READER_HOST"]["value"] == ro_host, suffix
        assert f"@{primary_host}:5432/" in env["DATABASE_URL"]["value"], suffix


def _env_list(dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered env list of a Deployment's first container."""
    return dep["spec"]["template"]["spec"]["containers"][0].get("env", []) or []


@pytest.mark.parametrize("helm", list(_HELMS))
def test_explicit_host_overrides_ro(helm: str) -> None:
    """readReplicas.host wins over the computed -ro FQDN (external replica)."""
    docs = _render(helm, "values-alpha.yaml", ("pipeline.rustDataPlane.enabled=true",))
    assert docs  # warm cache; the override render below is the assertion
    result = _run(helm, "values-alpha.yaml", ("pipeline.rustDataPlane.enabled=true",),
                  ("infrastructure.postgresql.readReplicas.host=ext-replica.example.internal",))
    assert result.returncode == 0, result.stderr[-1500:]
    out = [d for d in yaml.safe_load_all(result.stdout) if d]
    env = _env(_by_suffix(out, "svc-process-rust")["spec"]["template"]["spec"]["containers"][0])
    assert "@ext-replica.example.internal:5432/" in env["DATABASE_REPLICA_URL"]["value"]


@pytest.mark.parametrize("helm", list(_HELMS))
@pytest.mark.parametrize(
    "overrides",
    [
        ("infrastructure.postgresql.readReplicas.count=0",),
        ("infrastructure.postgresql.readReplicas.enabled=false",),
    ],
    ids=["count0", "disabled"],
)
def test_opt_out_requires_explicit_flag(helm: str, overrides: tuple[str, ...]) -> None:
    """Replicas off without optOut=true fails the render; with it, no replica objects and reads use the primary."""
    bad = _run(helm, "values-alpha.yaml", (), overrides)
    assert bad.returncode != 0 and "optOut" in bad.stderr, bad.stderr[-800:]
    ok = _run(helm, "values-alpha.yaml", ("pipeline.rustDataPlane.enabled=true",),
              (*overrides, "infrastructure.postgresql.readReplicas.optOut=true"))
    assert ok.returncode == 0, ok.stderr[-1500:]
    docs = [d for d in yaml.safe_load_all(ok.stdout) if d]
    assert not _kind(docs, "StatefulSet", "postgres-replica")
    assert not [d for d in _kind(docs, "Service") if d["metadata"]["name"].endswith("-ro")]
    env = _env(_by_suffix(docs, "svc-process-rust")["spec"]["template"]["spec"]["containers"][0])
    assert "DATABASE_REPLICA_URL" not in env
    assert env["DB_READER_HOST"]["value"] == "infra-postgres.waddlebot.svc.cluster.local"
    assert "wal_level=replica" not in _one(docs, "StatefulSet", "postgres")["spec"]["template"]["spec"]["containers"][0]["args"]


def test_local_values_run_no_in_chart_postgres() -> None:
    """values-local uses an external database: neither primary nor replica objects render."""
    docs = _render("helm4", "values-local.yaml", ())
    assert not _kind(docs, "StatefulSet", "postgres") and not _kind(docs, "StatefulSet", "postgres-replica")
    assert not _kind(docs, "Deployment", "postgres")


@pytest.mark.parametrize("case", _PG_CASES, ids=_ids)
def test_replication_secret_is_not_in_shared_secrets(case) -> None:
    """The replication password lives in its own Secret, consumed only by the postgres pods."""
    helm, (values, extra) = case
    docs = _render(helm, values, extra)
    users = set()
    for d in docs:
        if d.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        tpl = d["spec"]["template"] if d["kind"] != "CronJob" else d["spec"]["jobTemplate"]["spec"]["template"]
        for c in [*tpl["spec"].get("containers", []), *tpl["spec"].get("initContainers", [])]:
            if "-db-replication" in yaml.dump(c):
                users.add(d["metadata"]["name"])
            for ef in c.get("envFrom", []) or []:
                assert "db-replication" not in str(ef), f"{d['metadata']['name']} envFrom's the replication Secret"
    assert users == {"postgres", "postgres-replica"}, users
    prov = [d for d in docs if d.get("kind") == "Job" and "provisions-secrets" in str(d["metadata"].get("annotations", {}))]
    assert prov and any("-db-replication" in d["metadata"]["annotations"]["waddles.penguintech.io/provisions-secrets"] for d in prov)


@pytest.mark.parametrize("helm", list(_HELMS))
def test_libpq_clients_require_channel_binding(helm: str) -> None:
    """libpq clients of the in-chart Postgres get PGCHANNELBINDING=require (asyncpg/sqlx lack the option and ignore the env)."""
    docs = _render(helm, "values-alpha.yaml", ())
    hub = _env(_by_suffix(docs, "hub-api-v3")["spec"]["template"]["spec"]["containers"][0])
    assert hub["PGCHANNELBINDING"]["value"] == "require" and hub["PGSSLMODE"]["value"] == "verify-full"
