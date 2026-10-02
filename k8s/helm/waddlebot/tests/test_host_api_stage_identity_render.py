"""Helm-template assertions for fix/chart-host-api-stage-identity.

# regression: executor required HOST_API_STAGE_IDENTITY, chart never set it (alpha 2026-10-02)

The alpha bundle-executor crash-looped with `Error: Config("HOST_API_STAGE_IDENTITY is
required ...")` (core/bundle_executor/src/config.rs's `validate_host_api_tls`) because the
chart never rendered that env var at all. This asserts, against the real rendered manifest
text -- not a hand-read of the templates -- that:

1. Both `bundle-executor` and `bundle-executor-action` Deployments carry a non-empty
   `HOST_API_STAGE_IDENTITY` env var with the SAME value (the shared host-api-tls identity
   cert serves both executors, see `_helpers.tpl`'s `waddlebot.hostApiTlsClientEnv`).
2. That value is byte-for-byte the Subject CN actually baked into the alpha-generated
   `<fullname>-host-api-tls` Secret's `tls.crt` -- i.e. `core/bundle_executor/src/tls.rs`'s
   `PinnedIdentityVerifier` (CN-fallback branch) will actually accept the stage's real
   certificate, not merely that some string got rendered somewhere.

Zero rendered documents, zero matching Deployments, or zero matching Secrets is a hard
failure here, never a silent pass (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"


def _helm_template() -> list[dict[str, Any]]:
    """Render the chart with values-alpha.yaml; returns every parsed document."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        [
            "helm", "template", "waddlebot", str(_CHART_DIR),
            "--kube-version", "1.30.0",
            "--values", str(_ALPHA_VALUES),
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


@pytest.fixture(scope="module")
def rendered_docs() -> list[dict[str, Any]]:
    if not _shutil_which("helm"):
        pytest.skip("helm CLI not available in this environment")
    return _helm_template()


def _shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def _find_container_env(
    docs: list[dict[str, Any]], deployment_name_suffix: str, container_name: str
) -> list[dict[str, Any]]:
    """Returns the named container's `env[]` list from the Deployment whose name ends with the suffix."""
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        name = doc.get("metadata", {}).get("name", "")
        if not name.endswith(deployment_name_suffix):
            continue
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for container in containers:
            if container.get("name") == container_name:
                return list(container.get("env", []) or [])
    return []


def _env_value(env_list: list[dict[str, Any]], name: str) -> str | None:
    for entry in env_list:
        if entry.get("name") == name:
            return entry.get("value")
    return None


def _host_api_tls_secret_cn(docs: list[dict[str, Any]]) -> str:
    """Decodes the generated `<fullname>-host-api-tls` Secret's `tls.crt` and returns its
    Subject Common Name -- the exact identity `core/bundle_executor/src/tls.rs`'s
    PinnedIdentityVerifier will see on the wire, independent of anything the chart's own
    templates/Helm functions *claim* the CN is.
    """
    secrets = [
        doc
        for doc in docs
        if doc.get("kind") == "Secret"
        and doc.get("metadata", {}).get("name", "").endswith("-host-api-tls")
    ]
    assert secrets, "no *-host-api-tls Secret found in rendered manifest -- alpha should generate one"
    secret = secrets[0]

    tls_crt_pem = (secret.get("stringData") or {}).get("tls.crt")
    if tls_crt_pem is None:
        raw = (secret.get("data") or {}).get("tls.crt", "")
        assert raw, "host-api-tls Secret has no tls.crt in either stringData or data"
        tls_crt_pem = base64.b64decode(raw).decode()

    cert = x509.load_pem_x509_certificate(tls_crt_pem.encode())
    cns = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    assert cns, "generated host-api-tls leaf certificate has no Subject CN"
    return str(cns[0].value)


class TestExecutorsGetHostApiStageIdentity:
    @pytest.mark.parametrize(
        ("deployment_suffix", "container_name"),
        [
            # Both Deployments name their container "bundle-executor" (see
            # templates/bundle-executor.yaml / bundle-executor-action.yaml) -- only the
            # Deployment name itself distinguishes the process-stage vs. action-stage pod.
            ("bundle-executor", "bundle-executor"),
            ("bundle-executor-action", "bundle-executor"),
        ],
    )
    def test_stage_identity_env_matches_generated_cert_cn(
        self,
        rendered_docs: list[dict[str, Any]],
        deployment_suffix: str,
        container_name: str,
    ) -> None:
        env = _find_container_env(rendered_docs, deployment_suffix, container_name)
        assert env, f"no {container_name} container found in a Deployment ending {deployment_suffix}"

        identity = _env_value(env, "HOST_API_STAGE_IDENTITY")
        assert identity, (
            "HOST_API_STAGE_IDENTITY missing/empty -- core/bundle_executor/src/config.rs's "
            "validate_host_api_tls will crashloop the pod on this exact Config error"
        )

        expected_cn = _host_api_tls_secret_cn(rendered_docs)
        assert identity == expected_cn, (
            f"HOST_API_STAGE_IDENTITY ({identity!r}) does not match the generated "
            f"host-api-tls certificate's Subject CN ({expected_cn!r}) -- "
            "PinnedIdentityVerifier would reject every real connection"
        )

    def test_both_executors_share_the_same_identity(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        process_env = _find_container_env(rendered_docs, "bundle-executor", "bundle-executor")
        action_env = _find_container_env(
            rendered_docs, "bundle-executor-action", "bundle-executor"
        )
        assert process_env and action_env, "both executor Deployments must render"
        assert _env_value(process_env, "HOST_API_STAGE_IDENTITY") == _env_value(
            action_env, "HOST_API_STAGE_IDENTITY"
        ), "both executors dial the same shared host-api-tls identity cert -- their expected identity must match"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
