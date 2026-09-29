"""Unit tests for scripts/ci/check-no-placeholder-credentials.py.

Regression coverage for CodeQL py/clear-text-logging-sensitive-data (alerts
15790/15791): a placeholder-credential finding must never include the
plaintext credential value itself -- only structural metadata (object kind/
name, container, key or env NAME) and the truncated regex match text that
triggered it. Also re-proves the zero-denominator gate and that the original
alpha-incident placeholder render still fails the check.
"""
from __future__ import annotations

import base64
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).parent.parent / "check-no-placeholder-credentials.py"


def _load_module() -> Any:
    """Loads the hyphenated CI script as an importable module for unit tests."""
    spec = importlib.util.spec_from_file_location(
        "check_no_placeholder_credentials", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()

SECRET_RENDER = """
apiVersion: v1
kind: Secret
metadata:
  name: waddlebot-secrets
type: Opaque
data:
  discord_bot_token: {value}
"""


def _b64(value: str) -> str:
    """Base64-encodes a string the way a rendered Secret's `data` field would."""
    return base64.b64encode(value.encode()).decode()


def _run(render: str) -> subprocess.CompletedProcess[str]:
    """Runs the CI script as a subprocess against a given YAML render on stdin."""
    return subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=render,
        capture_output=True,
        text=True,
        check=False,
    )


class TestNoPlaintextLeak:
    """Full-process check: the real credential value never reaches stdout/stderr."""

    def test_finding_never_contains_full_secret_value(self) -> None:
        secret_value = "sk-live-EXAMPLE-abc123def456"
        render = SECRET_RENDER.format(value=_b64(secret_value))
        result = _run(render)
        combined = result.stdout + result.stderr

        assert result.returncode == 1
        assert "FAIL" in combined
        assert "placeholder" in combined.lower()
        assert secret_value not in combined
        assert _b64(secret_value) not in combined


class TestFindingsOmitValue:
    """Unit-level check: findings carry only names/pattern, never the value."""

    def test_check_secret_finding_omits_value(self) -> None:
        findings: list[str] = []
        secret_value = "sk-live-EXAMPLE-abc123def456"
        doc = {
            "kind": "Secret",
            "metadata": {"name": "waddlebot-secrets"},
            "data": {"discord_bot_token": _b64(secret_value)},
        }

        examined = mod.check_secret(doc, findings)

        assert examined == 1
        assert len(findings) == 1
        assert secret_value not in findings[0]
        assert "waddlebot-secrets" in findings[0]
        assert "discord_bot_token" in findings[0]
        assert "EXAMPLE" in findings[0]

    def test_check_workload_env_finding_omits_value(self) -> None:
        findings: list[str] = []
        placeholder_value = "REPLACE_ME_discord_bot_token_dev_only"
        doc = {
            "kind": "Deployment",
            "metadata": {"name": "waddlebot-core"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "core",
                                "env": [
                                    {
                                        "name": "DISCORD_BOT_TOKEN",
                                        "value": placeholder_value,
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
        }

        examined = mod.check_workload_env(doc, findings)

        assert examined == 1
        assert len(findings) == 1
        assert placeholder_value not in findings[0]
        assert "waddlebot-core" in findings[0]
        assert "DISCORD_BOT_TOKEN" in findings[0]
        assert "REPLACE_ME" in findings[0]


class TestZeroDenominatorGate:
    """Zero objects examined must fail hard, never a silent pass."""

    def test_empty_render_fails(self) -> None:
        result = _run("")

        assert result.returncode == 1
        assert "objects examined: 0" in result.stdout
        assert "FAIL" in result.stdout


class TestOriginalIncidentStillCaught:
    """Proof the check still catches the alpha-incident placeholder token."""

    def test_dev_placeholder_still_fails(self) -> None:
        render = SECRET_RENDER.format(
            value=_b64("REPLACE_ME_discord_bot_token_dev_only")
        )
        result = _run(render)

        assert result.returncode == 1
        assert "FAIL" in result.stdout

    def test_clean_render_passes(self) -> None:
        render = SECRET_RENDER.format(value=_b64("a-real-looking-token-value-123"))
        result = _run(render)

        assert result.returncode == 0
        assert "PASS" in result.stdout
