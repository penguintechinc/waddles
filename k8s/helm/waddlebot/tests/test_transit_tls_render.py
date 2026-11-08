"""Helm-render assertions for in-cluster HTTP transit TLS (audit F3/F4/F6).

Covers ``values-dal2alpha.yaml`` (enabled) and ``values-alpha.yaml`` (default off, forced on via
``--set``): hub-api serves HTTPS (hypercorn certfile/keyfile), every internal HTTP URL to hub-api /
core-data / svc-streaming is ``https://<FQDN>``, every client container carries the internal CA
(``INTERNAL_CA_PEM``) + ``INTERNAL_TLS_REQUIRED``, certs are ECDSA P-256 from the internal-CA
issuer with FQDN-only SANs, and the Gateway hop is re-encrypted. ``helm`` missing skips locally but
FAILS in CI; zero rendered documents / zero containers examined is a hard failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_FQDN_SUFFIX = ".waddlebot.svc.cluster.local"
_HUB_API_FQDN = f"waddlebot-hub-api-v3{_FQDN_SUFFIX}"


def _helm(values: str, *extra_set: str) -> subprocess.CompletedProcess[str]:
    """Run ``helm template`` with the given values file and ``--set`` overrides."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0", "--values", str(_CHART_DIR / values),
    ]  # fmt: skip
    for item in extra_set:
        argv += ["--set", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _render(values: str, *extra_set: str) -> list[dict[str, Any]]:
    """Render and parse; a failed render or zero documents is a hard failure."""
    result = _helm(values, *extra_set)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Pod spec of a workload document, else None."""
    kind = doc["kind"]
    if kind in ("Deployment", "StatefulSet", "Job"):
        return doc["spec"]["template"]["spec"]  # type: ignore[no-any-return]
    if kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]  # type: ignore[no-any-return]
    return None


def _containers(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """All (workload-name, container) pairs including init containers."""
    out: list[tuple[str, dict[str, Any]]] = []
    for doc in docs:
        spec = _pod_spec(doc)
        if spec:
            for c in spec["containers"] + spec.get("initContainers", []):
                out.append((doc["metadata"]["name"], c))
    return out


def _env(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Container env as name -> entry."""
    return {e["name"]: e for e in container.get("env", [])}


def _by_component(
    docs: list[dict[str, Any]], kind: str, component: str
) -> dict[str, Any]:
    """The single document of ``kind`` with the given component label."""
    found = [
        d for d in docs
        if d["kind"] == kind
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == component
    ]  # fmt: skip
    assert len(found) == 1, f"expected 1 {kind}/{component}, got {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def dal2() -> list[dict[str, Any]]:
    """Rendered dal2alpha values (transit TLS on)."""
    return _render("values-dal2alpha.yaml")


def test_hub_api_serves_https_only(dal2: list[dict[str, Any]]) -> None:
    """hub-api command binds with certfile/keyfile and probes use HTTPS."""
    dep = _by_component(dal2, "Deployment", "hub-api")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    cmd = container["command"]
    assert cmd[0] == "hypercorn"
    assert "--certfile" in cmd and "--keyfile" in cmd and "--ciphers" in cmd
    for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
        assert container[probe]["httpGet"]["scheme"] == "HTTPS", probe
    env = _env(container)
    assert env["TLS_CERT_FILE"]["value"] == "/etc/waddlebot/tls/tls.crt"
    assert env["INTERNAL_TLS_REQUIRED"]["value"] == "true"
    assert "INTERNAL_CA_PEM" in env


def test_no_plaintext_hub_api_or_internal_http_urls(dal2: list[dict[str, Any]]) -> None:
    """No rendered env/ConfigMap value points an internal client at http://hub-api/core-data/streaming."""
    offenders: list[str] = []
    examined = 0
    for name, c in _containers(dal2):
        for e in c.get("env", []):
            val = e.get("value")
            if not isinstance(val, str):
                continue
            examined += 1
            if val.startswith("http://") and any(
                s in val for s in ("hub-api", "core-data", "svc-streaming")
            ):
                offenders.append(f"{name}/{c['name']}:{e['name']}={val}")
    for doc in dal2:
        if doc["kind"] == "ConfigMap":
            for k, v in (doc.get("data") or {}).items():
                examined += 1
                if isinstance(v, str) and v.startswith("http://") and "hub-api" in v:
                    offenders.append(f"ConfigMap {doc['metadata']['name']}:{k}={v}")
    assert examined > 100, (
        f"only {examined} env values examined -- scan is not exercising the chart"
    )
    assert not offenders, offenders


def test_hub_api_urls_use_fqdn_https(dal2: list[dict[str, Any]]) -> None:
    """HUB_API_URL, token mint and JWKS endpoints are https://<FQDN> (CA nameConstraints form)."""
    seen: dict[str, int] = {"HUB_API_URL": 0, "token": 0, "jwks": 0}
    for _name, c in _containers(dal2):
        for e in c.get("env", []):
            val = e.get("value", "")
            if e["name"] == "HUB_API_URL" and isinstance(val, str):
                assert val == f"https://{_HUB_API_FQDN}:8204", val
                seen["HUB_API_URL"] += 1
            if isinstance(val, str) and val.endswith("/internal/service-token"):
                assert val == f"https://{_HUB_API_FQDN}:8204/internal/service-token", (
                    val
                )
                seen["token"] += 1
            if isinstance(val, str) and val.endswith("/internal/service-jwks.json"):
                assert (
                    val == f"https://{_HUB_API_FQDN}:8204/internal/service-jwks.json"
                ), val
                seen["jwks"] += 1
    assert all(n > 0 for n in seen.values()), seen
    assert seen["token"] >= 4, (
        "svc-ingest/process/action/presentation each mint over https"
    )


def test_every_hub_api_client_has_ca_and_refuses_plaintext(
    dal2: list[dict[str, Any]],
) -> None:
    """Any container with an explicit hub-api URL env gets INTERNAL_CA_PEM (Secret ref) + REQUIRED."""
    checked = 0
    for name, c in _containers(dal2):
        env = _env(c)
        urls = [
            e.get("value", "") for e in env.values()
            if isinstance(e.get("value"), str) and _HUB_API_FQDN in e["value"]
        ]  # fmt: skip
        if not urls and not c["name"] == "wait-for-hub-api":
            continue
        checked += 1
        assert "INTERNAL_CA_PEM" in env, f"{name}/{c['name']} lacks the internal CA"
        ref = env["INTERNAL_CA_PEM"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "waddlebot-hub-api-http-tls", "key": "ca.crt"}
        assert env["INTERNAL_TLS_REQUIRED"]["value"] == "true", f"{name}/{c['name']}"
    assert checked >= 8, f"only {checked} hub-api client containers found"


def test_wait_for_hub_api_probes_https_with_ca(dal2: list[dict[str, Any]]) -> None:
    """The shared wait-for-hub-api init container verifies the https /ready with the CA."""
    inits = [
        c for _n, c in _containers(dal2) if c["name"] == "wait-for-hub-api"
    ]  # fmt: skip
    assert len(inits) >= 5
    for c in inits:
        env = _env(c)
        assert env["HUB_API_READY_URL"]["value"].startswith(f"https://{_HUB_API_FQDN}:")
        script = c["args"][0]
        assert "ssl.create_default_context(cadata=pem)" in script
        assert "context=ctx" in script


def test_certificates_are_ecdsa_p256_fqdn_only(dal2: list[dict[str, Any]]) -> None:
    """Four server certs: ECDSA 256, internal-CA ClusterIssuer, SAN = service FQDN only."""
    certs = {
        d["metadata"]["name"]: d["spec"]
        for d in dal2
        if d["kind"] == "Certificate" and d["metadata"]["name"].endswith("-http-tls")
    }
    assert set(certs) == {
        "waddlebot-hub-api-http-tls",
        "waddlebot-hub-webui-http-tls",
        "waddlebot-core-data-http-tls",
        "waddlebot-svc-streaming-http-tls",
    }
    for name, spec in certs.items():
        assert (
            spec["privateKey"]["algorithm"] == "ECDSA"
            and spec["privateKey"]["size"] == 256
        )
        assert spec["issuerRef"]["name"] == "waddlebot-internal-ca"
        assert spec["issuerRef"]["kind"] == "ClusterIssuer"
        assert spec["usages"] == ["server auth"]
        assert len(spec["dnsNames"]) == 1 and spec["dnsNames"][0].endswith(
            _FQDN_SUFFIX
        ), name
        assert "ipAddresses" not in spec


def test_core_data_and_streaming_over_tls(dal2: list[dict[str, Any]]) -> None:
    """F6: hub-api dials core-data (HTTPS) and svc-streaming (TLS port); both servers terminate TLS."""
    hub = _by_component(dal2, "Deployment", "hub-api")["spec"]["template"]["spec"][
        "containers"
    ][0]
    env = _env(hub)
    assert (
        env["ANALYTICS_CORE_API_URL"]["value"]
        == f"https://waddlebot-core-data{_FQDN_SUFFIX}:8040"
    )
    assert (
        env["STREAMING_RTC_URL"]["value"]
        == f"https://waddlebot-svc-streaming{_FQDN_SUFFIX}:8448"
    )

    cd = _by_component(dal2, "Deployment", "core-data")["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert (
        "--certfile" in cd["command"]
        and cd["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    )

    st = _by_component(dal2, "Deployment", "svc-streaming")["spec"]["template"]["spec"]
    senv = _env(st["containers"][0])
    assert senv["STREAMING_TLS_PORT"]["value"] == "8448"
    assert senv["TLS_CERT_FILE"]["value"].endswith("tls.crt")
    assert "INTERNAL_CA_PEM" in senv  # its own hub-api ledger client
    svc = _by_component(dal2, "Service", "svc-streaming")
    assert any(p["name"] == "https" and p["port"] == 8448 for p in svc["spec"]["ports"])


def test_gateway_to_webui_is_reencrypted(dal2: list[dict[str, Any]]) -> None:
    """F4: hub-webui serves TLS and a BackendTLSPolicy verifies it at the service FQDN."""
    web = _by_component(dal2, "Deployment", "hub-webui-v3")["spec"]["template"]["spec"]
    env = _env(web["containers"][0])
    assert env["HUB_API_URL"]["value"] == f"https://{_HUB_API_FQDN}:8204"
    assert env["TLS_KEY_FILE"]["value"].endswith("tls.key")
    assert web["containers"][0]["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    policies = [d for d in dal2 if d["kind"] == "BackendTLSPolicy"]
    assert len(policies) == 1
    spec = policies[0]["spec"]
    assert spec["targetRefs"][0]["name"] == "waddlebot-hub-webui-v3"
    assert spec["validation"]["hostname"] == f"waddlebot-hub-webui-v3{_FQDN_SUFFIX}"
    assert spec["validation"]["caCertificateRefs"][0]["kind"] == "ConfigMap"


def test_alpha_default_is_legacy_http_and_flag_turns_it_on() -> None:
    """values-alpha keeps the legacy wiring (no cert-manager); --set enabled=true flips it to https."""
    off = _render("values-alpha.yaml")
    off_env = [
        e for _n, c in _containers(off) for e in c.get("env", []) if e["name"] == "INTERNAL_TLS_REQUIRED"
    ]  # fmt: skip
    assert not off_env
    assert not [d for d in off if d["kind"] == "BackendTLSPolicy"]
    on = _render("values-alpha.yaml", "global.internalTransitTls.enabled=true")
    hub = _by_component(on, "Deployment", "hub-api")["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert "--certfile" in hub["command"]
    assert any(
        d["kind"] == "Certificate"
        and d["metadata"]["name"].endswith("hub-api-http-tls")
        for d in on
    )


def test_missing_issuer_fails_render_loud() -> None:
    """Enabled with an empty issuer name must fail the render, never produce plaintext wiring."""
    result = _helm(
        "values-dal2alpha.yaml", "global.internalTransitTls.certManager.issuerName="
    )
    assert result.returncode != 0
    assert "issuerName" in (result.stderr + result.stdout)
