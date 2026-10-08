"""Helm-template assertions for fix/valkey-cert-rollout-and-migrate-wait.

# regression: valkey cert regenerated but server pod not rolled; clients BadSignature (alpha 2026-10-02)

fix/cert-regen-on-identity-change (#541) added `waddlebot.io/cert-identity`/
`waddlebot.io/cert-sans-sha256` annotations to the `{{ fullname }}-valkey-tls` Secret and a
stale-identity detector (`waddlebot.tlsSecretIdentityMatches`), so a changed identity/SAN set
correctly regenerates (alpha/local) or fails closed (beta/gamma/production) that Secret. It
never closed the other half of the gap: no template actually ROLLED a pod on that change. The
Valkey server Deployment (templates/infrastructure/redis.yaml) kept its existing pod -- still
serving the OLD leaf cert -- while every client Deployment/Job that re-rendered picked up the
regenerated CA, so the server and its clients disagreed about which CA to trust and every
connection failed with `invalid peer certificate: BadSignature`.

This fix adds a shared `checksum/valkey-tls` pod-template annotation (`waddlebot.
valkeyTlsPodChecksum`, identity+SANs input only, never key material -- mirrors
`waddlebot.hostApiTlsPodChecksum`'s design) to the server Deployment AND every
Deployment/Job that mounts the Valkey CA (hub-api, svc-ingest-rust, svc-process-rust,
svc-action-rust, core-bundle-seeder), so a `helm upgrade` always rolls all of them together
on any identity/SAN change, regardless of whether the underlying Secret object was KEPT (same
name, no new resourceVersion) or regenerated.

Zero matching workloads, or a zero-length checksum set, is a hard failure here, never a
silent pass (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"

# Every Deployment/Job that mounts the Valkey CA via waddlebot.valkeyTlsCaVolumeMount, plus
# the Valkey server itself (mounts the full Secret via waddlebot.valkeyTlsServerVolumeMount).
# Server resource name is the bare "redis" (templates/infrastructure/redis.yaml), never
# fullname-prefixed -- every other entry here IS fullname-prefixed.
_SERVER_NAME = "redis"
_CLIENT_SUFFIXES = (
    "-hub-api-v3",
    "-svc-ingest-rust",
    "-svc-process-rust",
    "-svc-action-rust",
    "-core-bundle-seeder",
)


def _shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


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
    if not _shutil_which("helm"):
        pytest.skip("helm CLI not available in this environment")


@pytest.fixture(scope="module")
def rendered_docs() -> list[dict[str, Any]]:
    result = _helm_template(_ALPHA_VALUES)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _workload_checksums(docs: list[dict[str, Any]]) -> dict[str, str | None]:
    checksums: dict[str, str | None] = {}
    for doc in docs:
        if doc.get("kind") not in ("Deployment", "Job"):
            continue
        name = doc.get("metadata", {}).get("name", "")
        if name == _SERVER_NAME:
            key = _SERVER_NAME
        else:
            matched = next((s for s in _CLIENT_SUFFIXES if name.endswith(s)), None)
            if not matched:
                continue
            key = matched
        annotations = doc.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}) or {}
        checksums[key] = annotations.get("checksum/valkey-tls")
    return checksums


def test_all_valkey_ca_consumers_and_server_present(rendered_docs: list[dict[str, Any]]) -> None:
    checksums = _workload_checksums(rendered_docs)
    expected = {_SERVER_NAME, *_CLIENT_SUFFIXES}
    assert checksums, "no Valkey server or CA-mounting client workloads found -- cannot be a pass"
    missing = expected - set(checksums)
    assert not missing, f"expected workloads not found in render: {sorted(missing)}"


def test_every_consumer_carries_the_checksum_annotation(rendered_docs: list[dict[str, Any]]) -> None:
    checksums = _workload_checksums(rendered_docs)
    assert len(checksums) == 6, f"expected the server + 5 client workloads, found {sorted(checksums)}"
    for key, value in checksums.items():
        assert value, f"{key} is missing its checksum/valkey-tls pod-template annotation"


def test_server_and_every_client_share_the_same_checksum(rendered_docs: list[dict[str, Any]]) -> None:
    checksums = _workload_checksums(rendered_docs)
    distinct = set(checksums.values())
    assert len(distinct) == 1, (
        "checksum/valkey-tls differs between the Valkey server and its clients -- they "
        f"would not all roll together on an identity/SAN change: {checksums}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
