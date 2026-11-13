"""Helm render: svc-ingest gets DEFAULT_TENANT_ID (alpha = 1) so the env Discord app serves the default tenant."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM_BIN", "helm")


def _svc_ingest_env(values: str) -> dict[str, str]:
    res = subprocess.run(  # noqa: S603 -- fixed argv, test-only
        [_HELM, "template", "waddlebot", str(_CHART), "--kube-version", "1.30.0",
         "--values", str(_CHART / values)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr[-600:]
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm rendered zero documents"
    deps = [
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("svc-ingest-rust")
    ]
    assert len(deps) == 1, f"expected one svc-ingest-rust Deployment, got {len(deps)}"
    env = deps[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e.get("value", "") for e in env}


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml"])
def test_alpha_svc_ingest_default_tenant_id_is_one(values: str) -> None:
    assert _svc_ingest_env(values).get("DEFAULT_TENANT_ID") == "1"
