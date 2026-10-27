"""Helm-template assertions for fix/no-empty-kept-secrets.

# regression: lookup-keep preserved EMPTY reader password; multi-app path off (alpha 2026-10-02)

`lookup` always returns empty outside a real `helm install`/`upgrade` against a
live cluster (see templates/auto-provisioned-secrets.yaml's own header comment),
so a `helm template` run can never directly exercise "an existing Secret key
decodes to the empty string" -- the exact branch this fix added. Instead:

1. `TestHelperLogicDirect` renders `templates/debug-helper-probe.yaml`
   (gated behind `debugHelperProbe.enabled`, --set only, never a real
   values-*.yaml default) and asserts the shared `waddlebot.secretKeyNonEmpty`
   / `waddlebot.tlsSecretComplete` helpers (_helpers.tpl) treat an empty
   decoded value as MISSING, not KEEP -- this is the real fix, tested
   directly against the actual template code, not a re-implementation.
2. `TestFailsClosedOutsideAlphaLocal` confirms every lookup-KEEP site still
   fails chart rendering (actionable message) in beta/gamma/production when no
   value exists at all (lookup returns nil in `helm template` either way, so
   this exercises the "missing" path identically whether the cause was never-
   existed or existed-but-empty).
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
_BETA_VALUES = _CHART_DIR / "values-beta.yaml"


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
        return dict(probes[0].get("data") or {})

    def test_secret_key_non_empty_true_case(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["nonEmptySingle"] == "true"

    def test_secret_key_non_empty_rejects_empty_value(self, probe_data: dict[str, Any]) -> None:
        # The actual bug this fix closes: an existing key with an EMPTY decoded
        # value must NOT be treated as present/KEEP.
        assert probe_data["emptyValueSingle"] == ""

    def test_secret_key_non_empty_rejects_missing_key(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["missingKeySingle"] == ""

    def test_secret_key_non_empty_rejects_nil_existing(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["nilExistingSingle"] == ""

    def test_tls_secret_complete_true_case(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["completeTls"] == "true"

    def test_tls_secret_complete_rejects_empty_key(self, probe_data: dict[str, Any]) -> None:
        # The TLS counterpart of the same bug: ca.crt/tls.crt present but
        # tls.key decodes empty must NOT be treated as a complete bundle.
        assert probe_data["emptyKeyTls"] == ""

    def test_tls_secret_complete_rejects_partial_bundle(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["partialTls"] == ""

    def test_tls_secret_complete_rejects_nil_existing(self, probe_data: dict[str, Any]) -> None:
        assert probe_data["nilExistingTls"] == ""


class TestFailsClosedOutsideAlphaLocal:
    """Every lookup-KEEP site still fails rendering in beta/gamma/production with
    no pre-existing Secret and no explicit value -- unaffected by this fix
    (lookup returns nil in `helm template` regardless of cause).
    """

    def test_waddlebot_secrets_fails_without_explicit_values(self) -> None:
        result = _helm_template(_BETA_VALUES)
        assert result.returncode != 0, "beta render should fail closed with no generated secrets supplied"
        assert "outside alpha/local" in result.stderr or "outside alpha/local" in result.stdout

    def test_alpha_still_generates_successfully(self) -> None:
        # Sanity check: the fix must not have broken the alpha generate path.
        result = _helm_template(_ALPHA_VALUES)
        assert result.returncode == 0, f"alpha render should succeed:\n{result.stdout}\n{result.stderr}"
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        assert docs, "helm template produced zero documents -- cannot be a pass"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
