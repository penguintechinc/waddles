"""Regression for fix/auto-provision-keys-faildloud-jwt-kid.

Bug: the `auto-provision-keys` hook Job built the service-JWT private-key field
name from the chart's `serviceJwt.keyId`, but an EXISTING `service-jwt-signing-key`
Secret stores the key under its own `SERVICE_JWT_ACTIVE_KID`. The jsonpath resolved
empty, `openssl ... >/dev/null 2>&1` swallowed the error and `set -e` exited
non-zero with an empty log (BackoffLimitExceeded, no diagnosis).

Contract proven here by rendering the real Job script and executing it against a
stub `kubectl` + the real `openssl`:
  * existing Secret with a KID different from the chart keyId -> succeeds, key kept
  * Secret whose key field is missing -> exit 1 with a message naming the key
"""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"

_STUB_KUBECTL = r"""#!/usr/bin/env bash
set -u
D="${STUB_DIR}"
args="$*"
echo "kubectl $args" >> "${D}/calls.log"
name=""; prev=""
for a in "$@"; do
  if [ "$prev" = "secret" ]; then name="$a"; fi
  prev="$a"
done
case "$args" in
  *"get secret"*"go-template"*) ls "${D}/${name}" 2>/dev/null | tr '\n' ' ' ;;
  *"get secret"*"jsonpath={.data."*)
    key="${args##*jsonpath={.data.}"; key="${key%%\}*}"
    [ -f "${D}/${name}/${key}" ] && cat "${D}/${name}/${key}"; exit 0 ;;
  *"get secret"*) [ -d "${D}/${name}" ] ;;
  *"apply -f -"*) cat > /dev/null ;;
  *) : ;;
esac
"""


def _job_script_and_env() -> tuple[str, dict[str, str]]:
    """Render the chart and return the auto-provision-keys Job's script + env."""
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_ALPHA_VALUES), "--set", "global.imageTag=test"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("auto-provision-keys"):
            c: dict[str, Any] = doc["spec"]["template"]["spec"]["containers"][0]
            return c["args"][0], {e["name"]: e["value"] for e in c["env"]}
    raise AssertionError("auto-provision-keys Job not rendered")


@pytest.fixture(scope="module", autouse=True)
def _require_tools() -> None:
    for tool in ("helm", "openssl", "bash"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not available")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _ed25519_pem_and_seed(tmp: Path) -> tuple[bytes, bytes]:
    """Generate an Ed25519 key; return (PEM bytes, raw 32-byte seed)."""
    pem = tmp / "k.pem"
    der = tmp / "k.der"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(pem)], check=True, capture_output=True)  # noqa: S603,S607
    subprocess.run(["openssl", "pkey", "-in", str(pem), "-outform", "DER", "-out", str(der)], check=True, capture_output=True)  # noqa: S603,S607
    return pem.read_bytes(), der.read_bytes()[-32:]


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def _run(tmp_path: Path, jwt_fields: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Execute the rendered Job script against a stub kubectl holding the given JWT Secret."""
    script, env = _job_script_and_env()
    stub_dir = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(_STUB_KUBECTL)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)

    _, seed = _ed25519_pem_and_seed(tmp_path)
    bs = stub_dir / env["BUNDLE_SIGNING_SECRET"]
    _write(bs / "BUNDLE_SIGNING_PRIVATE_KEY", _b64(_b64(seed).encode()))
    _write(bs / "BUNDLE_SIGNING_KEY_ID", _b64(env["BUNDLE_SIGNING_KEY_ID"].encode()))
    for field, value in jwt_fields.items():
        _write(stub_dir / env["SERVICE_JWT_SECRET"] / field, value)

    run_env = {**os.environ, **env, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_DIR": str(stub_dir)}
    run_env.pop("OPENSSL_CONF", None)
    proc = subprocess.run(["bash", "-c", script], env=run_env, capture_output=True, text=True, check=False)  # noqa: S603,S607
    return proc, stub_dir


def test_existing_secret_with_different_kid_is_kept_and_derived(tmp_path: Path) -> None:
    """Existing Secret under an operator KID: key kept, public key derived, nothing regenerated."""
    pem, _ = _ed25519_pem_and_seed(tmp_path)
    proc, state = _run(tmp_path, {
        "SERVICE_JWT_ACTIVE_KID": _b64(b"operator-kid-9"),
        "SERVICE_JWT_PRIVATE_KEY_operator-kid-9": _b64(pem),
    })
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "kid=operator-kid-9" in proc.stdout
    assert "auto-provision-keys: done" in proc.stdout
    assert "create secret generic" not in (state / "calls.log").read_text()


def test_missing_key_field_fails_loud_naming_expected_key(tmp_path: Path) -> None:
    """Active KID points at an absent field: exit 1, stderr names expected + present key names."""
    pem, _ = _ed25519_pem_and_seed(tmp_path)
    proc, _ = _run(tmp_path, {
        "SERVICE_JWT_ACTIVE_KID": _b64(b"operator-kid-9"),
        "SERVICE_JWT_PRIVATE_KEY_some-other-name": _b64(pem),
    })
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "FATAL" in proc.stderr
    assert "SERVICE_JWT_PRIVATE_KEY_operator-kid-9" in proc.stderr
    assert "SERVICE_JWT_PRIVATE_KEY_some-other-name" in proc.stderr
