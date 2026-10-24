"""Helm-template assertions for svc-ingest's per-community Twitch sessions (edge-reshape B2, Task 9).

Complements (never duplicates) test_svc_ingest_connections_scope_render.py, which already pins the
machine-JWT scope matrix. This file pins what B2 adds: INGEST_DYNAMIC_CONNECTIONS + the two Twitch
timing envs on alpha, the absence of the static TWITCH_IRC_NICK/TWITCH_IRC_CHANNEL pair (a FATAL
conflict with the flag ON), the svc-ingest -> hub-api HTTP egress rule, and an unchanged
beta/production render. Zero rendered documents is a hard failure; missing ``helm`` skips locally
but fails in CI.
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
_DATAPLANE = (
    "pipeline.rustDataPlane.enabled=true",
    "pipeline.rustDataPlane.svcIngest.enabled=true",
)
_STATIC_PAIR = {"TWITCH_IRC_NICK", "TWITCH_IRC_CHANNEL"}
_TWITCH_ENVS = {"TWITCH_RECONCILE_INTERVAL_SECS", "TWITCH_RESOLVE_TIMEOUT_SECS"}


def _render(values_file: str, *extra_set: str) -> list[dict[str, Any]]:
    """Render the chart with the given values file and the Rust svc-ingest Deployment on."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART_DIR / values_file),
    ]
    for item in (*_DATAPLANE, *extra_set):
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _ingest_env(docs: list[dict[str, Any]]) -> dict[str, str | None]:
    found = [
        d
        for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"]["name"].endswith("-svc-ingest-rust")
    ]
    assert len(found) == 1, f"expected one svc-ingest-rust Deployment, got {len(found)}"
    env = found[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env, "svc-ingest-rust container has zero env entries"
    return {e["name"]: e.get("value") for e in env}


@pytest.fixture(scope="module")
def alpha() -> list[dict[str, Any]]:
    """Alpha render (dynamicConnections.enabled true)."""
    return _render("values-alpha.yaml")


def test_alpha_renders_dynamic_connections_and_twitch_timing(
    alpha: list[dict[str, Any]],
) -> None:
    env = _ingest_env(alpha)
    assert env["INGEST_DYNAMIC_CONNECTIONS"] == "true"
    assert _TWITCH_ENVS <= set(env), env.keys()
    assert 10 <= int(env["TWITCH_RECONCILE_INTERVAL_SECS"]) <= 3600
    assert int(env["TWITCH_RESOLVE_TIMEOUT_SECS"]) >= 1


def test_alpha_renders_no_static_twitch_irc_pair(alpha: list[dict[str, Any]]) -> None:
    env = _ingest_env(alpha)
    assert len(env) >= 10, "denominator: env list suspiciously small"
    assert not (_STATIC_PAIR & set(env)), (
        "static TWITCH_IRC_* + flag ON is a fatal conflict"
    )


@pytest.mark.parametrize("values_file", ["values-beta.yaml", "values-production.yaml"])
def test_beta_and_production_are_unchanged(values_file: str) -> None:
    # deploymentTier=alpha only relaxes the secret-material guards of a bare render; the svc-ingest env is tier-independent.
    env = _ingest_env(_render(values_file, "global.deploymentTier=alpha"))
    assert "INGEST_DYNAMIC_CONNECTIONS" not in env
    assert not (_TWITCH_ENVS & set(env))
    assert not (_STATIC_PAIR & set(env))


def test_alpha_scope_matrix_still_has_both_connection_scopes(
    alpha: list[dict[str, Any]],
) -> None:
    """Guard against a regression of Phase A's grant (exact matrix lives in the sibling test)."""
    import json

    for doc in alpha:
        if doc.get("kind") != "Deployment":
            continue
        for c in doc["spec"]["template"]["spec"].get("containers", []):
            for e in c.get("env", []) or []:
                if e.get("name") == "SERVICE_JWT_IDENTITIES":
                    ids = {
                        x["service_id"].rsplit("/", 1)[-1]: x
                        for x in json.loads(e["value"])
                    }
                    scopes = ids["svc-ingest"]["allowed_scopes"]
                    assert {"connections:resolve", "connections:list"} <= set(scopes)
                    return
    pytest.fail("no SERVICE_JWT_IDENTITIES rendered")


def test_dataplane_egress_allows_hub_api_http_port_on_alpha(
    alpha: list[dict[str, Any]],
) -> None:
    port = str(
        yaml.safe_load((_CHART_DIR / "values.yaml").read_text())["pipeline"]["hubApi"][
            "port"
        ]
    )
    cnps = [
        d
        for d in alpha
        if d.get("kind") == "CiliumNetworkPolicy"
        and d["metadata"]["name"].endswith("-dataplane-egress-deny")
    ]
    assert len(cnps) == 1, f"expected one dataplane egress CNP, got {len(cnps)}"
    hits = [
        r
        for r in cnps[0]["spec"]["egress"]
        if any(
            m.get("matchLabels", {}).get("app.kubernetes.io/component") == "hub-api"
            for m in r.get("toEndpoints", [])
        )
        and any(p["port"] == port for tp in r.get("toPorts", []) for p in tp["ports"])
    ]
    assert len(hits) == 1, f"svc-ingest -> hub-api:{port} egress rule missing"
