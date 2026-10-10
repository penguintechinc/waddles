"""Helm-template assertions for fix/svc-streaming-teardown-hang (PUBLIC_BASE_URL part).

# regression: alpha's svc-streaming PUBLIC_BASE_URL (and svc-presentation's
# PUBLIC_STREAMING_URL) was a hardcoded `http://192.168.10.75:31208`; the node's LAN IP is
# now 192.168.2.210, so every playback URL / ICE host the service handed out pointed at a
# dead address.

The fix makes the value env-driven instead of re-hardcoding a new IP: when
`publicBaseUrl` is empty and `publicBaseUrlFromNodeIP` is on, the pod derives
`http://<node hostIP>:<nodePort.httpPort>` from the downward API (`status.hostIP`) via
kubelet `$(NODE_HOST_IP)` env expansion. These tests pin:

1. alpha renders the derived form -- `NODE_HOST_IP` fieldRef declared BEFORE the
   `PUBLIC_BASE_URL` that references it (kubelet only expands earlier-declared vars) --
   and no literal LAN IP survives in either streaming env var;
2. an explicit `publicBaseUrl` always wins over the node-IP derivation;
3. with neither set the env var is absent (the service's own default applies), not empty;
4. asking for the node-IP derivation without the NodePort it needs fails the render loudly
   instead of emitting a half-formed URL;
5. the Python svc-presentation's PUBLIC_STREAMING_URL follows the same rules.

Zero matching Deployments/containers is a hard failure, never a silent pass
(critical-rules.md Verification Integrity) -- each helper reports what it examined.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_STALE_LAN_IP = "192.168.10.75"
# alpha enables the Rust svc-presentation, which does not read PUBLIC_STREAMING_URL; the
# Python Deployment (the only consumer of presentation.publicStreamingUrl) needs it off.
_PYTHON_PRESENTATION = ["presentation.rust.enabled=false"]


def _helm_template(
    extra_set: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Render the chart under alpha values with `extra_set` overrides."""
    args = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART_DIR / "values-alpha.yaml"),
    ]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    return result


