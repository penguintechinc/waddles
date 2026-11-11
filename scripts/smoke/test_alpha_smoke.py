"""Guard-logic tests for scripts/smoke/alpha-smoke.sh (no cluster needed)."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).with_name("alpha-smoke.sh")


def run_bash(snippet: str) -> subprocess.CompletedProcess:
    """Source the script (main is guarded) and run a snippet."""
    return subprocess.run(
        ["bash", "-c", f"source {SCRIPT}; {snippet}"], capture_output=True, text=True, check=False
    )


def test_zero_checks_is_failure():
    """Zero executed checks must fail, never pass."""
    res = run_bash("summarize")
    assert res.returncode != 0
    assert "checks=0 passed=0 failed=0" in res.stdout


def test_any_failure_exits_nonzero_and_counts():
    """One failing check fails the run with accurate counts."""
    res = run_bash("record a PASS ok >/dev/null; record b FAIL bad >/dev/null; record c SKIP s >/dev/null; summarize")
    assert res.returncode != 0
    assert "checks=3 passed=1 failed=1 skipped=1" in res.stdout


def test_all_pass_exits_zero():
    """All-pass (with a skip) exits zero."""
    res = run_bash("record a PASS ok >/dev/null; record c SKIP s >/dev/null; summarize")
    assert res.returncode == 0


def test_jwt_redacted():
    """JWT-shaped strings never reach output."""
    res = run_bash("record login PASS 'tok eyJabc.def.ghi Bearer xyz'")
    assert "eyJabc" not in res.stdout and "xyz" not in res.stdout


def test_unknown_arg_rejected():
    """Unknown args exit 2."""
    res = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, check=False)
    assert res.returncode == 2
