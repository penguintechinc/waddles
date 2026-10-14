"""Helm-template assertions for svc-ingest's machine-JWT identity (provider-connections consumer).

Rust svc-ingest reads provider connections through hub-api's internal routes, which need a machine
JWT with scope ``connections:resolve`` from a ``system``-tenant identity. hub-api's token endpoint
maps a TokenReview-validated ServiceAccount to an allow-listed identity, so the chart must:

* allow-list svc-ingest for exactly ``connections:resolve`` (least privilege), pinned to a
  DEDICATED ServiceAccount (never the chart-wide shared one -- any pod using it could bootstrap);
* actually render that ServiceAccount, run the svc-ingest-rust Deployment under it, and project the
  audience-scoped bootstrap token + endpoint env the caller needs.

hub_api/tests/provider_connections/test_chart_service_identity_grant.py exercises the same
rendered policy through the real issuer and routes. Zero rendered documents / no matching object is
a hard failure, never a silent pass (critical-rules.md Verification Integrity); a missing ``helm``
is a skip locally but a FAILURE in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    """Render the alpha chart with the Rust data plane on (svc-ingest-rust Deployment present)."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
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
            "--set",
            "pipeline.rustDataPlane.enabled=true",
            "--set",
            "pipeline.rustDataPlane.svcIngest.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _identities(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []) or []:
                if env.get("name") == "SERVICE_JWT_IDENTITIES":
                    return {
                        entry["service_id"].rsplit("/", 1)[-1]: entry
                        for entry in json.loads(env["value"])
                    }
    pytest.fail("no SERVICE_JWT_IDENTITIES env var rendered")


def _svc_ingest_deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    found = [
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-svc-ingest-rust")
    ]
    assert len(found) == 1, f"expected exactly one svc-ingest-rust Deployment, got {len(found)}"
    return found[0]


def test_svc_ingest_is_allow_listed_for_exactly_connections_resolve_on_the_system_tenant(
    docs: list[dict[str, Any]],
) -> None:
    entry = _identities(docs)["svc-ingest"]
    assert entry["allowed_scopes"] == ["connections:resolve"]
    assert entry["tenant"] == "system"
    assert entry["k8s_service_account"] == "svc-ingest"
    assert entry["service_id"].startswith("spiffe://penguintech.io/")


def test_connections_resolve_is_granted_to_no_other_identity(docs: list[dict[str, Any]]) -> None:
    identities = _identities(docs)
    assert len(identities) >= 3, identities.keys()
    for name, entry in identities.items():
        if name != "svc-ingest":
            assert "connections:resolve" not in entry["allowed_scopes"], name


def test_the_dedicated_svc_ingest_service_account_is_rendered(docs: list[dict[str, Any]]) -> None:
    accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
    assert [a["metadata"]["name"] for a in accounts if a["metadata"]["name"] == "svc-ingest"] == [
        "svc-ingest"
    ]


def test_svc_ingest_deployment_runs_as_the_dedicated_identity(docs: list[dict[str, Any]]) -> None:
    pod = _svc_ingest_deployment(docs)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "svc-ingest"


def test_svc_ingest_deployment_projects_the_audience_scoped_bootstrap_token(
    docs: list[dict[str, Any]],
) -> None:
    pod = _svc_ingest_deployment(docs)["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "service-jwt-bootstrap-token")
    (source,) = volume["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "hub-api"  # TokenReview audience
    container = pod["containers"][0]
    assert any(
        m["name"] == "service-jwt-bootstrap-token" and m["readOnly"]
        for m in container["volumeMounts"]
    )
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["SERVICE_JWT_SA_TOKEN_PATH"].endswith("/service-jwt-bootstrap-token")
    assert env["SERVICE_JWT_TOKEN_ENDPOINT"].endswith("/internal/service-token")
