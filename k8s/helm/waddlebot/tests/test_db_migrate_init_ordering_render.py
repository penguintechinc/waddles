"""Fresh-install ordering: schema migration runs BEFORE hub-api's main container.

regression: fresh install (new env / wiped DB) deadlocked under `helm upgrade --install --wait`
(2026-11-27). db-migrate was a post-install hook; hub-api `/ready` waits for the alembic head
revision, so --wait never completed and the post hook never ran.

Asserts, per env: db-migrate is a hub-api initContainer (advisory-locked wrapper, rootless,
read-only rootfs, limits, CA mounted), the migrator DSN Secret is only in that init container,
no db-migrate resource sits behind a post-install hook, the hook Job is pre-upgrade only and
ordered after its SA/secrets, Tetragon gives the init container its OWN allowlist, and hub-api's
CNP admits Postgres egress. Zero documents / zero containers examined is a failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM") or shutil.which("helm")
_INIT = "db-migrate"
_SECRET = "waddlebot-db-migrate-secret"
_SSE = "infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek"

_ENVS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    ((), (_SSE,)),
    (("values-alpha.yaml",), ()),
    (("values-alpha.yaml", "values-dal2alpha.yaml"), ()),
]
_IDS = ["base", "alpha", "dal2alpha"]


def _render(values: tuple[str, ...], sets: tuple[str, ...]) -> list[dict[str, Any]]:
    """Render the chart and return the non-empty documents."""
    if not _HELM:
        pytest.fail("helm CLI not on PATH -- this gate must never silently skip")
    argv = [_HELM, "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0"]
    for v in values:
        argv += ["--values", str(_CHART / v)]
    for s in sets:
        argv += ["--set", s]
    out = subprocess.run(argv, capture_output=True, text=True, check=False)  # fixed argv, test-only
    assert out.returncode == 0, out.stderr
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    assert docs, "zero documents rendered"
    return docs


def _hub_api(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return hub-api's pod spec (exactly one Deployment expected)."""
    deps = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("hub-api-v3")]
    assert len(deps) == 1, f"expected one hub-api Deployment, got {len(deps)}"
    return deps[0]["spec"]["template"]["spec"]


