"""dal2alpha renders :8443 public/callback URLs and a scoped Twitch egress CNP.

Regression: alpha's public origin is https://waddles-alpha.penguintech.cloud:8443 (plain :443 is
beta's); the callback base URLs lacked the port so OAuth redirects hit the wrong env. Also no CNP
allowed Twitch hosts. Zero matching documents is a failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ORIGIN = "https://waddles-alpha.penguintech.cloud:8443"
_HELM = shutil.which("helm") or shutil.which("helm3") or str(Path.home() / ".local/bin/helm3")

pytestmark = pytest.mark.skipif(not Path(_HELM).exists(), reason="helm not installed")


def _render() -> list[dict[str, Any]]:
    """Render the chart with values-dal2alpha.yaml and return all documents."""
    result = subprocess.run(
        [_HELM, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.35.0",
         "--values", str(_CHART_DIR / "values-dal2alpha.yaml")],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def test_public_urls_carry_port() -> None:
    """Every public/callback/CORS value in the configmap includes :8443."""
    cms = [d for d in _render() if d["kind"] == "ConfigMap" and "IDENTITY_CALLBACK_BASE_URL" in d.get("data", {})]
    assert len(cms) == 1, f"expected 1 config ConfigMap, got {len(cms)}"
    data = cms[0]["data"]
    keys = ("IDENTITY_CALLBACK_BASE_URL", "CONNECTIONS_CALLBACK_BASE_URL", "PUBLIC_WEBUI_URL",
            "FRONTEND_ORIGIN", "CORS_ORIGIN")
    for k in keys:
        assert data.get(k) == _ORIGIN, f"{k}={data.get(k)!r}"


def test_twitch_egress_policy_rendered() -> None:
    """The Twitch CNP selects the Twitch callers and allows only the named FQDNs on 443."""
    cnps = [d for d in _render() if d["kind"] == "CiliumNetworkPolicy"
            and d["metadata"]["name"].endswith("-twitch-egress")]
    assert len(cnps) == 1, f"expected 1 twitch CNP, got {len(cnps)}"
    spec = cnps[0]["spec"]
    comps = spec["endpointSelector"]["matchExpressions"][0]["values"]
    assert "svc-ingest-rust" in comps and "svc-action" in comps
    fqdn_rules = [r for r in spec["egress"] if "toFQDNs" in r]
    assert len(fqdn_rules) == 1
    names = {f["matchName"] for f in fqdn_rules[0]["toFQDNs"]}
    assert names == {"irc-ws.chat.twitch.tv", "id.twitch.tv", "api.twitch.tv",
                     "eventsub.wss.twitch.tv"}
    assert fqdn_rules[0]["toPorts"][0]["ports"] == [{"port": "443", "protocol": "TCP"}]
    dns = [r for r in spec["egress"] if "toEndpoints" in r][0]["toPorts"][0]["rules"]["dns"]
    assert {d["matchName"] for d in dns} == names
