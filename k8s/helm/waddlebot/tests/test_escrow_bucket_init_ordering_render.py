"""Fresh-install ordering: SeaweedFS bucket init (escrow bucket included) runs BEFORE hub-api.

regression: alpha fresh install deadlocked (2026-11-27). The bucket-init Job was a
post-install/post-upgrade hook; hub-api's platform vault refuses to boot without the escrow bucket
(`escrow_unavailable`), so `helm upgrade --wait` never completed and the post hook never ran.

Asserts, per env: the bucket init is hub-api's initContainer (rootless, read-only rootfs, limits,
CA mounted, credentials only in the init container), no hook Job duplicates it, no post hook
creates buckets, every post-hook resource is a known, reviewed one, Tetragon gives the init
container its OWN allowlist (hub-api's never gains a shell), and the CNPs allow hub-api -> S3.
Zero documents / zero containers examined is a failure.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM") or shutil.which("helm")
_INIT = "seaweedfs-bucket-init"
_SSE = "infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek"

# (values files, extra --set) -- base needs the SSE secret outside alpha.
_ENVS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    ((), (_SSE,)),
    (("values-alpha.yaml",), ()),
    (("values-alpha.yaml", "values-dal2alpha.yaml"), ()),
]
_IDS = ["base", "alpha", "dal2alpha"]

# Every post-install hook resource must be listed here with why it may run after `--wait`.
# A new entry means: prove no Deployment needs it to become Ready, or move it before boot.
_KNOWN_POST_HOOKS: dict[str, str] = {
    # KNOWN fresh-install boot dependency (follow-up): hub-api /ready waits for alembic head,
    # which only this post-install Job produces -> `--wait` can still deadlock on an empty DB.
    "waddlebot-db-migrate": "schema migrate (KNOWN boot dependency, follow-up)",
    "waddlebot-db-migrate-secret": "db-migrate hook credentials",
    "db-migrate": "schema migrate (KNOWN boot dependency, follow-up)",
    "core-bundle-seeder": "seeds first-party bundles; hub-api boots without it",
    "hub-admin-seed": "seeds the first admin; hub-api boots without it",
    "bundle-signing-backfill": "operator-run one-off; default disabled",
    "posthog-bootstrap": "alpha-local PostHog admin/flags; consumers read it optional: true",
}


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
    deps = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("hub-api-v3")]
    assert len(deps) == 1, f"expected one hub-api Deployment, got {len(deps)}"
    return deps[0]["spec"]["template"]["spec"]


def _hooks(doc: dict[str, Any]) -> list[str]:
    raw = (doc["metadata"].get("annotations") or {}).get("helm.sh/hook", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


@pytest.fixture(scope="module", params=_ENVS, ids=_IDS)
def docs(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    return _render(*request.param)


def test_bucket_init_is_hub_api_first_init_container(docs: list[dict[str, Any]]) -> None:
    pod = _hub_api(docs)
    inits = pod.get("initContainers") or []
    assert inits and inits[0]["name"] == _INIT, [c["name"] for c in inits]
    c = inits[0]
    script = c["command"][-1]
    escrow = re.search(r'"(waddles-platform-escrow[^"]*|[^"]*escrow[^"]*)"', script)
    assert escrow, "escrow bucket not in the bucket-init loop"
    assert "head-bucket" in script and "create-bucket" in script  # create-if-absent
    assert "buckets verified" in script and "exit 1" in script     # fails loud -> kubelet retries
    sc = c["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"]["cpu"] and c["resources"]["limits"]["memory"]
    mounts = {m["name"] for m in c.get("volumeMounts", [])}
    assert mounts & {v["name"] for v in pod["volumes"]}, "CA mount has no backing volume"
    keys = {e["valueFrom"]["secretKeyRef"]["key"] for e in c["env"] if "valueFrom" in e}
    assert keys == {"BUCKET_INIT_S3_ACCESS_KEY_ID", "BUCKET_INIT_S3_SECRET_ACCESS_KEY"}


def test_admin_bucket_credentials_never_reach_hub_api_container(docs: list[dict[str, Any]]) -> None:
    pod = _hub_api(docs)
    assert pod["containers"], "zero hub-api containers examined"
    for c in pod["containers"]:
        blob = yaml.safe_dump(c)
        assert "BUCKET_INIT_S3" not in blob, f"admin S3 credential in long-running container {c['name']}"


def test_no_bucket_creation_behind_a_post_hook(docs: list[dict[str, Any]]) -> None:
    jobs = [d for d in docs if d["kind"] == "Job"]
    assert jobs, "zero Jobs examined"
    assert not [d for d in jobs if d["metadata"]["name"] == _INIT], "bucket-init hook Job still rendered"
    for d in jobs:
        if any(h.startswith("post-") for h in _hooks(d)):
            assert "create-bucket" not in yaml.safe_dump(d), f"post hook {d['metadata']['name']} creates buckets"


def test_every_post_install_hook_is_known(docs: list[dict[str, Any]]) -> None:
    post = [d for d in docs if "post-install" in _hooks(d)]
    assert post, "zero post-install hooks examined"
    unknown = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in post
        if not any(d["metadata"]["name"].endswith(k) or k in d["metadata"]["name"] for k in _KNOWN_POST_HOOKS)
    ]
    assert not unknown, f"unreviewed post-install hooks (could gate boot under --wait): {unknown}"
    # Nothing a Deployment mounts/references may be post-install-only.
    post_names = {d["metadata"]["name"] for d in post if d["kind"] in ("Secret", "ConfigMap", "PersistentVolumeClaim")}
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet"):
            continue
        blob = yaml.safe_dump(d["spec"]["template"]["spec"])
        for n in post_names:
            assert f"name: {n}\n" not in blob, f"{d['metadata']['name']} needs post-install hook resource {n}"


def test_hub_api_disabled_falls_back_to_hook_job() -> None:
    docs = _render(("values-alpha.yaml",), ("pipeline.hubApi.enabled=false",))
    jobs = [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == _INIT]
    assert len(jobs) == 1 and "post-install" in _hooks(jobs[0])


def test_tetragon_scopes_bucket_init_allowlist_to_its_container() -> None:
    docs = _render(("values-alpha.yaml", "values-dal2alpha.yaml"), ())
    tps = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "TracingPolicyNamespaced"}
    main = tps["waddlebot-hub-api-exec-allowlist"]
    init = tps[f"waddlebot-hub-api-{_INIT}-exec-allowlist"]
    assert main["containerSelector"]["matchExpressions"] == [
        {"key": "name", "operator": "NotIn", "values": [_INIT]}
    ]
    assert init["containerSelector"]["matchExpressions"] == [{"key": "name", "operator": "In", "values": [_INIT]}]
    main_vals = [v for m in main["kprobes"][0]["selectors"][0]["matchArgs"] for v in m["values"]]
    assert "/bin/sh" not in main_vals and "/usr/local/bin/aws" not in main_vals
    init_args = init["kprobes"][0]["selectors"][0]["matchArgs"]
    allowed = {v for m in init_args if m["operator"] == "NotEqual" for v in m["values"]}
    for b in ("/bin/sh", "/usr/local/bin/aws", "/usr/bin/timeout", "/usr/bin/date",
              "/usr/bin/grep", "/usr/bin/tail", "/usr/bin/sleep"):
        assert b in allowed, f"{b} missing from bucket-init allowlist"


def test_cnp_allows_hub_api_to_seaweedfs_s3() -> None:
    docs = _render(("values-alpha.yaml", "values-dal2alpha.yaml"), ())
    cnps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "CiliumNetworkPolicy"}
    ns = next(d for d in docs if d["kind"] == "Deployment")["metadata"].get("namespace")
    hub = cnps["waddlebot-hub-api-policy"]["spec"]
    if "egress" in hub:  # egress default-deny on: own-namespace endpoints must be reachable
        assert any(
            "toEndpoints" in r and any("io.kubernetes.pod.namespace" in e.get("matchLabels", {}) for e in r["toEndpoints"])
            for r in hub["egress"]
        ), "hub-api egress has no own-namespace rule (SeaweedFS unreachable)"
    sw = cnps["waddlebot-seaweedfs-policy"]["spec"]
    s3_ok = any(
        any(p["port"] == "8333" for tp in r.get("toPorts", []) for p in tp["ports"])
        for r in sw.get("ingress", [])
    )
    assert s3_ok, "seaweedfs ingress does not admit same-namespace S3 (8333)"
    assert ns is None or isinstance(ns, str)
