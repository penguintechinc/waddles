"""Helm-template assertions for hub-api outbound email (`values.smtp`).

Pins: non-secret relay settings land in the hub-api ConfigMap; SMTP AUTH credentials are only
ever `secretKeyRef`s to the operator Secret (`smtp.existingSecret`, keys `username`/`password`)
and are absent when no Secret is named (IP-allowlisted relay); the hub-api CNP allows exactly
`<host>:<port>` by FQDN; an invalid EHLO name / missing sender fails the render loudly.

Zero rendered documents is a hard failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_DAL2_VALUES = _CHART_DIR / "values-dal2alpha.yaml"


def _helm(values: Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    cmd = [
        "helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
        "--values", str(values), *extra,
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603


def _render(values: Path = _ALPHA_VALUES, extra: list[str] | None = None) -> list[dict[str, Any]]:
    result = _helm(values, extra or [])
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _config(docs: list[dict[str, Any]]) -> dict[str, str]:
    maps = [
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    ]
    assert len(maps) == 1
    return dict(maps[0]["data"])


def _hub_api_env(docs: list[dict[str, Any]]) -> dict[str, Any]:
    deploys = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component")
        == "hub-api"
    ]
    assert len(deploys) == 1
    containers = deploys[0]["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e for c in containers for e in c.get("env", [])}


def _hub_api_cnp_egress(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cnps = [
        d
        for d in docs
        if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-hub-api-policy")
    ]
    assert len(cnps) == 1
    return list(cnps[0]["spec"].get("egress", []))


@pytest.mark.parametrize("values", [_ALPHA_VALUES, _DAL2_VALUES])
def test_alpha_relay_is_ip_allowlisted_gmail_without_auth(values: Path) -> None:
    docs = _render(values)
    cfg = _config(docs)
    assert cfg["SMTP_HOST"] == "smtp-relay.gmail.com"
    assert cfg["SMTP_PORT"] == "587" and cfg["SMTP_TLS"] == "starttls"
    assert cfg["SMTP_EHLO_NAME"] == "waddles-alpha.penguintech.cloud"
    assert cfg["SMTP_FROM"] == "no-reply@penguintech.io" and cfg["SMTP_FROM_NAME"] == "Waddles"
    assert cfg["SMTP_TOTAL_TIMEOUT"] == "30"
    assert not any(k.startswith("SMTP_") and "PASS" in k for k in cfg)
    env = _hub_api_env(docs)
    assert "SMTP_USERNAME" not in env and "SMTP_PASSWORD" not in env
    if values == _ALPHA_VALUES:
        return  # local alpha renders no workload CNPs (networkPolicy/workloadEgress off)
    smtp_rules = [r for r in _hub_api_cnp_egress(docs) if "toFQDNs" in r]
    matched = [
        r
        for r in smtp_rules
        if r["toFQDNs"] == [{"matchName": "smtp-relay.gmail.com"}]
        and r["toPorts"][0]["ports"] == [{"port": "587", "protocol": "TCP"}]
    ]
    assert len(matched) == 1


def test_named_secret_supplies_auth_by_reference_only() -> None:
    docs = _render(extra=["--set", "smtp.existingSecret=relay-creds"])
    env = _hub_api_env(docs)
    for name, key in (("SMTP_USERNAME", "username"), ("SMTP_PASSWORD", "password")):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "relay-creds", "key": key}  # not optional: missing Secret is loud
        assert "value" not in env[name]


def test_unset_host_means_no_relay_and_no_egress() -> None:
    docs = _render(_DAL2_VALUES, ["--set", "smtp.host="])
    assert _config(docs)["SMTP_HOST"] == ""
    egress = _hub_api_cnp_egress(docs)
    assert not any(
        r.get("toFQDNs") == [{"matchName": "smtp-relay.gmail.com"}] for r in egress
    )


@pytest.mark.parametrize(
    ("flag", "needle"),
    [
        ("smtp.ehloName=localhost", "smtp.ehloName must"),
        ("smtp.ehloName=10.1.2.3", "smtp.ehloName must"),
        ("smtp.ehloName=", "smtp.ehloName must"),
        ("smtp.from=", "smtp.from is required"),
        ("smtp.tls=none", "smtp.tls must be"),
    ],
)
def test_invalid_relay_config_fails_the_render(flag: str, needle: str) -> None:
    result = _helm(_ALPHA_VALUES, ["--set", flag])
    assert result.returncode != 0 and needle in result.stderr
