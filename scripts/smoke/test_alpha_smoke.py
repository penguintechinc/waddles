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


def _load_checker():
    """Import alpha_checks.py as a module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ac", Path(__file__).with_name("alpha_checks.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_checker_constants_and_no_hardcoded_min():
    """Vendor field names are top-level constants; catalog uses a relative assertion."""
    src = Path(__file__).with_name("alpha_checks.py").read_text()
    for const in ("VENDORS_PATH", "VENDOR_KEY_FIELD", "VENDOR_NAME_FIELD", "VENDOR_COUNT_FIELD"):
        assert f"\n{const} = " in src
    assert "MIN_CATALOG" not in src


def test_vendor_check_sum_must_match_catalog(monkeypatch, capsys):
    """Vendor count sum must equal catalog total; mismatch fails."""
    import json

    ac = _load_checker()

    def fake(total):
        def http(method, url, body=None, token=None):
            if url.endswith(ac.VENDORS_PATH):
                vendor = {ac.VENDOR_KEY_FIELD: ac.FIRST_PARTY_VENDOR,
                          ac.VENDOR_NAME_FIELD: "x", ac.VENDOR_COUNT_FIELD: 5}
                return 200, json.dumps({ac.VENDORS_LIST_KEY: [vendor]})
            return 200, json.dumps({"pagination": {"total": total}, "modules": [1]})

        return http

    for total, expect in ((5, "PASS"), (6, "FAIL")):
        monkeypatch.setattr(ac, "http", fake(total))
        ac.check_vendors("t")
        assert f"marketplace-vendors|{expect}" in capsys.readouterr().out
