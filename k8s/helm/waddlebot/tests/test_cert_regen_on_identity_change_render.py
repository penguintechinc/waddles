"""Helm-template assertions for fix/cert-regen-on-identity-change.

# regression: kept host-api cert with stale CN after identity pin change (alpha 2026-10-02)

fix/chart-host-api-stage-identity (#539) pinned the host-api-tls identity cert's CN to
`waddlebot.hostApiStageIdentity`'s SPIFFE-style string, and fix/no-empty-kept-secrets
(#540) made the lookup-KEEP branch require a COMPLETE ca.crt/tls.crt/tls.key bundle. Neither
closed the actual gap: alpha's live `waddlebot-host-api-tls` Secret was minted under the OLD
`waddlebot` CN, is still COMPLETE, so the KEEP branch kept it forever -- the bundle-executor
pods, which now require the new pinned identity, rejected its certificate on every
connection. The same class of bug applies to ANY future identity/SAN change on this Secret
(or infrastructure/valkey-tls-secret.yaml's), not just this one incident.

This fix adds `waddlebot.io/cert-identity` / `waddlebot.io/cert-sans-sha256` annotations to
every rendered TLS Secret and only takes the KEEP branch when an existing, COMPLETE Secret's
annotations match what the current render wants; a mismatch (or a Secret that pre-dates
these annotations entirely) regenerates in alpha/local and fails chart rendering (actionable
message) in beta/gamma/production.

`lookup` always returns empty outside a real `helm install`/`upgrade` against a live cluster
(see templates/auto-provisioned-secrets.yaml's own header comment), so a `helm template` run
can never directly exercise the "Secret already exists" branches of host-api-tls-secret.yaml/
valkey-tls-secret.yaml. Following the precedent set by
tests/test_no_empty_kept_secrets_render.py:

1. `TestHelperLogicDirect` renders `templates/debug-helper-probe.yaml` (gated behind
   `debugHelperProbe.enabled`, --set only, never a real values-*.yaml) and asserts the
   shared `waddlebot.tlsSecretIdentityMatches` / `waddlebot.tlsSecretStaleFailsClosed`
   helpers (_helpers.tpl) directly, against the actual template code, not a
   re-implementation.
2. `TestAlphaRenderAnnotationsAndChecksum` renders the real chart with values-alpha.yaml and
   asserts the generated host-api-tls/valkey-tls Secrets carry annotations that match their
   own actually-generated certificate content (decoded CN, for host-api-tls), and that all
   four host-api-tls consumer Deployments (svc-process-rust, svc-action-rust,
   bundle-executor, bundle-executor-action) carry the SAME `checksum/host-api-tls`
   pod-template annotation.

Zero rendered documents, zero matching Deployments/Secrets, or zero probe entries is a hard
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


class TestHelperLogicDirect:
    """Renders the debug probe ConfigMap and asserts each helper branch directly."""

    @staticmethod
    @pytest.fixture(scope="class")
    def probe_data() -> dict[str, Any]:
        result = _helm_template(_ALPHA_VALUES, extra_set=["debugHelperProbe.enabled=true"])
        if result.returncode != 0:
            pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        assert docs, "helm template produced zero documents -- cannot be a pass"
        probes = [
            doc for doc in docs
            if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name", "").endswith("-helper-probe")
        ]
        assert probes, "no *-helper-probe ConfigMap found -- debugHelperProbe.enabled did not render"
        data = dict(probes[0].get("data") or {})
        assert data, "helper-probe ConfigMap has zero data entries -- cannot be a pass"
        return data

    # -- waddlebot.tlsSecretIdentityMatches --

    def test_identity_matches_keeps(self, probe_data: dict[str, Any]) -> None:
        # Complete bundle, annotations match the desired identity/SANs -> KEEP.
        assert probe_data["identityMatchKeeps"] == "true"

    def test_identity_mismatch_cn_rejected(self, probe_data: dict[str, Any]) -> None:
        # The actual incident: a stored waddlebot.io/cert-identity from a stale CN
        # ("waddlebot") must NOT match the newly-desired pinned identity.
        assert probe_data["identityMismatchCN"] == ""

    def test_identity_mismatch_sans_rejected(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["identityMismatchSans"] == ""

    def test_identity_missing_annotations_rejected(self, probe_data: dict[str, Any]) -> None:
        # A Secret minted before this fix existed has no cert-identity/cert-sans-sha256
        # annotations at all -- must NOT be treated as matching.
        assert probe_data["identityMissingAnnotations"] == ""

    def test_identity_incomplete_tls_ignored(self, probe_data: dict[str, Any]) -> None:
        # Matching annotations on an INCOMPLETE bundle still doesn't count as a match --
        # waddlebot.tlsSecretComplete gates first.
        assert probe_data["identityIncompleteTlsIgnored"] == ""

    # -- waddlebot.tlsSecretStaleFailsClosed --

    def test_stale_fails_closed_false_on_match(self, probe_data: dict[str, Any]) -> None:
        # Matching identity/SANs is never "stale" -- the KEEP branch handles it, this
        # helper must stay false regardless of canGenerate.
        assert probe_data["staleFailsClosedOnMatch"] == ""

    def test_stale_fails_closed_true_mismatch_beta(self, probe_data: dict[str, Any]) -> None:
        # Identity mismatch + canGenerate=false (beta/gamma/production) -> FAIL closed.
        assert probe_data["staleFailsClosedMismatchBeta"] == "true"

    def test_stale_regenerates_mismatch_alpha(self, probe_data: dict[str, Any]) -> None:
        # Identity mismatch + canGenerate=true (alpha/local) -> NOT fail-closed, falls
        # through to the chart's generate branch instead (the exact fix for the
        # original incident).
        assert probe_data["staleFailsClosedMismatchAlpha"] == ""

    def test_stale_fails_closed_true_missing_annotations_beta(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["staleFailsClosedMissingAnnotationsBeta"] == "true"

    def test_stale_regenerates_missing_annotations_alpha(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["staleFailsClosedMissingAnnotationsAlpha"] == ""

    def test_stale_fails_closed_false_nil_existing(self, probe_data: dict[str, Any]) -> None:
        # No existing Secret at all is "missing", not "stale" -- the ORIGINAL
        # not-found fail message applies instead, not this one.
        assert probe_data["staleFailsClosedNilExisting"] == ""

    def test_stale_fails_closed_false_incomplete_ignored(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["staleFailsClosedIncompleteIgnored"] == ""


class TestAlphaRenderAnnotationsAndChecksum:
    """Render-level proof against the actual chart templates (not just the helpers)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def rendered_docs() -> list[dict[str, Any]]:
        result = _helm_template(_ALPHA_VALUES)
        if result.returncode != 0:
            pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        assert docs, "helm template produced zero documents -- cannot be a pass"
        return docs

    @staticmethod
    def _secret(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
        secrets = [
            doc for doc in docs
            if doc.get("kind") == "Secret" and doc.get("metadata", {}).get("name", "").endswith(suffix)
        ]
        assert secrets, f"no Secret ending in {suffix!r} found -- cannot be a pass"
        return secrets[0]

    def test_host_api_tls_secret_identity_annotation_matches_cert_cn(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        secret = self._secret(rendered_docs, "-host-api-tls")
        annotations = secret.get("metadata", {}).get("annotations", {})
        identity_annotation = annotations.get("waddlebot.io/cert-identity")
        assert identity_annotation, "waddlebot.io/cert-identity annotation missing from generated host-api-tls Secret"
        tls_crt_b64 = secret.get("stringData", {}).get("tls.crt") or secret.get("data", {}).get("tls.crt")
        assert tls_crt_b64, "generated host-api-tls Secret has no tls.crt"
        pem = tls_crt_b64 if secret.get("stringData") else base64.b64decode(tls_crt_b64).decode()
        cert = x509.load_pem_x509_certificate(pem.encode())
        cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        assert cn_attrs, "generated host-api-tls certificate has no Subject CN"
        assert cn_attrs[0].value == identity_annotation, (
            "waddlebot.io/cert-identity annotation does not match the actual cert CN -- "
            "the stale-identity detector would never fire a correct keep/regen decision"
        )

    def test_host_api_tls_secret_has_sans_sha256_annotation(self, rendered_docs: list[dict[str, Any]]) -> None:
        secret = self._secret(rendered_docs, "-host-api-tls")
        annotations = secret.get("metadata", {}).get("annotations", {})
        sans_hash = annotations.get("waddlebot.io/cert-sans-sha256")
        assert sans_hash, "waddlebot.io/cert-sans-sha256 annotation missing from generated host-api-tls Secret"
        assert len(sans_hash) == 64, f"expected a 64-char sha256 hex digest, got {sans_hash!r}"

    def test_valkey_tls_secret_has_identity_annotations(self, rendered_docs: list[dict[str, Any]]) -> None:
        secret = self._secret(rendered_docs, "-valkey-tls")
        annotations = secret.get("metadata", {}).get("annotations", {})
        assert annotations.get("waddlebot.io/cert-identity"), "waddlebot.io/cert-identity missing from valkey-tls Secret"
        assert annotations.get("waddlebot.io/cert-sans-sha256"), "waddlebot.io/cert-sans-sha256 missing from valkey-tls Secret"

    def test_host_api_tls_checksum_consistent_across_all_four_consumers(
        self, rendered_docs: list[dict[str, Any]]
    ) -> None:
        suffixes = ("-svc-process-rust", "-svc-action-rust", "-bundle-executor", "-bundle-executor-action")
        checksums: dict[str, str] = {}
        for doc in rendered_docs:
            if doc.get("kind") != "Deployment":
                continue
            name = doc.get("metadata", {}).get("name", "")
            matched = next((s for s in suffixes if name.endswith(s)), None)
            if not matched:
                continue
            annotations = doc.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}) or {}
            checksums[matched] = annotations.get("checksum/host-api-tls")

        assert len(checksums) == 4, f"expected all 4 host-api-tls consumer Deployments, found {sorted(checksums)}"
        for suffix, value in checksums.items():
            assert value, f"{suffix} is missing its checksum/host-api-tls pod-template annotation"
        distinct = set(checksums.values())
        assert len(distinct) == 1, (
            "checksum/host-api-tls differs across consumer Deployments -- they would not "
            f"all roll together on an identity/SAN change: {checksums}"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
