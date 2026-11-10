"""Helm-template assertions: the Rust svc-presentation is THE (only) overlay runtime.

The legacy Python svc-presentation template was removed; `presentation.rust.enabled` defaults to
true in every environment (base, so production/beta/gamma inherit it). When on, svc-action
additionally gets the overlay PUSH grant (identity scope, egress policy); setting it false
renders no presentation workload and none of that grant. Zero rendered documents is a hard failure; a
missing helm is a skip locally but a FAILURE in CI. Set HELM_BIN=helm3 where the default is v4.
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

_CHART = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM_BIN", "helm")
_RUST_ENVS = ["alpha", "dal2alpha", "local"]


def _render(*args: str) -> list[dict[str, Any]]:
    if not shutil.which(_HELM):
        if os.getenv("CI"):
            pytest.fail(f"{_HELM} CLI is required in CI")
        pytest.skip(f"{_HELM} CLI not available")
    argv = [_HELM, "template", "w", str(_CHART), "--kube-version", "1.30.0", *args]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(scope="module", params=_RUST_ENVS)
def rust_docs(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    # rustDataPlane on so the Rust svc-action Deployment renders under `local` too.
    return _render(
        "-f", str(_CHART / f"values-{request.param}.yaml"), "--set", "pipeline.rustDataPlane.enabled=true"
    )


@pytest.fixture(scope="module")
def default_docs() -> list[dict[str, Any]]:
    """Alpha render with `presentation.rust.enabled` explicitly switched off."""
    return _render(
        "-f", str(_CHART / "values-alpha.yaml"),
        "--set", "presentation.rust.enabled=false",
    )


def test_production_style_values_inherit_rust_default() -> None:
    """Chart default is true and no production/beta/gamma values file switches it off."""
    base = yaml.safe_load((_CHART / "values.yaml").read_text())
    assert base["presentation"]["rust"]["enabled"] is True
    for env in ("production", "beta", "gamma"):
        values = yaml.safe_load((_CHART / f"values-{env}.yaml").read_text()) or {}
        assert values.get("presentation", {}).get("rust", {}).get("enabled", True) is True, env


def _by_name(docs: list[dict[str, Any]], kind: str, suffix: str) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]


def _env(deployment: dict[str, Any]) -> dict[str, str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def _action_identity(docs: list[dict[str, Any]]) -> dict[str, Any]:
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []) or []:
                if e["name"] == "SERVICE_JWT_IDENTITIES":
                    for entry in json.loads(e["value"]):
                        if entry["service_id"].endswith("/svc-action"):
                            return entry
    pytest.fail("svc-action identity not rendered")


def test_rust_runtime_is_the_only_presentation_deployment(rust_docs: list[dict[str, Any]]) -> None:
    deployments = _by_name(rust_docs, "Deployment", "-svc-presentation")
    assert len(deployments) == 1
    services = _by_name(rust_docs, "Service", "-svc-presentation")
    assert len(services) == 1
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].rsplit("/", 1)[-1].startswith("svc-presentation:")
    assert "PUSH_JWKS_ENDPOINT" in _env(deployments[0])  # Rust-only config surface


def test_svc_action_can_push_overlays_when_rust_is_on(rust_docs: list[dict[str, Any]]) -> None:
    assert "presentation.overlay:push" in _action_identity(rust_docs)["allowed_scopes"]
    action = _by_name(rust_docs, "Deployment", "-svc-action-rust")
    assert len(action) == 1
    env = _env(action[0])
    # The mint is gRPC now: hub-api's internal gRPC port over TLS, never the REST port/path.
    assert env["SERVICE_JWT_TOKEN_ENDPOINT"].startswith("https://")
    assert "/internal/service-token" not in env["SERVICE_JWT_TOKEN_ENDPOINT"]
    assert env["SERVICE_JWT_SA_TOKEN_PATH"]
    policies = _by_name(rust_docs, "CiliumNetworkPolicy", "-svc-action-presentation-egress")
    assert len(policies) == 1
    peer = policies[0]["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"]
    assert peer["app.kubernetes.io/component"] == "svc-presentation"


def test_rust_disabled_renders_no_presentation_and_no_push_grant(
    default_docs: list[dict[str, Any]],
) -> None:
    assert not _by_name(default_docs, "Deployment", "-svc-presentation")
    assert not _by_name(default_docs, "Service", "-svc-presentation")
    assert "presentation.overlay:push" not in _action_identity(default_docs)["allowed_scopes"]
    assert not _by_name(default_docs, "CiliumNetworkPolicy", "-svc-action-presentation-egress")


def test_presentation_off_renders_no_push_grant_even_with_rust_on() -> None:
    docs = _render(
        "-f", str(_CHART / "values-alpha.yaml"),
        "--set", "pipeline.svcPresentation.enabled=false",
    )
    assert not _by_name(docs, "Deployment", "-svc-presentation")
    assert "presentation.overlay:push" not in _action_identity(docs)["allowed_scopes"]
