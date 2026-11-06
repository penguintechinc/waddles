"""Helm-template assertions for PR-H (connector runtime / inbound manager wiring).

Spec: docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md SS1.3/SS2/SS5/SS7/SS8.
All connectors OFF (default, incl. values-alpha/dal2alpha) must render none of it; each ON
combination must render the right env on the right deployments; missing prerequisites and a
Discord connector must fail the render. Zero rendered documents / zero matches is a hard failure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_HELM = os.environ.get("HELM_BIN", "helm")
_D = "sha256:" + "b" * 64
_RT = "pipeline.rustDataPlane.svcIngest.connectorRuntime"
_CT = "pipeline.rustDataPlane.svcIngest.connectorTools"
_BASE = [
    "--set", "pipeline.svcIngest.enabled=true",  # the Python svc-ingest (off in alpha) must see the connector env too
    "--set", f"{_CT}.enabled=true",
    "--set", f"pipeline.connectors.digests.twitch={_D}",
    "--set", f"pipeline.connectors.digests.discord={_D}",
    "--set", f"pipeline.connectors.digests.youtube={_D}",
]


def _yt(*extra: str) -> list[str]:
    return [*_BASE, "--set", f"{_RT}.connector.youtube.enabled=true", *extra]


def _render(*extra: str, values: str = "values-alpha.yaml") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [_HELM, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_CHART_DIR / values), *extra],
        capture_output=True, text=True, check=False,
    )


def _docs(extra: list[str], values: str = "values-alpha.yaml") -> list[dict[str, Any]]:
    res = _render(*extra, values=values)
    assert res.returncode == 0, res.stderr[-600:]
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm rendered zero documents"
    return docs


def _dep(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    found = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(suffix)]
    assert len(found) == 1, f"expected exactly one *{suffix} Deployment, got {len(found)}"
    return found[0]


def _env(dep: dict[str, Any]) -> dict[str, Any]:
    return {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def _secret_vols(dep: dict[str, Any]) -> set[str]:
    return {v["secret"]["secretName"] for v in dep["spec"]["template"]["spec"].get("volumes", []) if "secret" in v}


_RUNTIME_ENV = (
    "CONNECTOR_RUNTIME_ENABLED", "CONNECTOR_TWITCH_ENABLED", "CONNECTOR_DISCORD_ENABLED",
    "CONNECTOR_YOUTUBE_ENABLED", "CONNECTOR_WIX_ENABLED", "SENDER_EXEC_DIGEST_WIX", "CONNECTOR_RECONCILE_INTERVAL_SECS", "CONNECTOR_HEARTBEAT_ENABLED", "CONNECTOR_MAX_CONNECTIONS",
    "CONNECTOR_START_CONCURRENCY", "CONNECTOR_DISCORD_START_PACE_MS",
    "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS", "CONNECTOR_ROUTE_KEY_SECRET",
    "CONNECTOR_HOST_API_ADDR", "CONNECTOR_HOST_API_IDENTITY", "EXECUTOR_CONNECTOR_CONNECTIONS",
    "CONNECTOR_PIN_DIGEST_TWITCH", "CONNECTOR_PIN_DIGEST_DISCORD", "CONNECTOR_PIN_DIGEST_YOUTUBE",
)


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml", "values-local.yaml"])
def test_all_off_renders_nothing_new(values: str) -> None:
    docs = _docs([], values)
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            names = {e["name"] for e in c.get("env", [])}
            assert not (names & set(_RUNTIME_ENV)), (d["metadata"]["name"], names & set(_RUNTIME_ENV))
    assert not [d for d in docs if d["metadata"]["name"].endswith("-svc-ingest-host-api")]


def test_youtube_on_renders_env_on_both_ingest_deployments_from_one_value() -> None:
    docs = _docs(_yt())
    rust, py, ex = (_dep(docs, "-svc-ingest-rust"), _dep(docs, "-svc-ingest"), _dep(docs, "-bundle-executor"))
    for dep in (rust, py):
        env = _env(dep)
        assert env["CONNECTOR_YOUTUBE_ENABLED"]["value"] == "true"
        assert env["CONNECTOR_TWITCH_ENABLED"]["value"] == "false"
        assert env["CONNECTOR_DISCORD_ENABLED"]["value"] == "false"
        assert env["CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS"]["value"] == ""
    penv = _env(py)
    assert "CONNECTOR_RECONCILE_INTERVAL_SECS" not in penv and "CONNECTOR_ROUTE_KEY_SECRET" not in penv
    renv = _env(rust)
    assert renv["CONNECTOR_RUNTIME_ENABLED"]["value"] == "true"
    assert renv["CONNECTOR_RECONCILE_INTERVAL_SECS"]["value"] == "60"
    assert renv["CONNECTOR_MAX_CONNECTIONS"]["value"] == "500"
    assert renv["CONNECTOR_START_CONCURRENCY"]["value"] == "8"
    assert renv["CONNECTOR_DISCORD_START_PACE_MS"]["value"] == "5500"
    assert "CONNECTOR_ROUTE_KEY_SECRET" not in renv
    assert renv["HOST_API_CLIENT_IDENTITY"]["value"].endswith("-ingest-executor")
    for p in ("TWITCH", "DISCORD", "YOUTUBE"):
        assert renv[f"SENDER_EXEC_DIGEST_{p}"]["value"] == _D  # one source of truth ...
        assert _env(ex)[f"CONNECTOR_PIN_DIGEST_{p}"]["value"] == _D  # ... feeding both


def test_executor_env_and_distinct_client_secret_mounted_only_there() -> None:
    docs = _docs(_yt())
    ex = _dep(docs, "-bundle-executor")
    env = _env(ex)
    assert env["CONNECTOR_HOST_API_ADDR"]["value"].endswith("-svc-ingest-rust:8301")
    assert env["CONNECTOR_HOST_API_IDENTITY"]["value"].endswith("-svc-ingest-rust")
    assert env["EXECUTOR_CONNECTOR_CONNECTIONS"]["value"] == "1"
    # Exact names the executor reads (core/bundle_executor/src/config.rs): dedicated cert/key, CA reused.
    assert env["CONNECTOR_HOST_API_CLIENT_CERT_FILE"]["value"].endswith("/tls.crt")
    assert env["CONNECTOR_HOST_API_CLIENT_KEY_FILE"]["value"].endswith("/tls.key")
    assert env["CONNECTOR_HOST_API_CLIENT_CERT_FILE"]["value"] != env["HOST_API_CLIENT_CERT_FILE"]["value"]
    assert env["CONNECTOR_HOST_API_CLIENT_KEY_FILE"]["value"] != env["HOST_API_CLIENT_KEY_FILE"]["value"]
    assert "HOST_API_CA_FILE" in env
    assert "CONNECTOR_HOST_API_CA_FILE" not in env
    cli = [n for n in _secret_vols(ex) if n.endswith("-ingest-executor-tls")]
    assert len(cli) == 1
    # Existing stage secret stays; the connector client secret is a SECOND, separate volume.
    assert any(n.endswith("-host-api-tls") and "ingest" not in n for n in _secret_vols(ex))
    owners = {d["metadata"]["name"] for d in docs if d.get("kind") == "Deployment" and cli[0] in _secret_vols(d)}
    assert owners == {ex["metadata"]["name"]}
    ingest_secret = [d for d in docs if d.get("kind") == "Deployment" and
                     any(n.endswith("-svc-ingest-host-api-tls") for n in _secret_vols(d))]
    assert [d["metadata"]["name"] for d in ingest_secret] == [_dep(docs, "-svc-ingest-rust")["metadata"]["name"]]
    assert cli[0] not in _secret_vols(_dep(docs, "-svc-ingest-rust"))


def test_executor_client_cert_san_is_executor_identity_only() -> None:
    crypto = pytest.importorskip("cryptography.x509")
    docs = _docs(_yt())
    sec = next(d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-ingest-executor-tls"))
    cert = crypto.load_pem_x509_certificate(sec["stringData"]["tls.crt"].encode())
    sans = cert.extensions.get_extension_for_class(crypto.SubjectAlternativeName).value.get_values_for_type(crypto.DNSName)
    assert sans == [_env(_dep(docs, "-svc-ingest-rust"))["HOST_API_CLIENT_IDENTITY"]["value"]]


def test_twitch_webhook_route_key_comes_from_existing_secret_only() -> None:
    extra = ["--set", f"{_RT}.connector.twitch.enabled=true", "--set", f"{_RT}.webhookPlatforms={{twitch}}",
             "--set", f"{_RT}.routeKeySecret.existingSecret=ops-route-key"]
    res = _render(*_BASE, *extra)
    assert res.returncode == 0, res.stderr[-600:]
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    renv = _env(_dep(docs, "-svc-ingest-rust"))
    assert renv["CONNECTOR_ROUTE_KEY_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "ops-route-key", "key": "CONNECTOR_ROUTE_KEY_SECRET"}
    assert "value" not in renv["CONNECTOR_ROUTE_KEY_SECRET"]
    assert renv["CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS"]["value"] == "twitch"
    assert _env(_dep(docs, "-svc-ingest"))["CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS"]["value"] == "twitch"
    assert "CONNECTOR_ROUTE_KEY_SECRET" not in _env(_dep(docs, "-svc-ingest"))
    assert "ops-route-key" not in "".join(
        e.get("value", "") for dep in docs if dep.get("kind") == "Deployment"
        for c in dep["spec"]["template"]["spec"]["containers"] for e in c.get("env", []))


def test_wix_on_renders_env_digest_and_route_key_wiring() -> None:
    wix = [*_BASE, "--set", f"pipeline.connectors.digests.wix={_D}",
           "--set", f"{_RT}.connector.wix.enabled=true", "--set", f"{_RT}.webhookPlatforms={{wix}}",
           "--set", f"{_RT}.routeKeySecret.existingSecret=ops-route-key"]
    docs = _docs(wix)
    for suffix in ("-svc-ingest-rust", "-svc-ingest"):
        env = _env(_dep(docs, suffix))
        assert env["CONNECTOR_WIX_ENABLED"]["value"] == "true"
        assert env["CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS"]["value"] == "wix"
        assert env["CONNECTOR_YOUTUBE_ENABLED"]["value"] == "false"
    renv = _env(_dep(docs, "-svc-ingest-rust"))
    assert renv["SENDER_EXEC_DIGEST_WIX"]["value"] == _D
    assert renv["CONNECTOR_ROUTE_KEY_SECRET"]["valueFrom"]["secretKeyRef"]["name"] == "ops-route-key"
    off = _docs(_yt())
    assert _env(_dep(off, "-svc-ingest-rust"))["CONNECTOR_WIX_ENABLED"]["value"] == "false"
    assert "SENDER_EXEC_DIGEST_WIX" not in _env(_dep(off, "-svc-ingest-rust"))


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (["--set", f"{_RT}.connector.youtube.enabled=true"], "connectorTools"),
        ([*_BASE, "--set", f"{_RT}.connector.wix.enabled=true"], "digests.wix"),
        ([*_BASE[:8], "--set", f"{_RT}.connector.youtube.enabled=true"], "digests.youtube"),
        ([*_BASE, "--set", f"{_RT}.connector.twitch.enabled=true", "--set", f"{_RT}.webhookPlatforms={{twitch}}"],
         "routeKeySecret"),
        ([*_BASE, "--set", f"{_RT}.webhookPlatforms={{slack}}", "--set", f"{_RT}.enabled=true"], "webhookPlatforms"),
        ([*_BASE, "--set", f"{_CT}.digests.twitch={_D}"], "single source of truth"),
        ([*_BASE, "--set", f"{_RT}.enabled=true", "--set", f"{_CT}.hostApiTls.clientIdentity=spiffe-stage"
          "", "--set", "global.hostApiTls.stageIdentity=spiffe-stage"], "DISTINCT"),
    ],
)
def test_missing_prerequisites_fail_render(bad: list[str], needle: str) -> None:
    res = _render(*bad)
    assert res.returncode != 0
    assert needle in res.stderr, res.stderr[-500:]


def test_discord_connector_rejected_unless_identify_budget_ready() -> None:
    # Spec change (Discord flag-on): alpha/dal2alpha now default identifyBudgetReady=true, so the
    # refusal is pinned by forcing it false (the base/beta/gamma/production default).
    on = [*_BASE, "--set", f"{_RT}.connector.discord.enabled=true"]
    res = _render(*on, "--set", f"{_RT}.identifyBudgetReady=false")
    assert res.returncode != 0
    assert "discord connector requires the identify budget (not yet ported)" in res.stderr
    ok = _render(*on, "--set", f"{_RT}.identifyBudgetReady=true")
    assert ok.returncode == 0, ok.stderr[-500:]


@pytest.mark.parametrize(
    ("values", "want"),
    [
        ("values-alpha.yaml", True),
        ("values-dal2alpha.yaml", True),
        ("values-local.yaml", True),
        ("values.yaml", False),
        ("values-beta.yaml", False),
        ("values-gamma.yaml", False),
        ("values-production.yaml", False),
    ],
)
def test_identify_budget_ready_only_in_alpha_and_local_values(values: str, want: bool) -> None:
    loaded = yaml.safe_load((_CHART_DIR / values).read_text()) or {}
    rt = (
        loaded.get("pipeline", {}).get("rustDataPlane", {}).get("svcIngest", {}).get("connectorRuntime", {})
    )
    assert bool(rt.get("identifyBudgetReady", False)) is want, values
    if values == "values-alpha.yaml":  # the only file whose other render prerequisites _BASE satisfies
        on = [*_BASE, "--set", f"{_RT}.connector.discord.enabled=true"]
        res = _render(*on, values=values)
        assert res.returncode == 0, res.stderr[-500:]


def test_network_policies_are_cilium_and_scoped() -> None:
    docs = _docs(_yt())
    cnp = next(d for d in docs if d["metadata"]["name"].endswith("-svc-ingest-host-api"))
    assert cnp["kind"] == "CiliumNetworkPolicy"
    rules = cnp["spec"]["ingress"]
    assert len(rules) == 1
    assert rules[0]["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "bundle-executor"
    assert rules[0]["toPorts"][0]["ports"] == [{"port": "8301", "protocol": "TCP"}]
    # Only these two CNPs (ingest ingress-from-executor, executor egress) mention 8301.
    opens = sorted(d["metadata"]["name"] for d in docs
                   if d["kind"] == "CiliumNetworkPolicy" and '"8301"' in yaml.safe_dump(d["spec"]).replace("'", '"'))
    assert len(opens) == 2 and opens[0].endswith("-bundle-executor") and opens[1].endswith("-svc-ingest-host-api"), opens
    ex = next(d for d in docs if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-bundle-executor"))
    hits = [r for r in ex["spec"]["egress"]
            if r.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("app.kubernetes.io/component") == "svc-ingest-rust"]
    assert len(hits) == 1 and hits[0]["toPorts"][0]["ports"] == [{"port": "8301", "protocol": "TCP"}]
    plain = [d for d in docs if d["kind"] == "NetworkPolicy" and "host-api" in d["metadata"]["name"]]
    assert not plain


def test_security_context_unchanged_and_no_literal_secrets() -> None:
    res = _render(*_yt())
    assert res.returncode == 0
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    for suffix in ("-svc-ingest-rust", "-bundle-executor", "-svc-ingest"):
        c = _dep(docs, suffix)["spec"]["template"]["spec"]["containers"][0]
        assert c["securityContext"]["runAsNonRoot"] is True
        assert c["securityContext"]["allowPrivilegeEscalation"] is False
    for dep in docs:
        if dep.get("kind") != "Deployment":
            continue
        for c in dep["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []):
                if "ROUTE_KEY" in e["name"] or e["name"].endswith(("_SECRET", "_TOKEN")):
                    assert "value" not in e or not e["value"].strip() or "/" in e["value"] or e["value"].startswith("http"), e["name"]


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml", "values-local.yaml"])
def test_helm_lint_clean(values: str) -> None:
    res = subprocess.run(  # noqa: S603 -- fixed argv, test-only
        [_HELM, "lint", str(_CHART_DIR), "--values", str(_CHART_DIR / values)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stdout[-600:] + res.stderr[-300:]
    assert "0 chart(s) failed" in res.stdout


def _ca_of(docs: list[dict[str, Any]], suffix: str) -> Any:
    """Return the parsed ca.crt x509 of the Secret whose name ends with `suffix`."""
    crypto = pytest.importorskip("cryptography.x509")
    sec = next(d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith(suffix)
               and (suffix != "-host-api-tls" or "ingest" not in d["metadata"]["name"]))
    return crypto.load_pem_x509_certificate(sec["stringData"]["ca.crt"].encode()), sec


def test_alpha_connector_certs_use_a_separate_ca_and_executor_trusts_both() -> None:
    """Stage servers only chain-verify clients, so connector certs MUST NOT chain to the stage CA.
    The executor trusts stage CA + connector CA through a bundle mounted over HOST_API_CA_FILE."""
    crypto = pytest.importorskip("cryptography.x509")
    hashes = pytest.importorskip("cryptography.hazmat.primitives.hashes")
    docs = _docs(_yt())
    stage, stage_sec = _ca_of(docs, "-host-api-tls")
    srv_ca, srv = _ca_of(docs, "-svc-ingest-host-api-tls")
    cli_ca, cli = _ca_of(docs, "-ingest-executor-tls")
    fp = lambda c: c.fingerprint(hashes.SHA256())  # noqa: E731
    assert fp(srv_ca) == fp(cli_ca) and fp(srv_ca) != fp(stage)
    for sec in (srv, cli):
        leaf = crypto.load_pem_x509_certificate(sec["stringData"]["tls.crt"].encode())
        assert leaf.issuer == srv_ca.subject and leaf.issuer != stage.subject
    # the stage cert must not verify as anything signed by the connector CA and vice versa
    stage_leaf = crypto.load_pem_x509_certificate(stage_sec["stringData"]["tls.crt"].encode())
    assert stage_leaf.issuer == stage.subject
    # SANs distinct; svc-ingest cert SAN is not any stage identity
    def sans(sec: dict[str, Any]) -> set[str]:
        c = crypto.load_pem_x509_certificate(sec["stringData"]["tls.crt"].encode())
        return set(c.extensions.get_extension_for_class(crypto.SubjectAlternativeName).value.get_values_for_type(crypto.DNSName))
    stage_sans = sans(stage_sec)
    assert not (sans(srv) & sans(cli)) and not (sans(srv) & stage_sans) and not (sans(cli) & stage_sans)
    # bundle = stage CA + connector CA, mounted over the executor's HOST_API_CA_FILE path
    bundle = next(d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-executor-ca-bundle"))
    pems = bundle["stringData"]["ca.crt"].split("-----END CERTIFICATE-----")
    certs = [crypto.load_pem_x509_certificate((p + "-----END CERTIFICATE-----").encode()) for p in pems if "BEGIN" in p]
    assert {fp(c) for c in certs} == {fp(stage), fp(srv_ca)}
    ex = _dep(docs, "-bundle-executor")
    mounts = ex["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    m = next(m for m in mounts if m["name"] == "executor-ca-bundle")
    assert m["mountPath"] == _env(ex)["HOST_API_CA_FILE"]["value"] and m["subPath"] == "ca.crt"


def test_external_connector_secrets_require_executor_ca_bundle_secret() -> None:
    ext = ["--set", f"{_CT}.hostApiTls.externalSecret=true"]
    res = _render(*_yt(*ext))
    assert res.returncode != 0 and "executorCaBundleSecretName" in res.stderr
    ok = _render(*_yt(*ext, "--set", f"{_CT}.hostApiTls.executorCaBundleSecretName=my-bundle"))
    assert ok.returncode == 0, ok.stderr[-600:]
    docs = [d for d in yaml.safe_load_all(ok.stdout) if d]
    vols = _dep(docs, "-bundle-executor")["spec"]["template"]["spec"]["volumes"]
    assert next(v for v in vols if v["name"] == "executor-ca-bundle")["secret"]["secretName"] == "my-bundle"


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml", "values-local.yaml"])
def test_connectors_off_mints_no_connector_secrets_and_never_fails(values: str) -> None:
    """Connector connectors OFF: render succeeds, no connector Secret is emitted, and the stage
    Secret is rendered exactly as before (the connector branch is gated on connectorTools.enabled;
    helm template cannot stub `lookup`, so the existing-stage-Secret case is covered by that gate)."""
    docs = _docs([], values)
    names = [d["metadata"]["name"] for d in docs if d.get("kind") == "Secret"]
    assert not [n for n in names if n.endswith(("-ingest-executor-tls", "-svc-ingest-host-api-tls"))]


def test_heartbeat_env_defaults_false_and_is_settable() -> None:
    """CONNECTOR_HEARTBEAT_ENABLED mirrors the Rust default (false) and a strict "true"/"false" value."""
    ing = _dep(_docs(_yt()), "-svc-ingest-rust")
    assert _env(ing)["CONNECTOR_HEARTBEAT_ENABLED"]["value"] == "false"
    on = _dep(_docs(_yt("--set", f"{_RT}.heartbeatEnabled=true")), "-svc-ingest-rust")
    assert _env(on)["CONNECTOR_HEARTBEAT_ENABLED"]["value"] == "true"


_SPECTRUM = "pipeline.connectors.digests.spectrum"


def _spectrum(*extra: str) -> list[str]:
    return [*_BASE, "--set", f"{_SPECTRUM}={_D}", "--set", f"{_RT}.connector.spectrum.enabled=true", *extra]


def test_spectrum_on_renders_env_digest_and_pin_from_one_value() -> None:
    docs = _docs(_spectrum())
    rust, py, ex = (_dep(docs, "-svc-ingest-rust"), _dep(docs, "-svc-ingest"), _dep(docs, "-bundle-executor"))
    for dep in (rust, py):
        env = _env(dep)
        assert env["CONNECTOR_SPECTRUM_ENABLED"]["value"] == "true"
        assert env["CONNECTOR_YOUTUBE_ENABLED"]["value"] == "false"
        assert env["CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS"]["value"] == ""  # Spectrum has no webhook
    assert _env(rust)["CONNECTOR_RUNTIME_ENABLED"]["value"] == "true"
    assert _env(rust)["SENDER_EXEC_DIGEST_SPECTRUM"]["value"] == _D
    assert _env(ex)["CONNECTOR_PIN_DIGEST_SPECTRUM"]["value"] == _D


def test_spectrum_off_is_the_default_and_renders_no_spectrum_digest() -> None:
    docs = _docs(_yt())
    rust, py = _dep(docs, "-svc-ingest-rust"), _dep(docs, "-svc-ingest")
    for dep in (rust, py):
        assert _env(dep)["CONNECTOR_SPECTRUM_ENABLED"]["value"] == "false"
    assert "SENDER_EXEC_DIGEST_SPECTRUM" not in _env(rust)
    assert "CONNECTOR_PIN_DIGEST_SPECTRUM" not in _env(_dep(docs, "-bundle-executor"))


@pytest.mark.parametrize("values", ["values.yaml", "values-alpha.yaml", "values-dal2alpha.yaml", "values-local.yaml"])
def test_spectrum_is_off_in_every_values_file(values: str) -> None:
    chart = yaml.safe_load((_CHART_DIR / values).read_text())
    rt = chart.get("pipeline", {}).get("rustDataPlane", {}).get("svcIngest", {}).get("connectorRuntime", {})
    assert rt.get("connector", {}).get("spectrum", {"enabled": False}).get("enabled") is False
    assert chart.get("pipeline", {}).get("connectors", {}).get("digests", {}).get("spectrum", "") == ""


def test_spectrum_on_without_its_digest_fails_render() -> None:
    res = _render(*_BASE, "--set", f"{_RT}.connector.spectrum.enabled=true")
    assert res.returncode != 0
    assert "digests.spectrum" in res.stderr, res.stderr[-500:]


def test_spectrum_has_no_webhook_surface() -> None:
    res = _render(*_spectrum(), "--set", f"{_RT}.webhookPlatforms={{spectrum}}")
    assert res.returncode != 0
    assert "webhookPlatforms" in res.stderr, res.stderr[-500:]
