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
  * dotted KID (e.g. `kid.v1.2`) -> resolved via quoted jsonpath bracket form
  * no SERVICE_JWT_ACTIVE_KID -> falls back to the chart keyId and publishes it
  * Secret absent (alpha) -> PEM key generated, stored under the chart keyId,
    public key derived + published
"""

from __future__ import annotations

import base64
import functools
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
# Secret name = the arg after `secret` (get/annotate) or after `secret generic` (create).
name=""; prev=""
for a in "$@"; do
  if [ "$prev" = "secret" ] || [ "$prev" = "generic" ]; then name="$a"; fi
  prev="$a"
done
case "$args" in
  *"get secret"*"go-template"*) ls "${D}/${name}" 2>/dev/null | tr '\n' ' ' ;;
  # Quoted bracket form {.data['<key>']} -- the supported form (dots in the key are literal).
  *"get secret"*"jsonpath={.data['"*)
    key="${args##*jsonpath=\{.data[\'}"; key="${key%%\'\]*}"
    [ -f "${D}/${name}/${key}" ] && cat "${D}/${name}/${key}"; exit 0 ;;
  # Unquoted {.data.<key>} -- emulate kubectl: a '.' inside the key splits the path -> empty.
  *"get secret"*"jsonpath={.data."*)
    key="${args##*jsonpath=\{.data.}"; key="${key%%\}*}"
    case "$key" in *.*) exit 0 ;; esac
    [ -f "${D}/${name}/${key}" ] && cat "${D}/${name}/${key}"; exit 0 ;;
  *"get secret"*) [ -d "${D}/${name}" ] ;;
  # Persist a created Secret (base64, as the API would) so the derive step can read it back.
  *"create secret generic"*)
    mkdir -p "${D}/${name}"
    for a in "$@"; do
      case "$a" in
        --from-file=*) kv="${a#--from-file=}"; base64 -w0 < "${kv#*=}" > "${D}/${name}/${kv%%=*}" ;;
        --from-literal=*) kv="${a#--from-literal=}"; printf '%s' "${kv#*=}" | base64 -w0 > "${D}/${name}/${kv%%=*}" ;;
      esac
    done ;;
  *"create configmap"*) echo "$args" >> "${D}/cm.log" ;;
  *"apply -f -"*) cat > /dev/null ;;
  *) : ;;
esac
"""


@functools.lru_cache(maxsize=1)
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


def _chart_kid() -> str:
    """The chart's serviceJwt.keyId as rendered into the Job env."""
    return _job_script_and_env()[1]["SERVICE_JWT_KEY_ID"]


def _pub_b64_of(pem: bytes, tmp: Path) -> str:
    """Independently derive the base64 raw Ed25519 public key (last 32 DER bytes) from a PEM."""
    src, der = tmp / "chk.pem", tmp / "chk.der"
    src.write_bytes(pem)
    subprocess.run(["openssl", "pkey", "-in", str(src), "-pubout", "-outform", "DER", "-out", str(der)], check=True, capture_output=True)  # noqa: S603,S607
    return _b64(der.read_bytes()[-32:])


def test_dotted_kid_resolves_via_quoted_jsonpath(tmp_path: Path) -> None:
    """KID containing dots must resolve (bracket-quoted jsonpath), not fail as an empty field."""
    pem, _ = _ed25519_pem_and_seed(tmp_path)
    kid = "kid.v1.2"
    proc, state = _run(tmp_path, {
        "SERVICE_JWT_ACTIVE_KID": _b64(kid.encode()),
        f"SERVICE_JWT_PRIVATE_KEY_{kid}": _b64(pem),
    })
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert f"kid={kid}" in proc.stdout
    assert "FATAL" not in proc.stderr
    assert "create secret generic" not in (state / "calls.log").read_text()


def test_no_active_kid_falls_back_to_chart_keyid(tmp_path: Path) -> None:
    """Secret without SERVICE_JWT_ACTIVE_KID: chart keyId selects the key and is published as kid."""
    pem, _ = _ed25519_pem_and_seed(tmp_path)
    chart_kid = _chart_kid()
    proc, state = _run(tmp_path, {f"SERVICE_JWT_PRIVATE_KEY_{chart_kid}": _b64(pem)})
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert f"falling back to chart keyId '{chart_kid}'" in proc.stdout
    assert f"publishing chart keyId '{chart_kid}' as the kid" in proc.stdout
    assert f"kid={chart_kid}" in proc.stdout
    assert "create secret generic" not in (state / "calls.log").read_text()
    cm = (state / "cm.log").read_text()
    assert f"--from-literal=SERVICE_JWT_ACTIVE_KID={chart_kid}" in cm
    assert f"--from-literal=SERVICE_JWT_PUBLIC_KEY={_pub_b64_of(pem, tmp_path)}" in cm


def test_missing_secret_generates_pem_key_and_publishes_public(tmp_path: Path) -> None:
    """First-time provisioning (alpha): PEM key generated under the chart keyId, public key published."""
    chart_kid = _chart_kid()
    proc, state = _run(tmp_path, {})
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert f"kid={chart_kid}, format=pem" in proc.stdout
    calls = (state / "calls.log").read_text()
    assert "create secret generic" in calls and "helm.sh/resource-policy=keep" in calls
    secret_dir = state / _job_script_and_env()[1]["SERVICE_JWT_SECRET"]
    stored_pem = base64.b64decode((secret_dir / f"SERVICE_JWT_PRIVATE_KEY_{chart_kid}").read_text())
    # PKCS8 PEM (not a raw seed); marker split so secret scanners don't flag the literal.
    assert stored_pem.startswith(b"-----BEGIN") and b"PRIVATE KEY" in stored_pem.splitlines()[0]
    assert base64.b64decode((secret_dir / "SERVICE_JWT_ACTIVE_KID").read_text()).decode() == chart_kid
    cm = (state / "cm.log").read_text()
    assert f"--from-literal=SERVICE_JWT_ACTIVE_KID={chart_kid}" in cm
    assert f"--from-literal=SERVICE_JWT_PUBLIC_KEY={_pub_b64_of(stored_pem, tmp_path)}" in cm
