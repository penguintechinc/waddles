"""Rescan CronJob least privilege: dedicated token-less SA, no whole-secret envFrom."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_SET = ["sourceAutoUpdate.enabled=true", "gitProxy.enabled=true", "global.deploymentTier=alpha"]


def _docs() -> list[dict]:
    """Render the chart with the rescan job enabled; render failure or zero docs fails."""
    args = ["helm", "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
            "--values", str(_CHART / "values-beta.yaml")]
    for kv in _SET:
        args += ["--set", kv]
    r = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    if r.returncode != 0:
        pytest.fail(f"helm template failed:\n{r.stdout}\n{r.stderr}")
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    assert docs, "zero documents rendered"
    return docs


def _job(docs):
    jobs = [d for d in docs if d["kind"] == "CronJob" and d["metadata"]["name"].endswith("-marketplace-source-rescan")]
    assert len(jobs) == 1
    return jobs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def test_dedicated_sa_no_token() -> None:
    docs = _docs()
    spec = _job(docs)
    sas = [d for d in docs if d["kind"] == "ServiceAccount" and d["metadata"]["name"].endswith("-marketplace-rescan")]
    assert len(sas) == 1
    assert sas[0]["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == sas[0]["metadata"]["name"]
    assert spec["automountServiceAccountToken"] is False


def test_no_whole_secret_envfrom() -> None:
    c = _job(_docs())["containers"][0]
    for ef in c.get("envFrom", []):
        assert "secretRef" not in ef
    names = {e["name"] for e in c["env"]}
    assert {"SECRET_KEY", "DB_PASSWORD", "MARKETPLACE_GIT_PROXY_URL"} <= names
