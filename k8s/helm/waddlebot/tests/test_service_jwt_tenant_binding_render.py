"""Helm-template assertions for the machine-JWT tenant binding (identity hardening).

hub-api's internal gRPC server rejects any token with no `tenant` claim, and the issuer
stamps that claim from the allow-listed identity (`serviceJwt.identities.<svc>.tenant`),
never from the caller. The chart therefore has to render `tenant` into hub-api's
`SERVICE_JWT_IDENTITIES`: svc-process and svc-action serve every tenant, so they are bound
to the operator-plane `system` tenant. A render that silently dropped the key would pass
every unit test and then fail every identity RPC at runtime, so it is asserted here.

Zero rendered documents / no hub-api Deployment / no env var is a hard failure, never a
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


def _render(extra_args: list[str] | None = None) -> list[dict[str, Any]]:
    cmd = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_ALPHA_VALUES),
    ]
    cmd.extend(extra_args or [])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _identities(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """hub-api's rendered SERVICE_JWT_IDENTITIES, keyed by the service's short name."""
    examined = 0
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            for entry in container.get("env", []) or []:
                if entry.get("name") == "SERVICE_JWT_IDENTITIES":
                    examined += 1
                    parsed = json.loads(entry["value"])
                    return {item["service_id"].rsplit("/", 1)[-1]: item for item in parsed}
    pytest.fail(f"no SERVICE_JWT_IDENTITIES env var rendered (Deployments examined: {examined})")


@pytest.fixture(scope="module")
def alpha_docs() -> list[dict[str, Any]]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    return _render()


def test_data_plane_identities_are_bound_to_the_system_tenant(
    alpha_docs: list[dict[str, Any]],
) -> None:
    identities = _identities(alpha_docs)
    assert {"svc-process", "svc-action"} <= set(identities), identities.keys()
    for name in ("svc-process", "svc-action"):
        assert identities[name]["tenant"] == "system", name


def test_an_identity_without_tenant_renders_empty_not_a_default(
    alpha_docs: list[dict[str, Any]],
) -> None:
    """Unbinding an identity must render "" (rejected by the gRPC server), never `system`."""
    docs = _render(["--set", "serviceJwt.identities.svc-action.tenant="])
    identities = _identities(docs)
    assert identities["svc-action"]["tenant"] == ""
    assert identities["svc-process"]["tenant"] == "system"