def _docs(extra_set: list[str]) -> list[dict[str, Any]]:
    docs = [d for d in yaml.safe_load_all(_helm_template(extra_set).stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _env(
    docs: list[dict[str, Any]], workload_suffix: str, container: str
) -> list[dict[str, Any]]:
    """Return the ordered env list of `container` in the Deployment ending `workload_suffix`."""
    examined = 0
    for doc in docs:
        if doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith(
            workload_suffix
        ):
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            examined += 1
            if c["name"] == container:
                return c.get("env") or []
    pytest.fail(
        f"no container {container!r} in a Deployment ending {workload_suffix!r} "
        f"(examined {examined} containers)"
    )


def _by_name(env: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in env}


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


class TestSvcStreamingPublicBaseUrl:
    """pipeline.svcStreaming.publicBaseUrl / publicBaseUrlFromNodeIP."""

    def test_alpha_derives_the_url_from_the_node_ip_not_a_literal(self) -> None:
        env = _env(_docs([]), "-svc-streaming", "svc-streaming")
        names = [e["name"] for e in env]
        assert "NODE_HOST_IP" in names and "PUBLIC_BASE_URL" in names, (
            f"examined {len(env)} env vars: {names}"
        )
        assert names.index("NODE_HOST_IP") < names.index("PUBLIC_BASE_URL"), (
            "NODE_HOST_IP must be declared before PUBLIC_BASE_URL or kubelet will not expand it"
        )
        by_name = _by_name(env)
        assert (
            by_name["NODE_HOST_IP"]["valueFrom"]["fieldRef"]["fieldPath"]
            == "status.hostIP"
        )
        assert by_name["PUBLIC_BASE_URL"]["value"] == "http://$(NODE_HOST_IP):31208"

    def test_no_stale_literal_ip_survives_in_the_streaming_env(self) -> None:
        env = _env(_docs([]), "-svc-streaming", "svc-streaming")
        assert env, "examined zero env vars"
        for entry in env:
            assert _STALE_LAN_IP not in str(entry), f"stale LAN IP in {entry}"

    def test_http_port_follows_the_nodeport_value(self) -> None:
        env = _env(
            _docs(["pipeline.svcStreaming.nodePort.httpPort=32001"]),
            "-svc-streaming",
            "svc-streaming",
        )
        assert (
            _by_name(env)["PUBLIC_BASE_URL"]["value"] == "http://$(NODE_HOST_IP):32001"
        )

    def test_explicit_url_wins_over_the_node_ip_derivation(self) -> None:
        env = _env(
            _docs(["pipeline.svcStreaming.publicBaseUrl=https://media.example.com"]),
            "-svc-streaming",
            "svc-streaming",
        )
        by_name = _by_name(env)
        assert by_name["PUBLIC_BASE_URL"]["value"] == "https://media.example.com"
        assert "NODE_HOST_IP" not in by_name, (
            "no downward-API var when the URL is explicit"
        )

    def test_unset_means_absent_not_empty(self) -> None:
        env = _env(
            _docs(["pipeline.svcStreaming.publicBaseUrlFromNodeIP=false"]),
            "-svc-streaming",
            "svc-streaming",
        )
        names = [e["name"] for e in env]
        assert env, "examined zero env vars"
        assert "PUBLIC_BASE_URL" not in names and "NODE_HOST_IP" not in names, names

    def test_node_ip_derivation_without_a_nodeport_fails_the_render(self) -> None:
        result = _helm_template(
            ["pipeline.svcStreaming.nodePort.enabled=false"], check=False
        )
        assert result.returncode != 0, (
            "render must fail rather than emit a half-formed URL"
        )
        assert "publicBaseUrlFromNodeIP requires" in result.stderr, result.stderr


class TestSvcPresentationPublicStreamingUrl:
    """presentation.publicStreamingUrl / publicStreamingUrlFromNodeIP (Python presentation)."""

    def test_alpha_derives_the_url_from_the_node_ip(self) -> None:
        env = _env(_docs(_PYTHON_PRESENTATION), "-svc-presentation", "svc-presentation")
        names = [e["name"] for e in env]
        assert names.index("NODE_HOST_IP") < names.index("PUBLIC_STREAMING_URL"), names
        by_name = _by_name(env)
        assert (
            by_name["NODE_HOST_IP"]["valueFrom"]["fieldRef"]["fieldPath"]
            == "status.hostIP"
        )
        assert (
            by_name["PUBLIC_STREAMING_URL"]["value"] == "http://$(NODE_HOST_IP):31208"
        )
        assert _STALE_LAN_IP not in str(by_name["PUBLIC_STREAMING_URL"])

    def test_explicit_url_wins(self) -> None:
        env = _env(
            _docs(
                _PYTHON_PRESENTATION
                + ["presentation.publicStreamingUrl=https://media.example.com"]
            ),
            "-svc-presentation",
            "svc-presentation",
        )
        by_name = _by_name(env)
        assert by_name["PUBLIC_STREAMING_URL"]["value"] == "https://media.example.com"
        assert "NODE_HOST_IP" not in by_name

    def test_unset_renders_the_documented_empty_default(self) -> None:
        env = _env(
            _docs(
                _PYTHON_PRESENTATION
                + ["presentation.publicStreamingUrlFromNodeIP=false"]
            ),
            "-svc-presentation",
            "svc-presentation",
        )
        by_name = _by_name(env)
        assert by_name["PUBLIC_STREAMING_URL"]["value"] == ""
        assert "NODE_HOST_IP" not in by_name

    def test_node_ip_derivation_without_a_nodeport_fails_the_render(self) -> None:
        # svc-streaming gets an explicit URL so ITS node-IP guard cannot be the failure
        # under test -- this must be the presentation template's own guard firing.
        result = _helm_template(
            _PYTHON_PRESENTATION
            + [
                "pipeline.svcStreaming.nodePort.enabled=false",
                "pipeline.svcStreaming.publicBaseUrl=https://media.example.com",
            ],
            check=False,
        )
        assert result.returncode != 0
        assert "publicStreamingUrlFromNodeIP requires" in result.stderr, result.stderr
