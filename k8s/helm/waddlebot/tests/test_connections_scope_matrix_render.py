"""Helm-render assertions for the provider-connection scope matrix (least privilege per identity).

Matrix (rendered into hub-api's ``SERVICE_JWT_IDENTITIES``):

* svc-action: ``connections:resolve`` + ``connections:resolve:static`` (static Lambda/GCP/HTTP kinds),
  never ``connections:list``.
* svc-ingest: ``connections:resolve`` + ``connections:list`` (twitch OAuth + binding discovery),
  never ``connections:resolve:static``.
* every other identity: no ``connections:*`` scope at all.
* each connections grant is gated by its own ``dynamicConnections.enabled`` value (default false);
  svc-action's non-connection scopes survive the gate being off.

Also pins the SPIFFE env defaults and the legacy Python svc-action SA-token wiring. Zero rendered
documents / identities is a hard failure (Verification Integrity); ``helm`` missing skips locally
but FAILS under CI.
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

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


def _render(*extra_set: str) -> list[dict[str, Any]]:
    """Render the alpha chart (Rust data plane + legacy svc-action on) with extra --set values."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    sets = [
        "pipeline.rustDataPlane.enabled=true",
        "pipeline.rustDataPlane.svcIngest.enabled=true",
        "pipeline.svcAction.enabled=true",
        *extra_set,
    ]
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0", "--values", str(_ALPHA_VALUES),
    ]  # fmt: skip
    for item in sets:
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _env(docs: list[dict[str, Any]], deployment_suffix: str) -> dict[str, str]:
    found = [
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(deployment_suffix)
    ]  # fmt: skip
    assert len(found) == 1, (
        f"expected exactly one {deployment_suffix}, got {len(found)}"
    )
    env: dict[str, str] = {}
    for container in found[0]["spec"]["template"]["spec"]["containers"]:
        for item in container.get("env", []) or []:
            env[item["name"]] = item.get("value", "")
    return env


def _identities(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    raw = _env(docs, "-hub-api-v3")["SERVICE_JWT_IDENTITIES"]
    return {e["service_id"].rsplit("/", 1)[-1]: e for e in json.loads(raw)}


@pytest.fixture(scope="module")
def enabled() -> list[dict[str, Any]]:
    """Alpha render: both dynamicConnections flags on."""
    return _render()


@pytest.fixture(scope="module")
def disabled() -> list[dict[str, Any]]:
    """Both dynamicConnections flags off (the beta/prod default)."""
    return _render(
        "pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=false",
        "pipeline.rustDataPlane.svcAction.dynamicConnections.enabled=false",
    )


def test_svc_action_identity_has_resolve_and_static(
    enabled: list[dict[str, Any]],
) -> None:
    scopes = _identities(enabled)["svc-action"]["allowed_scopes"]
    assert "connections:resolve" in scopes
    assert "connections:resolve:static" in scopes
    assert "connections:list" not in scopes
    assert {"identity:displayname:read", "egress:connect"} <= set(scopes)


def test_svc_ingest_identity_gets_list_and_resolve_not_static(
    enabled: list[dict[str, Any]],
) -> None:
    scopes = _identities(enabled)["svc-ingest"]["allowed_scopes"]
    assert {"connections:resolve", "connections:list"} <= set(scopes)
    assert "connections:resolve:static" not in scopes


def test_no_other_identity_holds_any_connections_scope(
    enabled: list[dict[str, Any]],
) -> None:
    identities = _identities(enabled)
    assert len(identities) >= 4, identities.keys()
    examined = 0
    for name, entry in identities.items():
        if name in {"svc-action", "svc-ingest"}:
            continue
        examined += 1
        assert not [
            s for s in entry["allowed_scopes"] if s.startswith("connections:")
        ], name
    assert examined >= 2


def test_connections_identities_are_system_tenant_and_dedicated_sa(
    enabled: list[dict[str, Any]],
) -> None:
    identities = _identities(enabled)
    for name in ("svc-action", "svc-ingest"):
        assert identities[name]["tenant"] == "system"
        assert identities[name]["k8s_service_account"] == name


def test_identities_absent_when_feature_disabled(
    disabled: list[dict[str, Any]],
) -> None:
    identities = _identities(disabled)
    assert "svc-ingest" not in identities
    assert len(identities) >= 3, identities.keys()
    for name, entry in identities.items():
        assert not [
            s for s in entry["allowed_scopes"] if s.startswith("connections:")
        ], name
    # svc-action itself survives with its non-connection scopes.
    assert "egress:connect" in identities["svc-action"]["allowed_scopes"]


def test_each_flag_gates_only_its_own_grant() -> None:
    only_action = _identities(
        _render("pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=false")
    )
    assert "svc-ingest" not in only_action
    assert "connections:resolve:static" in only_action["svc-action"]["allowed_scopes"]
    only_ingest = _identities(
        _render("pipeline.rustDataPlane.svcAction.dynamicConnections.enabled=false")
    )
    assert "connections:list" in only_ingest["svc-ingest"]["allowed_scopes"]
    assert "connections:resolve" not in only_ingest["svc-action"]["allowed_scopes"]


def test_flags_default_off_in_base_values_and_on_in_alpha() -> None:
    base = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())["pipeline"][
        "rustDataPlane"
    ]
    alpha = yaml.safe_load(_ALPHA_VALUES.read_text())["pipeline"]["rustDataPlane"]
    assert base["svcAction"]["dynamicConnections"]["enabled"] is False
    assert alpha["svcAction"]["dynamicConnections"]["enabled"] is True


def test_legacy_python_svc_action_mounts_token_and_gets_mint_env_when_enabled(
    enabled: list[dict[str, Any]], disabled: list[dict[str, Any]]
) -> None:
    pods = [
        d for d in enabled
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-svc-action")
    ]  # fmt: skip
    assert len(pods) == 1
    spec = pods[0]["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "svc-action"
    volume = next(
        v for v in spec["volumes"] if v["name"] == "service-jwt-bootstrap-token"
    )
    assert (
        volume["projected"]["sources"][0]["serviceAccountToken"]["audience"]
        == "hub-api"
    )
    env = _env(enabled, "-svc-action")
    assert env["SERVICE_JWT_SA_TOKEN_PATH"].endswith("/service-jwt-bootstrap-token")
    assert env["SERVICE_JWT_TOKEN_ENDPOINT"].endswith("/internal/service-token")
    assert "SERVICE_JWT_SA_TOKEN_PATH" not in _env(disabled, "-svc-action")


def test_spiffe_peer_env_defaults_are_safe(enabled: list[dict[str, Any]]) -> None:
    env = _env(enabled, "-hub-api-v3")
    assert env["SPIFFE_PEER_TRUST"] == "false"
    assert env["SPIFFE_PEER_ID_HEADER"] == "X-Spiffe-Peer-Id"
