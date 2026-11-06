"""Helm-template assertions: the Rust svc-presentation is THE overlay runtime where enabled.

alpha / dal2alpha / local flip `presentation.rust.enabled` on; base (== production/beta/gamma,
which inherit the default) keeps the Python svc-presentation. The two templates share Deployment
and Service names and are mutually exclusive, so the two can never serve the same routes. When the
Rust runtime is on, svc-action additionally gets the overlay PUSH grant (identity scope, env,
egress policy); when off, none of that renders. Zero rendered documents is a hard failure; a
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
    return _render(
        "-f", str(_CHART / f"values-{request.param}.yaml"), "--set", "pipeline.svcAction.enabled=true"
    )


@pytest.fixture(scope="module")
def default_docs() -> list[dict[str, Any]]:
    """Alpha render with `presentation.rust.enabled` at the chart default (false).

    Equivalent to what beta/gamma/production inherit for this switch (their own required secrets
    make a bare production render impractical in a unit test).
    """
    return _render(
        "-f", str(_CHART / "values-alpha.yaml"),
        "--set", "presentation.rust.enabled=false",
        "--set", "pipeline.svcAction.enabled=true",
    )


def test_production_style_values_never_enable_rust() -> None:
    """No production/beta/gamma values file flips the switch; the chart default stays false."""
    base = yaml.safe_load((_CHART / "values.yaml").read_text())
    assert base["presentation"]["rust"]["enabled"] is False
    for env in ("production", "beta", "gamma"):
        values = yaml.safe_load((_CHART / f"values-{env}.yaml").read_text()) or {}
        assert not values.get("presentation", {}).get("rust", {}).get("enabled", False), env


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
    assert len(deployments) == 1, "Python and Rust svc-presentation must never both render"
    services = _by_name(rust_docs, "Service", "-svc-presentation")
    assert len(services) == 1
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    assert "svc-presentation-rust" in container["image"]
    assert "PUSH_JWKS_URL" in _env(deployments[0])  # Rust-only config surface


def test_svc_action_can_push_overlays_when_rust_is_on(rust_docs: list[dict[str, Any]]) -> None:
    assert "presentation.overlay:push" in _action_identity(rust_docs)["allowed_scopes"]
    action = _by_name(rust_docs, "Deployment", "-svc-action")
    assert len(action) == 1
    env = _env(action[0])
    assert env["PRESENTATION_URL"].endswith("-svc-presentation:8207")
    assert env["SERVICE_JWT_TOKEN_ENDPOINT"].endswith("/internal/service-token")
    assert env["SERVICE_JWT_SA_TOKEN_PATH"]
    policies = _by_name(rust_docs, "CiliumNetworkPolicy", "-svc-action-presentation-egress")
    assert len(policies) == 1
    peer = policies[0]["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"]
    assert peer["app.kubernetes.io/component"] == "svc-presentation"


def test_defaults_keep_python_presentation_and_render_no_push_grant(
    default_docs: list[dict[str, Any]],
) -> None:
    deployments = _by_name(default_docs, "Deployment", "-svc-presentation")
    assert len(deployments) == 1
    assert "svc-presentation-rust" not in deployments[0]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "PUSH_JWKS_URL" not in _env(deployments[0])
    assert "presentation.overlay:push" not in _action_identity(default_docs)["allowed_scopes"]
    assert "PRESENTATION_URL" not in _env(_by_name(default_docs, "Deployment", "-svc-action")[0])
    assert not _by_name(default_docs, "CiliumNetworkPolicy", "-svc-action-presentation-egress")


def test_presentation_off_renders_no_push_grant_even_with_rust_on() -> None:
    docs = _render(
        "-f", str(_CHART / "values-alpha.yaml"),
        "--set", "pipeline.svcPresentation.enabled=false",
        "--set", "pipeline.svcAction.enabled=true",
    )
    assert not _by_name(docs, "Deployment", "-svc-presentation")
    assert "presentation.overlay:push" not in _action_identity(docs)["allowed_scopes"]
