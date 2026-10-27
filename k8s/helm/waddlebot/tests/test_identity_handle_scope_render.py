"""Helm-template assertions for the bundle `identity` capability's hub-api scope.

svc-process resolves a free-text `@handle` mention through hub-api's
`IdentityService.ResolveHandle`, which requires the machine-JWT scope
`identity:handle:resolve` (`hub_api/grpc_internal/servicers.py::REQUIRED_SCOPES`).
hub-api's token endpoint only mints scopes listed in the caller's
`serviceJwt.identities[*].allowedScopes`, rendered into the hub-api Deployment's
`SERVICE_JWT_IDENTITIES` env var -- so without this entry every handle mention is
`unavailable` (fail-closed) even with a healthy hub-api.

Least privilege: the scope belongs to svc-process only (svc-action, the egress
detokenizer, must not gain a handle->uuid oracle).

Zero rendered documents, zero matching env vars is a hard failure here, never a
silent pass (critical-rules.md Verification Integrity).
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
_HANDLE_SCOPE = "identity:handle:resolve"


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


def test_svc_process_may_request_the_handle_resolve_scope_alongside_mint() -> None:
    scopes = _service_jwt_identities()["svc-process"]
    assert _HANDLE_SCOPE in scopes
    # The pre-existing mint scope the PII tokenizer needs is untouched.
    assert "identity:ephemeral:mint" in scopes


def test_the_handle_resolve_scope_is_not_granted_to_any_other_service() -> None:
    for account, scopes in _service_jwt_identities().items():
        if account != "svc-process":
            assert _HANDLE_SCOPE not in scopes, (
                f"{account} must not hold {_HANDLE_SCOPE}"
            )
