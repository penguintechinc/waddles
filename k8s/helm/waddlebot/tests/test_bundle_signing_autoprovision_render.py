"""Helm-template assertions for fix/chart-autogen-bundle-signing-key.

Regression this covers: `core/bundle_executor` fail-closes at startup unless
`BUNDLE_SIGNING_PUBLIC_KEYS` is set, and the chart required a human to run
`make generate-bundle-signing-key` and hand-paste the printed public key into
`pipeline.rustDataPlane.bundleSigningPublicKeys` before ANY deploy (including
alpha, which already self-provisions the underlying private key Secret via
`templates/auto-provisioned-keys-job.yaml`) could succeed.

Fix: `templates/auto-provisioned-keys-job.yaml`'s pre-install/pre-upgrade hook
now also publishes a JSON-map-shaped `BUNDLE_SIGNING_PUBLIC_KEYS` entry into the
`<fullname>-bundle-signing-public` ConfigMap it already derives+publishes from
whatever private key Secret is present; `templates/bundle-executor.yaml` and
`bundle-executor-action.yaml` source the env var from that ConfigMap by default,
falling back to an explicit `pipeline.rustDataPlane.bundleSigningPublicKeys`
values override when one is set (backward compat / multi-key rotation).

`lookup` is irrelevant here (no live-cluster KEEP/GENERATE branch is under
test) -- this only exercises render-time template logic, which `helm template`
proves directly and deterministically.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


def _helm_template(values_file: Path, extra_set: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    args = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(values_file),
    ]
    for kv in extra_set or []:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell, test-only


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


def _render_docs(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    assert result.returncode == 0, f"helm template failed:\n{result.stdout}\n{result.stderr}"
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _container_env(docs: list[dict[str, Any]], deployment_name_suffix: str, container_name: str) -> dict[str, Any]:
    deployments = [
        doc for doc in docs
        if doc.get("kind") == "Deployment" and doc.get("metadata", {}).get("name", "").endswith(deployment_name_suffix)
    ]
    assert deployments, f"no Deployment ending in {deployment_name_suffix!r} found"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    matches = [c for c in containers if c["name"] == container_name]
    assert matches, f"no container named {container_name!r} in {deployment_name_suffix!r}"
    env_by_name = {}
    for item in matches[0].get("env", []):
        env_by_name[item["name"]] = item
    return env_by_name


class TestBundleExecutorSourcesPublicKeysFromConfigMapByDefault:
    """No values override set -- both executors must fall back to the
    auto-provisioned ConfigMap, never a bare empty `{}` (the exact crash this
    fix closes: bundle-executor fail-closing at startup on alpha).
    """

    def test_bundle_executor_uses_configmap_ref(self) -> None:
        docs = _render_docs(_helm_template(_ALPHA_VALUES))
        env = _container_env(docs, "-bundle-executor", "bundle-executor")
        entry = env["BUNDLE_SIGNING_PUBLIC_KEYS"]
        assert "value" not in entry, "must not fall back to a literal empty/static value"
        assert entry["valueFrom"]["configMapKeyRef"]["name"] == "waddlebot-bundle-signing-public"
        assert entry["valueFrom"]["configMapKeyRef"]["key"] == "BUNDLE_SIGNING_PUBLIC_KEYS"

    def test_bundle_executor_action_uses_configmap_ref(self) -> None:
        docs = _render_docs(_helm_template(_ALPHA_VALUES))
        env = _container_env(docs, "-bundle-executor-action", "bundle-executor")
        entry = env["BUNDLE_SIGNING_PUBLIC_KEYS"]
        assert "value" not in entry
        assert entry["valueFrom"]["configMapKeyRef"]["name"] == "waddlebot-bundle-signing-public"
        assert entry["valueFrom"]["configMapKeyRef"]["key"] == "BUNDLE_SIGNING_PUBLIC_KEYS"


class TestExplicitValuesOverrideStillWins:
    """Backward compat: an operator-supplied values override (multi-key
    rotation, or a manually-run scripts/generate-bundle-signing-key.sh) must
    still take effect instead of the ConfigMap.
    """

    def test_bundle_executor_literal_override_wins(self) -> None:
        docs = _render_docs(
            _helm_template(_ALPHA_VALUES, extra_set=["pipeline.rustDataPlane.bundleSigningPublicKeys.k1=AAAA"])
        )
        env = _container_env(docs, "-bundle-executor", "bundle-executor")
        entry = env["BUNDLE_SIGNING_PUBLIC_KEYS"]
        assert "valueFrom" not in entry
        assert entry["value"] == '{"k1":"AAAA"}'


class TestAutoProvisionJobPublishesJsonMapForBundleSigningOnly:
    """The hook Job's shared `provision_ed25519` helper must only emit the
    JSON-map convenience key for bundleSigning -- serviceJwt consumes its
    ConfigMap differently (per-kid literals on the hub-api side) and must be
    unaffected by this change.
    """

    def test_bundle_signing_call_requests_json_key(self) -> None:
        result = _helm_template(_ALPHA_VALUES)
        assert result.returncode == 0, f"helm template failed:\n{result.stdout}\n{result.stderr}"
        assert (
            "BUNDLE_SIGNING_PRIVATE_KEY BUNDLE_SIGNING_KEY_ID BUNDLE_SIGNING_PUBLIC_KEY "
            "BUNDLE_SIGNING_PUBLIC_KEYS" in result.stdout
        )

    def test_service_jwt_call_unchanged_no_json_key(self) -> None:
        result = _helm_template(_ALPHA_VALUES)
        assert result.returncode == 0
        # kid-suffixed PEM private key name + empty json-key slot + "pem" format
        # (fix/hub-api-grpc-service-jwt-issuer); still no JSON-map key.
        assert (
            'SERVICE_JWT_ACTIVE_KID SERVICE_JWT_PUBLIC_KEY "" pem' in result.stdout
        )
        assert "SERVICE_JWT_PUBLIC_KEYS" not in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
