"""Helm-template assertions for the data plane's direct bundle-config database reads (issue #1306).

svc-process and svc-action read per-community bundle config straight from the read-only replica
(`core/bundle_config_db/src/lib.rs`) under the SAME column-scoped per-service role they
already connect as (alembic 0106). The chart must therefore:

* keep each pod's own role + `DATABASE_URL` (primary; the WARN-logged fallback) -- never a shared
  or owner credential;
* render `DATABASE_REPLICA_URL` ONLY when `infrastructure.postgresql.readReplicas.host` is set,
  for the pod's own role, with the password left to `$(DB_PASSWORD)` expansion (never a literal)
  and AFTER the `DB_PASSWORD` entry (Kubernetes only expands earlier entries).

Zero rendered Deployments / a missing pod is a hard failure, never a silent pass; a missing `helm`
is a skip locally but a FAILURE in CI.
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
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_REPLICA_HOST = "waddlebot-postgres-replica.example.internal"
_PODS = {
    "svc-process-rust": "waddles_svc_process",
    "svc-action-rust": "waddles_svc_action",
}


def _render(*extra_set: str) -> list[dict[str, Any]]:
    """Render the alpha chart with the Rust data plane on."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_ALPHA_VALUES),
        "--set",
        "pipeline.rustDataPlane.enabled=true",
    ]
    for item in extra_set:
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _env(docs: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    """The app container's `env` list of the Deployment named `*-{suffix}`."""
    found = [
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"].endswith(f"-{suffix}")
    ]
    assert len(found) == 1, (
        f"expected exactly one {suffix} Deployment, got {len(found)}"
    )
    containers = found[0]["spec"]["template"]["spec"]["containers"]
    assert containers, f"{suffix}: no containers"
    return containers[0].get("env", []) or []


@pytest.fixture(scope="module")
def default_docs() -> list[dict[str, Any]]:
    """Alpha render, no replica configured (the alpha default)."""
    return _render()


@pytest.fixture(scope="module")
def replica_docs() -> list[dict[str, Any]]:
    """Alpha render with a replica host configured."""
    return _render(f"infrastructure.postgresql.readReplicas.host={_REPLICA_HOST}")


@pytest.mark.parametrize(("pod", "role"), sorted(_PODS.items()))
def test_no_replica_host_renders_primary_only(
    default_docs: list[dict[str, Any]], pod: str, role: str
) -> None:
    """Unset host: own-role DATABASE_URL only (stage falls back to primary at WARN), no replica DSN."""
    env = _env(default_docs, pod)
    names = [e["name"] for e in env]
    assert "DATABASE_REPLICA_URL" not in names
    by_name = {e["name"]: e for e in env}
    assert by_name["DB_USER"]["value"] == role
    assert by_name["DATABASE_URL"]["value"].startswith(
        f"postgresql://{role}:$(DB_PASSWORD)@"
    )


@pytest.mark.parametrize(("pod", "role"), sorted(_PODS.items()))
def test_replica_host_renders_the_pods_own_role_without_a_literal_password(
    replica_docs: list[dict[str, Any]], pod: str, role: str
) -> None:
    """Set host: DATABASE_REPLICA_URL targets the replica as THIS pod's role, password expanded."""
    env = _env(replica_docs, pod)
    names = [e["name"] for e in env]
    assert names.count("DATABASE_REPLICA_URL") == 1
    entry = env[names.index("DATABASE_REPLICA_URL")]
    value = entry["value"]
    assert value.startswith(f"postgresql://{role}:$(DB_PASSWORD)@{_REPLICA_HOST}:"), (
        value
    )
    assert "valueFrom" not in entry, (
        "the DSN is a plain value; the password is only expanded"
    )
    assert names.index("DB_PASSWORD") < names.index("DATABASE_REPLICA_URL"), (
        "DB_PASSWORD must be defined BEFORE the DSN that expands it"
    )
    # The primary DSN is untouched (fallback path).
    assert env[names.index("DATABASE_URL")]["value"].startswith(f"postgresql://{role}:")
