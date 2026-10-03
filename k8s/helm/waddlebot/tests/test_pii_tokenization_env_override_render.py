"""Helm-template assertions for fix/pii-tokenization-env-override.

`pipeline.piiTokenization.enabled` gates a plain env/values off-switch for PII
tokenization/detokenization, independent of the `waddles.core.disable-pii-
tokenization`/`disable-pii-detokenization` PostHog kill-switches (unusable in an
environment with no in-cluster PostHog, e.g. alpha -- see values.yaml's own
comment on this key).

1. `values-alpha.yaml` sets `pipeline.piiTokenization.enabled: false` -- the
   rendered svc-process-rust Deployment must carry
   `PII_TOKENIZATION_ENABLED=false`, and svc-action-rust must carry
   `PII_DETOKENIZATION_ENABLED=false`.
2. `pipeline.piiTokenization.enabled: true` (the base `values.yaml` default)
   must omit both env vars entirely from either Deployment, so the existing
   PostHog-gated, default-ENABLED behavior is unchanged in every other
   environment.

Zero rendered documents, zero matching Deployments is a hard failure here,
never a silent pass (critical-rules.md Verification Integrity).
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


def _helm_template(extra_args: list[str] | None = None) -> list[dict[str, Any]]:
    cmd = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_ALPHA_VALUES),
    ]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(scope="module")
def alpha_docs() -> list[dict[str, Any]]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    return _helm_template()


@pytest.fixture(scope="module")
def enabled_true_docs() -> list[dict[str, Any]]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    return _helm_template(["--set", "pipeline.piiTokenization.enabled=true"])


def _find_container(
    docs: list[dict[str, Any]], deployment_name_suffix: str, container_name: str
) -> dict[str, Any] | None:
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc.get("metadata", {}).get("name", "")
        if not name.endswith(deployment_name_suffix):
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") == container_name:
                return container
    return None


def _env_value(container: dict[str, Any], name: str) -> str | None:
    for entry in container.get("env", []) or []:
        if entry.get("name") == name:
            return entry.get("value")
    return None


def _env_names(container: dict[str, Any]) -> set[str]:
    return {entry.get("name") for entry in container.get("env", []) or []}


class TestAlphaDisablesPiiTokenizationViaEnvOverride:
    @pytest.mark.parametrize(
        ("deployment_suffix", "container_name", "env_var"),
        [
            ("svc-process-rust", "svc-process-rust", "PII_TOKENIZATION_ENABLED"),
            ("svc-action-rust", "svc-action-rust", "PII_DETOKENIZATION_ENABLED"),
        ],
    )
    def test_env_override_rendered_false(
        self,
        alpha_docs: list[dict[str, Any]],
        deployment_suffix: str,
        container_name: str,
        env_var: str,
    ) -> None:
        container = _find_container(alpha_docs, deployment_suffix, container_name)
        assert container, f"no {container_name} container found in a Deployment ending {deployment_suffix}"
        value = _env_value(container, env_var)
        assert value == "false", (
            f"{env_var} must render as the string \"false\" on {deployment_suffix} when "
            f"pipeline.piiTokenization.enabled is false (values-alpha.yaml) -- got {value!r}"
        )


class TestDefaultEnabledOmitsTheOverrideEntirely:
    @pytest.mark.parametrize(
        ("deployment_suffix", "container_name", "env_var"),
        [
            ("svc-process-rust", "svc-process-rust", "PII_TOKENIZATION_ENABLED"),
            ("svc-action-rust", "svc-action-rust", "PII_DETOKENIZATION_ENABLED"),
        ],
    )
    def test_env_override_absent_when_enabled(
        self,
        enabled_true_docs: list[dict[str, Any]],
        deployment_suffix: str,
        container_name: str,
        env_var: str,
    ) -> None:
        container = _find_container(enabled_true_docs, deployment_suffix, container_name)
        assert container, f"no {container_name} container found in a Deployment ending {deployment_suffix}"
        names = _env_names(container)
        assert env_var not in names, (
            f"{env_var} must be omitted entirely when pipeline.piiTokenization.enabled is "
            f"true, so the existing PostHog-gated default behavior is unchanged -- found it "
            f"in {deployment_suffix}'s env"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
