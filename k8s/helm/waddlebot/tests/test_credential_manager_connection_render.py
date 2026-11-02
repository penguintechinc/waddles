"""Render credential-manager HTTPS connection configuration for the hub API.

The endpoint belongs to an independently deployed operator-secret service; the hub
must never invent an insecure default or mount a private server key as CA material.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1]


def _render(*settings: str) -> subprocess.CompletedProcess[str]:
    """Render the alpha chart with operator connection settings.

    Real Helm evaluation catches indentation and missing values in the deployment.
    """
    args = [
        "helm",
        "template",
        "--kube-version",
        "1.30.0",
        "waddlebot",
        str(CHART),
        "-f",
        str(CHART / "values-alpha.yaml"),
    ]
    for setting in settings:
        args.extend(["--set", setting])
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _hub(
    result: subprocess.CompletedProcess[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Locate the actual hub deployment in nonempty rendered YAML.

    Requiring exactly one match prevents a missing deployment from passing vacuously.
    """
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == "Deployment"
        and doc["metadata"]["name"].endswith("-hub-api-v3")
    ]
    assert len(matches) == 1
    pod = matches[0]["spec"]["template"]["spec"]
    return pod, next(c for c in pod["containers"] if c["name"] == "hub-api")


def test_default_does_not_invent_a_credential_manager_origin() -> None:
    """An absent operator deployment keeps bot tests explicitly unavailable.

    The chart must not send a privileged bearer to an assumed cluster hostname.
    """
    _, container = _hub(_render())
    env = {item["name"]: item for item in container["env"]}
    assert "CREDENTIAL_MANAGER_URL" not in env
    assert "CREDENTIAL_MANAGER_CA_FILE" not in env


def test_https_endpoint_and_ca_secret_reach_the_hub_client() -> None:
    """Private CA trust is mounted read-only and the configured URL is preserved.

    Only the CA certificate is projected; the client's server key is never mounted.
    """
    pod, container = _hub(
        _render(
            "pipeline.hubApi.credentialManager.url=https://credentials.example.com:8443",
            "pipeline.hubApi.credentialManager.caSecretName=operator-credential-ca",
            "pipeline.hubApi.credentialManager.timeoutSeconds=9",
        )
    )
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["CREDENTIAL_MANAGER_URL"] == "https://credentials.example.com:8443"
    assert env["CREDENTIAL_MANAGER_TIMEOUT_S"] == "9"
    assert (
        env["CREDENTIAL_MANAGER_CA_FILE"] == "/etc/waddlebot/credential-manager/ca.crt"
    )
    mount = next(
        m for m in container["volumeMounts"] if m["name"] == "credential-manager-ca"
    )
    assert (
        mount["mountPath"] == "/etc/waddlebot/credential-manager"
        and mount["readOnly"] is True
    )
    volume = next(v for v in pod["volumes"] if v["name"] == "credential-manager-ca")
    assert volume["secret"] == {
        "secretName": "operator-credential-ca",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://credential-manager:8095",
        "http://credentials.example.com",
        "ftp://credentials.example.com",
    ],
)
def test_plaintext_credential_manager_endpoint_fails_render(url: str) -> None:
    """Configuration rejects HTTP before any pod can forward an administrator token.

    The application repeats this check for origins supplied outside Helm values.
    """
    result = _render(f"pipeline.hubApi.credentialManager.url={url}")
    assert result.returncode != 0 and "must use HTTPS" in result.stderr
