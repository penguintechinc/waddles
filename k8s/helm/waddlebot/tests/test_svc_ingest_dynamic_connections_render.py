"""Helm-template assertions for svc-ingest-rust's dynamic Twitch connections wiring.

core/svc_ingest (`src/connections/`, flag `waddles.ingest-dynamic-connections`) resolves
Twitch IRC channels + OAuth tokens from hub-api's internal provider-connections REST routes.
Those routes need a machine JWT with scope `connections:resolve` from the `system` tenant, which
hub-api's token endpoint only mints for a ServiceAccount listed in `serviceJwt.identities`.
Without the identity + dedicated ServiceAccount + projected audience-scoped token, a flag-ON pod
cannot authenticate and refuses to start (fail loud).

Also pins the env-name fix: the Deployment used to set TWITCH_NICK/TWITCH_CHANNELS, which the
Rust binary never read (it reads TWITCH_IRC_NICK/TWITCH_IRC_CHANNEL).

Zero rendered documents, zero matching resources is a hard failure, never a silent pass
(critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_FLAG_ENV = "FLAG_WADDLES_INGEST_DYNAMIC_CONNECTIONS"
_SCOPE = "connections:resolve"
_SA_TOKEN_PATH = "/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token"


def _render(*extra: str) -> list[dict[str, Any]]:
    """`helm template` the chart against alpha values (+ overrides) and return its documents."""
    if not shutil.which("helm"):
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
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _ingest_deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-svc-ingest-rust")
    ]
    assert len(matches) == 1, f"expected exactly one svc-ingest-rust Deployment, got {len(matches)}"
    return matches[0]


def _env(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return {e["name"]: e for e in containers[0]["env"]}


def _identities(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            for env in container.get("env", []):
                if env.get("name") == "SERVICE_JWT_IDENTITIES":
                    found.extend(json.loads(env["value"]))
    assert found, "no SERVICE_JWT_IDENTITIES env var rendered -- cannot be a pass"
    return {entry["k8s_service_account"]: entry for entry in found}


def test_svc_ingest_identity_is_system_tenant_with_exactly_the_resolve_scope() -> None:
    identity = _identities(_render())["svc-ingest"]
    assert identity["allowed_scopes"] == [_SCOPE], "least privilege: one scope, nothing else"
    # hub-api's internal routes reject any token whose tenant claim is not `system`; the issuer
    # stamps it from this entry, never from the caller.
    assert identity["tenant"] == "system"
    assert identity["service_id"].endswith("/svc-ingest")


def test_the_resolve_scope_is_granted_to_no_other_service() -> None:
    for account, identity in _identities(_render()).items():
        if account != "svc-ingest":
            assert _SCOPE not in identity["allowed_scopes"], (
                f"{account} must not be able to resolve decrypted connection tokens"
            )


def test_a_dedicated_svc_ingest_serviceaccount_exists_and_the_deployment_uses_it() -> None:
    docs = _render()
    accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
    assert "svc-ingest" in {a["metadata"]["name"] for a in accounts}
    deployment = _ingest_deployment(docs)
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "svc-ingest"


def test_the_pod_mounts_the_audience_scoped_bootstrap_token_and_points_at_it() -> None:
    deployment = _ingest_deployment(_render())
    env = _env(deployment)
    assert env["SERVICE_JWT_SA_TOKEN_PATH"]["value"] == _SA_TOKEN_PATH
    assert env["SERVICE_JWT_TOKEN_ENDPOINT"]["value"].endswith("/internal/service-token")

    spec = deployment["spec"]["template"]["spec"]
    volume = next(v for v in spec["volumes"] if v["name"] == "service-jwt-bootstrap-token")
    source = volume["projected"]["sources"][0]["serviceAccountToken"]
    # Bound to hub-api's TokenReview audience -- a stolen token is useless against any other
    # in-cluster consumer.
    assert source["audience"] == "hub-api"
    mount = next(
        m
        for m in spec["containers"][0]["volumeMounts"]
        if m["name"] == "service-jwt-bootstrap-token"
    )
    assert mount["readOnly"] is True
    assert _SA_TOKEN_PATH.startswith(mount["mountPath"] + "/")


def test_static_twitch_env_names_match_what_the_binary_reads() -> None:
    env = _env(_ingest_deployment(_render()))
    # core/svc_ingest/src/config.rs: TWITCH_IRC_NICK / TWITCH_IRC_CHANNEL.
    assert "TWITCH_IRC_NICK" in env
    assert "TWITCH_IRC_CHANNEL" in env
    # The legacy names the binary never read must be gone.
    assert "TWITCH_NICK" not in env
    assert "TWITCH_CHANNELS" not in env
    # The binary takes ONE channel -- never a comma list.
    assert "," not in env["TWITCH_IRC_CHANNEL"]["value"]


def test_the_static_channel_list_is_reduced_to_its_first_entry() -> None:
    env = _env(
        _ingest_deployment(_render("--set", "oauth.twitch.channels=#first_chan\\, #second_chan"))
    )
    assert env["TWITCH_IRC_CHANNEL"]["value"] == "#first_chan"


def test_the_flag_is_off_by_default_and_on_only_via_the_alpha_env_baseline() -> None:
    # Alpha values switch it ON through the Docker ENV baseline (no PostHog in alpha)...
    alpha_env = _env(_ingest_deployment(_render()))
    assert alpha_env[_FLAG_ENV]["value"] == "true"
    assert alpha_env["INGEST_DYNAMIC_SCOPE_MODE"]["value"] == "runner"
    # ...and with that override removed (the chart default everywhere else) nothing sets it, so
    # the binary resolves it OFF and the static path runs unchanged.
    off_env = _env(
        _ingest_deployment(
            _render(
                "--set",
                "pipeline.rustDataPlane.svcIngest.extraEnv=null",
                "--set",
                "pipeline.rustDataPlane.svcIngest.dynamicConnections=null",
            )
        )
    )
    assert _FLAG_ENV not in off_env
    # Fail closed: without an explicit runner opt-in the scope mode is `binding`.
    assert off_env["INGEST_DYNAMIC_SCOPE_MODE"]["value"] == "binding"


# -- F4: the grant/mount/env render only when dynamicConnections.enabled ------------------
_GATE = "pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled"


def test_f4_nothing_cross_tenant_renders_when_dynamic_connections_is_disabled() -> None:
    docs = _render("--set", f"{_GATE}=false")
    assert "svc-ingest" not in _identities(docs), "no system-tenant grant for svc-ingest"
    deployment = _ingest_deployment(docs)
    spec = deployment["spec"]["template"]["spec"]
    assert "service-jwt-bootstrap-token" not in [v["name"] for v in spec["volumes"]]
    assert "service-jwt-bootstrap-token" not in [
        m["name"] for m in spec["containers"][0]["volumeMounts"]
    ]
    assert not [n for n in _env(deployment) if n.startswith("SERVICE_JWT_")]


# -- F3: plaintext http:// to hub-api is alpha-only; chart exposes https + explicit ack ----
def test_f3_hub_api_url_defaults_to_in_cluster_http_and_has_no_insecure_ack() -> None:
    env = _env(_ingest_deployment(_render()))
    assert env["HUB_API_URL"]["value"].startswith("http://")
    assert "HUB_API_ALLOW_INSECURE_HTTP" not in env, "the ack must be an explicit opt-in"


def test_f3_hub_api_url_can_be_an_https_base() -> None:
    env = _env(
        _ingest_deployment(
            _render(
                "--set",
                "pipeline.rustDataPlane.svcIngest.dynamicConnections.hubApiUrl=https://hub-api.waddlebot.svc:8204",
            )
        )
    )
    assert env["HUB_API_URL"]["value"] == "https://hub-api.waddlebot.svc:8204"


def test_f3_insecure_http_ack_renders_only_when_explicitly_set() -> None:
    env = _env(
        _ingest_deployment(
            _render(
                "--set",
                "pipeline.rustDataPlane.svcIngest.dynamicConnections.allowInsecureHttp=true",
            )
        )
    )
    assert env["HUB_API_ALLOW_INSECURE_HTTP"]["value"] == "true"
