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
from urllib.parse import urlparse
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


def _render(*extra_set: str) -> list[dict[str, Any]]:
    """Render the alpha chart with the Rust data plane on (svc-ingest-rust Deployment present)."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    sets = [
        "pipeline.rustDataPlane.enabled=true",
        "pipeline.rustDataPlane.svcIngest.enabled=true",
        *extra_set,
    ]
    argv = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_ALPHA_VALUES),
    ]
    for item in sets:
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    """Alpha render: svcIngest.dynamicConnections.enabled is true in values-alpha.yaml."""
    return _render()


@pytest.fixture(scope="module")
def disabled_docs() -> list[dict[str, Any]]:
    """Same render with svcIngest.dynamicConnections.enabled=false (the beta/prod default)."""
    return _render("pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=false")


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
    assert entry["allowed_scopes"] == ["connections:resolve", "dataplane:resolve-scope"]
    assert entry["tenant"] == "system"
    assert entry["k8s_service_account"] == "svc-ingest"
    parsed = urlparse(entry["service_id"])
    assert (parsed.scheme, parsed.netloc) == ("spiffe", "penguintech.io")
    assert parsed.path.rstrip("/").endswith("/svc-ingest")


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


def test_dynamic_connections_defaults_off_in_base_values_and_on_only_in_alpha() -> None:
    base = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    alpha = yaml.safe_load(_ALPHA_VALUES.read_text())
    assert base["pipeline"]["rustDataPlane"]["svcIngest"]["dynamicConnections"]["enabled"] is False
    assert alpha["pipeline"]["rustDataPlane"]["svcIngest"]["dynamicConnections"]["enabled"] is True


def test_disabled_renders_no_svc_ingest_grant(disabled_docs: list[dict[str, Any]]) -> None:
    identities = _identities(disabled_docs)
    assert "svc-ingest" not in identities
    assert len(identities) >= 3, identities.keys()  # other identities still rendered
    for name, entry in identities.items():
        assert "connections:resolve" not in entry["allowed_scopes"], name


def test_disabled_renders_no_service_jwt_mount_or_env_on_svc_ingest(
    disabled_docs: list[dict[str, Any]],
) -> None:
    pod = _svc_ingest_deployment(disabled_docs)["spec"]["template"]["spec"]
    assert "service-jwt-bootstrap-token" not in [v["name"] for v in pod["volumes"]]
    container = pod["containers"][0]
    assert "service-jwt-bootstrap-token" not in [m["name"] for m in container["volumeMounts"]]
    env_names = {e["name"] for e in container["env"]}
    assert not {n for n in env_names if n.startswith("SERVICE_JWT_")}


def test_enabled_renders_grant_mount_and_env(docs: list[dict[str, Any]]) -> None:
    assert "svc-ingest" in _identities(docs)
    pod = _svc_ingest_deployment(docs)["spec"]["template"]["spec"]
    assert "service-jwt-bootstrap-token" in [v["name"] for v in pod["volumes"]]
    env_names = {e["name"] for e in pod["containers"][0]["env"]}
    assert {"SERVICE_JWT_TOKEN_ENDPOINT", "SERVICE_JWT_SA_TOKEN_PATH"} <= env_names


_RESOLVER_SCOPE = "dataplane:resolve-scope"


def test_dataplane_resolve_scope_is_granted_to_svc_ingest_alone(docs: list[dict[str, Any]]) -> None:
    identities = _identities(docs)
    assert _RESOLVER_SCOPE in identities["svc-ingest"]["allowed_scopes"]
    assert len(identities) >= 3, identities.keys()  # denominator: the others were examined
    for name, entry in identities.items():
        if name != "svc-ingest":
            assert _RESOLVER_SCOPE not in entry["allowed_scopes"], name


def test_dataplane_resolve_scope_is_not_rendered_when_dynamic_connections_is_off(
    disabled_docs: list[dict[str, Any]],
) -> None:
    identities = _identities(disabled_docs)
    assert len(identities) >= 3, identities.keys()
    for name, entry in identities.items():
        assert _RESOLVER_SCOPE not in entry["allowed_scopes"], name
