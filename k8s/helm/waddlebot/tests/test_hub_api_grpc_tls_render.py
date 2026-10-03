"""Helm-template assertions for fix/hub-grpc-tls-and-ca-trust (PR #570 review blockers
1/2).

1. hub-api's Deployment must carry non-empty `GRPC_TLS_CERT_PATH`/`GRPC_TLS_KEY_PATH`
   env vars pointing at files actually mounted from the generated
   `<fullname>-hub-api-grpc-tls` Secret -- otherwise `hub_api/grpc_internal/server.py`'s
   internal gRPC listener never starts (blocker 1).
2. svc-process-rust and svc-action-rust must each carry a non-empty `HUB_API_GRPC_CA_FILE`
   env var pointing at a file mounted from the SAME Secret's `ca.crt` -- otherwise
   `core/hub_client::HubClient::connect` has no CA to trust (blocker 2).
3. The generated Secret's `tls.crt` Subject CN/SAN must actually match
   `<fullname>-hub-api-v3` (the hub-api gRPC Service's own DNS name) -- a mismatch here
   means the CA trust in (2) is real but the hostname check still fails every
   connection.

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
_SECRET_NAME_SUFFIX = "-hub-api-grpc-tls"


def _helm_template() -> list[dict[str, Any]]:
    result = subprocess.run(
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


def _volume_mount_paths(container: dict[str, Any]) -> set[str]:
    return {vm.get("mountPath") for vm in container.get("volumeMounts", []) or []}


def _pod_volumes(docs: list[dict[str, Any]], deployment_name_suffix: str) -> list[dict[str, Any]]:
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        if doc.get("metadata", {}).get("name", "").endswith(deployment_name_suffix):
            return doc.get("spec", {}).get("template", {}).get("spec", {}).get("volumes", []) or []
    return []


def _hub_api_grpc_tls_secret(docs: list[dict[str, Any]]) -> dict[str, Any]:
    secrets = [
        doc
        for doc in docs
        if doc.get("kind") == "Secret"
        and doc.get("metadata", {}).get("name", "").endswith(_SECRET_NAME_SUFFIX)
    ]
    assert secrets, f"no *{_SECRET_NAME_SUFFIX} Secret found in rendered manifest -- alpha should generate one"
    return secrets[0]


def _secret_tls_crt_pem(secret: dict[str, Any]) -> str:
    pem = (secret.get("stringData") or {}).get("tls.crt")
    if pem is not None:
        return pem
    raw = (secret.get("data") or {}).get("tls.crt", "")
    assert raw, "hub-api-grpc-tls Secret has no tls.crt in either stringData or data"
    return base64.b64decode(raw).decode()


class TestHubApiGrpcServerGetsTlsMaterial:
    def test_hub_api_container_has_cert_and_key_env(self, rendered_docs: list[dict[str, Any]]) -> None:
        container = _find_container(rendered_docs, "-hub-api-v3", "hub-api")
        assert container, "no hub-api container found in a Deployment ending -hub-api-v3"

        cert_path = _env_value(container, "GRPC_TLS_CERT_PATH")
        key_path = _env_value(container, "GRPC_TLS_KEY_PATH")
        assert cert_path, (
            "GRPC_TLS_CERT_PATH missing/empty -- hub_api/grpc_internal/server.py's "
            "_load_server_credentials will KeyError and the internal gRPC listener "
            "never starts"
        )
        assert key_path, "GRPC_TLS_KEY_PATH missing/empty -- same failure mode as cert_path"

        mount_paths = _volume_mount_paths(container)
        assert any(cert_path.startswith(p) for p in mount_paths), (
            f"GRPC_TLS_CERT_PATH={cert_path!r} is not under any mounted volumeMount "
            f"({mount_paths!r}) -- the env var points at a path the container never has"
        )
        assert any(key_path.startswith(p) for p in mount_paths), (
            f"GRPC_TLS_KEY_PATH={key_path!r} is not under any mounted volumeMount "
            f"({mount_paths!r})"
        )

    def test_hub_api_pod_mounts_the_generated_secret(self, rendered_docs: list[dict[str, Any]]) -> None:
        volumes = _pod_volumes(rendered_docs, "-hub-api-v3")
        assert volumes, "hub-api Deployment has no pod volumes at all"
        matching = [
            v for v in volumes
            if (v.get("secret") or {}).get("secretName", "").endswith(_SECRET_NAME_SUFFIX)
        ]
        assert matching, (
            f"no pod volume sources the *{_SECRET_NAME_SUFFIX} Secret -- "
            f"GRPC_TLS_CERT_PATH/KEY_PATH would point at a non-existent mount"
        )

    def test_generated_cert_identity_matches_hub_api_service_dns_name(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        secret = _hub_api_grpc_tls_secret(rendered_docs)
        pem = _secret_tls_crt_pem(secret)
        cert = x509.load_pem_x509_certificate(pem.encode())
        cns = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cns, "generated hub-api-grpc-tls leaf certificate has no Subject CN"
        cn = str(cns[0].value)
        assert cn.endswith("-hub-api-v3"), (
            f"certificate CN {cn!r} does not match the hub-api gRPC Service DNS name "
            "convention (<fullname>-hub-api-v3) -- HubClient::connect's hostname "
            "verification would reject every real connection"
        )


class TestRustDataPlaneTrustsHubApiCa:
    @pytest.mark.parametrize(
        ("deployment_suffix", "container_name"),
        [
            ("svc-process-rust", "svc-process-rust"),
            ("svc-action-rust", "svc-action-rust"),
        ],
    )
    def test_ca_file_env_and_mount_present(
        self,
        rendered_docs: list[dict[str, Any]],
        deployment_suffix: str,
        container_name: str,
    ) -> None:
        container = _find_container(rendered_docs, deployment_suffix, container_name)
        assert container, f"no {container_name} container found in a Deployment ending {deployment_suffix}"

        ca_file = _env_value(container, "HUB_API_GRPC_CA_FILE")
        assert ca_file, (
            "HUB_API_GRPC_CA_FILE missing/empty -- core/hub_client::HubClient::connect "
            "has no CA to trust and falls back to system/webpki roots, which never "
            "include this chart's internal CA"
        )
        mount_paths = _volume_mount_paths(container)
        assert any(ca_file.startswith(p) for p in mount_paths), (
            f"HUB_API_GRPC_CA_FILE={ca_file!r} is not under any mounted volumeMount "
            f"({mount_paths!r})"
        )

        volumes = _pod_volumes(rendered_docs, deployment_suffix)
        matching = [
            v for v in volumes
            if (v.get("secret") or {}).get("secretName", "").endswith(_SECRET_NAME_SUFFIX)
        ]
        assert matching, (
            f"{deployment_suffix} pod has no volume sourcing *{_SECRET_NAME_SUFFIX} -- "
            "HUB_API_GRPC_CA_FILE would point at a non-existent mount"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