def _hooks(doc: dict[str, Any]) -> list[str]:
    """Parse a resource's helm.sh/hook annotation into phases."""
    raw = (doc["metadata"].get("annotations") or {}).get("helm.sh/hook", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _weight(doc: dict[str, Any]) -> int:
    """Return a hook resource's helm.sh/hook-weight (default 0)."""
    return int((doc["metadata"].get("annotations") or {}).get("helm.sh/hook-weight", "0"))


@pytest.fixture(scope="module", params=_ENVS, ids=_IDS)
def docs(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """Rendered documents for one environment."""
    return _render(*request.param)


def test_db_migrate_is_hub_api_init_container(docs: list[dict[str, Any]]) -> None:
    pod = _hub_api(docs)
    inits = [c["name"] for c in pod.get("initContainers") or []]
    assert _INIT in inits, inits
    assert pod["containers"] and pod["containers"][0]["name"] == "hub-api"
    c = next(c for c in pod["initContainers"] if c["name"] == _INIT)
    assert c["command"] == ["/usr/local/bin/python3", "/app/migrate_locked.py", "/app/run.sh"]
    assert "migrations" in c["image"]
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"]["cpu"] and c["resources"]["limits"]["memory"]
    assert c["envFrom"] == [{"secretRef": {"name": _SECRET}}]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert int(env["MIGRATE_LOCK_TIMEOUT_SECONDS"]) > 0 and int(env["MIGRATE_DB_WAIT_SECONDS"]) > 0
    vols = {v["name"] for v in pod["volumes"]}
    mounts = {m["name"] for m in c["volumeMounts"]}
    assert mounts and mounts <= vols, f"init mounts without backing volume: {mounts - vols}"


def test_migrator_credentials_never_reach_hub_api_container(docs: list[dict[str, Any]]) -> None:
    pod = _hub_api(docs)
    assert pod["containers"], "zero hub-api containers examined"
    for c in pod["containers"]:
        assert _SECRET not in yaml.safe_dump(c), f"migrator Secret in long-running container {c['name']}"


def test_no_db_migrate_behind_a_post_install_hook(docs: list[dict[str, Any]]) -> None:
    hooked = [d for d in docs if _hooks(d)]
    assert hooked, "zero hook resources examined"
    post = [f"{d['kind']}/{d['metadata']['name']}" for d in hooked
            if "post-install" in _hooks(d) and "db-migrate" in d["metadata"]["name"]]
    assert not post, f"db-migrate resources still post-install: {post}"


def test_hook_job_is_pre_upgrade_only_and_ordered(docs: list[dict[str, Any]]) -> None:
    by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    job = by_name[("Job", "waddlebot-db-migrate")]
    assert _hooks(job) == ["pre-upgrade"]
    assert job["spec"]["template"]["spec"]["containers"][0]["command"][1] == "/app/migrate_locked.py"
    sa = by_name[("ServiceAccount", "waddlebot-db-migrate")]
    assert set(_hooks(sa)) == {"pre-install", "pre-upgrade"} and _weight(sa) < _weight(job)
    prov = [d for d in docs if d["kind"] == "Job" and "pre-upgrade" in _hooks(d)
            and _SECRET in (d["metadata"].get("annotations") or {}).get("waddles.penguintech.io/provisions-secrets", "")]
    assert len(prov) == 1, "exactly one hook must provision the migrator Secret"
    assert "pre-install" in _hooks(prov[0]) and _weight(prov[0]) < _weight(job)


def test_migrations_disabled_renders_neither_call_site() -> None:
    docs = _render(("values-alpha.yaml",), ("pipeline.migrationsJob.enabled=false",))
    pod = _hub_api(docs)
    assert _INIT not in [c["name"] for c in pod.get("initContainers") or []]
    assert not [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "waddlebot-db-migrate"]


def test_tetragon_scopes_db_migrate_allowlist_to_its_container() -> None:
    docs = _render(("values-alpha.yaml", "values-dal2alpha.yaml"), ())
    tps = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "TracingPolicyNamespaced"}
    main = tps["waddlebot-hub-api-exec-allowlist"]
    init = tps[f"waddlebot-hub-api-{_INIT}-exec-allowlist"]
    excluded = main["containerSelector"]["matchExpressions"][0]
    assert excluded["operator"] == "NotIn" and _INIT in excluded["values"]
    assert init["containerSelector"]["matchExpressions"] == [{"key": "name", "operator": "In", "values": [_INIT]}]
    main_vals = [v for m in main["kprobes"][0]["selectors"][0]["matchArgs"] for v in m["values"]]
    assert "/bin/sh" not in main_vals and "/usr/local/bin/alembic" not in main_vals
    allowed = {v for m in init["kprobes"][0]["selectors"][0]["matchArgs"] if m["operator"] == "NotEqual"
               for v in m["values"]}
    for b in ("/usr/local/bin/python3", "/usr/local/bin/alembic", "/bin/sh", "/usr/bin/sleep"):
        assert b in allowed, f"{b} missing from db-migrate allowlist"


def test_cnp_allows_hub_api_to_postgres() -> None:
    docs = _render(("values-alpha.yaml", "values-dal2alpha.yaml"), ())
    cnps = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "CiliumNetworkPolicy"}
    hub = cnps["waddlebot-hub-api-policy"]
    assert "egress" in hub, "hub-api egress is not default-deny (expected an explicit allowlist)"
    same_ns = any(
        "toEndpoints" in r and any("io.kubernetes.pod.namespace" in e.get("matchLabels", {}) for e in r["toEndpoints"])
        and not r.get("toPorts")
        for r in hub["egress"]
    )
    pg_port = any(p["port"] == "5432" for r in hub["egress"] for tp in r.get("toPorts", []) for p in tp["ports"])
    assert same_ns or pg_port, "hub-api egress admits neither own-namespace nor 5432 (Postgres unreachable)"
    pg = cnps["waddlebot-postgres-policy"]
    assert pg.get("ingress"), "zero postgres ingress rules examined"
    assert any(
        any("io.kubernetes.pod.namespace" in e.get("matchLabels", {}) for e in r.get("fromEndpoints", []))
        and any(p["port"] == "5432" for tp in r.get("toPorts", []) for p in tp["ports"])
        for r in pg["ingress"]
    ), "postgres ingress does not admit same-namespace 5432 (hub-api db-migrate init)"
