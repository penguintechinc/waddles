"""Helm-template assertions for the `key:stream-dek:read` machine-JWT scope (#442).

`KeyService.GetStreamDek` (`hub_api/grpc_internal/servicers.py::REQUIRED_SCOPES`) hands out
per-tenant stream identity-field DEKs. hub-api only mints scopes listed in the caller's
`serviceJwt.identities[*].allowedScopes` (rendered into `SERVICE_JWT_IDENTITIES`), so svc-ingest
(encrypt) and svc-process (decrypt) must hold it -- and NO other identity may.

Zero rendered documents / zero matching env vars is a hard failure, never a silent pass.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_SCOPE = "key:stream-dek:read"
_HOLDERS = frozenset({"svc-ingest", "svc-process"})


def _service_jwt_identities() -> dict[str, list[str]]:
    """Render the chart and return `{k8s_service_account: allowed_scopes}` from hub-api's env."""
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    result = subprocess.run(
        [
            "helm",
            "template",
            "waddlebot",
            str(_CHART_DIR),
            "--kube-version",
            "1.30.0",
            "--values",
            str(_ALPHA_VALUES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    found: list[dict[str, Any]] = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []):
                if env.get("name") == "SERVICE_JWT_IDENTITIES":
                    found.extend(json.loads(env["value"]))
    assert found, "no SERVICE_JWT_IDENTITIES env var rendered -- cannot be a pass"
    return {entry["k8s_service_account"]: entry["allowed_scopes"] for entry in found}


def test_svc_ingest_and_svc_process_may_request_the_stream_dek_scope() -> None:
    identities = _service_jwt_identities()
    for holder in sorted(_HOLDERS):
        assert _SCOPE in identities[holder], f"{holder} must hold {_SCOPE}"


def test_no_other_identity_holds_the_stream_dek_scope() -> None:
    identities = _service_jwt_identities()
    assert len(identities) > len(_HOLDERS), "too few identities rendered to prove exclusivity"
    for account, scopes in identities.items():
        if account not in _HOLDERS:
            assert _SCOPE not in scopes, f"{account} must not hold {_SCOPE}"
