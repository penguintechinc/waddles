#!/usr/bin/env bash
# Self-test for scripts/lib/checks.sh, scripts/lint.sh and the pytest coverage
# threshold (.coveragerc fail_under): proves the lint / coverage /
# security gates can actually FAIL (critical-rules.md Verification Integrity).
#
# The defect this guards against: every counter used to be
#   `tool 2>/dev/null | grep -c . || true`
# so a scanner that crashed on startup printed nothing, counted as zero
# findings, and the gate reported PASS having examined nothing. Each case below
# seeds one failure shape and asserts the gate turns red -- and one clean case
# asserts it still passes, so "always fails" is not mistaken for "works".
#
# Runs against a throwaway sandbox with fake scanners on PATH; needs no real
# flake8/shellcheck and touches nothing in the repo. Bash 3.2 compatible.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
pass=0
fail=0
sandboxes=""
cleanup() { for d in $sandboxes; do rm -rf "$d"; done; }
trap cleanup EXIT

ok()  { pass=$((pass + 1)); echo "  ok   $1"; }
bad() { fail=$((fail + 1)); echo "  FAIL $1" >&2; }

# expect_rc <want-nonzero|zero> <desc> <rc>
expect() {
  local want="$1" desc="$2" rc="$3"
  if [ "$want" = "zero" ] && [ "$rc" -eq 0 ]; then ok "$desc"
  elif [ "$want" = "nonzero" ] && [ "$rc" -ne 0 ]; then ok "$desc (rc=$rc)"
  else bad "$desc (rc=$rc, wanted $want)"; fi
}

echo "== tool_capture =="
# shellcheck source=scripts/lib/checks.sh
( . "$REPO_ROOT/scripts/lib/checks.sh"
  out=$(tool_capture "0 1" sh -c 'echo finding; exit 1')
  [ "$out" = "finding" ] ) && ok "findings exit (1) is accepted and stdout returned" \
  || bad "findings exit (1) is accepted and stdout returned"

rc=0
( . "$REPO_ROOT/scripts/lib/checks.sh"
  tool_capture "0 1" sh -c 'echo boom >&2; exit 2' >/dev/null 2>&1 ) || rc=$?
expect nonzero "unexpected exit (2) is a crash, not zero findings" "$rc"

echo "== run_counted_check =="
run_case() {
  # run_case <baseline> <counter-body>; prints the finish_checks exit status.
  local baseline="$1" body="$2" d rc=0
  d=$(mktemp -d); sandboxes="$sandboxes $d"
  printf 'demo=%s\n' "$baseline" > "$d/baseline"
  (
    CHECKS_BASELINE_FILE="$d/baseline"
    . "$REPO_ROOT/scripts/lib/checks.sh"
    counter() { eval "$body"; }
    run_counted_check demo sh 1 counter
    finish_checks demo
  ) >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

expect nonzero "crashing counter fails the gate"            "$(run_case 100 'return 1')"
expect nonzero "non-numeric counter output fails the gate"  "$(run_case 100 'echo oops')"
expect nonzero "empty counter output fails the gate"        "$(run_case 100 'echo')"
expect nonzero "findings above baseline fail the gate"      "$(run_case 5 'echo 6')"
expect zero    "findings at baseline pass"                  "$(run_case 5 'echo 5')"
expect zero    "findings below baseline pass"               "$(run_case 5 'echo 2')"

echo "== scripts/lint.sh end-to-end (fake flake8) =="
make_sandbox() {
  local d
  d=$(mktemp -d); sandboxes="$sandboxes $d"
  mkdir -p "$d/scripts/lib" "$d/bin"
  cp "$REPO_ROOT/scripts/lint.sh" "$d/scripts/lint.sh"
  cp "$REPO_ROOT/scripts/lib/checks.sh" "$d/scripts/lib/checks.sh"
  # lint.sh verifies the flake8 version against this pin before counting; without it every
  # fake dies at the version check, so crash/finding cases would "pass" for the wrong reason.
  cp "$REPO_ROOT/requirements-lint.txt" "$d/requirements-lint.txt"
  printf 'x = 1\n' > "$d/app.py"
  # The copied scripts/*.sh make shellcheck applicable; a clean fake keeps this
  # sandbox about flake8 alone (and needs no real shellcheck on the runner).
  printf '#!/bin/sh\nexit 0\n' > "$d/bin/shellcheck"
  chmod +x "$d/bin/shellcheck"
  echo "$d"
}

# fake_flake8 <sandbox> <body>: answers --version with the pinned version so the run reaches
# the code path under test instead of dying on the version check.
fake_flake8() {
  local pin
  pin=$(sed -n 's/^flake8==\([0-9.]*\).*/\1/p' "$REPO_ROOT/requirements-lint.txt")
  printf '#!/bin/sh\n[ "$1" = "--version" ] && { echo "%s (mccabe: fake)"; exit 0; }\n%s\n' "$pin" "$2" > "$1/bin/flake8"
  chmod +x "$1/bin/flake8"
}

run_lint() {
  # run_lint <sandbox>; prints exit status, output goes to <sandbox>/out.
  local d="$1" rc=0
  ( cd "$d" && PATH="$d/bin:$PATH" bash scripts/lint.sh ) > "$d/out" 2>&1 || rc=$?
  echo "$rc"
}

d=$(make_sandbox)
fake_flake8 "$d" 'echo "Traceback: plugin import failed" >&2; exit 1'
expect nonzero "flake8 that crashes (exit 1, no stdout) FAILS lint" "$(run_lint "$d")"

d=$(make_sandbox)
fake_flake8 "$d" 'echo "app.py:1:1: E999 seeded"; exit 0'
expect nonzero "flake8 reporting a new finding (baseline 0) FAILS lint" "$(run_lint "$d")"

d=$(make_sandbox)
fake_flake8 "$d" 'exit 0'
rc=$(run_lint "$d")
expect zero "flake8 clean run PASSES lint" "$rc"
if grep -q '1 file(s)' "$d/out" || grep -q 'PASS    flake8' "$d/out"; then
  ok "clean run prints a denominator (file count)"
else
  bad "clean run prints a denominator (file count)"
fi

echo "== coverage gate (.coveragerc fail_under=90 + pytest addopts) =="
# Uses the REAL .coveragerc the suites use (hub_api's), so a typo in fail_under
# there is caught here. pytest + pytest-cov are required: a missing dependency
# is a failure of this test, never a silent skip.
PYTEST_PY="${PYTHON_BIN:-python3}"
if ! "$PYTEST_PY" -c 'import pytest, pytest_cov' 2>/dev/null; then
  bad "pytest + pytest-cov importable by $PYTEST_PY (pip install -r scripts/ci/tests/requirements-gate-test.txt)"
else
  cov_sandbox() {
    # cov_sandbox <extra-test-lines>; prints the sandbox path. mod.py has two
    # functions; the test always calls f() and calls g() only when asked.
    local d
    d=$(mktemp -d); sandboxes="$sandboxes $d"
    mkdir -p "$d/tests"
    printf 'def f():\n    return 1\n\n\ndef g():\n    return 2\n' > "$d/mod.py"
    cp "$REPO_ROOT/hub_api/.coveragerc" "$d/.coveragerc"
    printf '[pytest]\ntestpaths = tests\naddopts = --cov --cov-config=.coveragerc --cov-report=term:skip-covered\n' > "$d/pytest.ini"
    printf 'import mod\n\n\ndef test_it():\n    assert mod.f() == 1\n%b' "$1" > "$d/tests/test_mod.py"
    echo "$d"
  }
  run_cov() {
    local rc=0
    ( cd "$1" && "$PYTEST_PY" -m pytest -q -p no:cacheprovider ) > "$1/out" 2>&1 || rc=$?
    echo "$rc"
  }

  d=$(cov_sandbox '')
  rc=$(run_cov "$d")
  expect nonzero "suite under 90% line coverage FAILS the pytest run" "$rc"
  if grep -q 'Required test coverage of 90' "$d/out"; then
    ok "failure is the coverage threshold, not an unrelated error"
  else
    bad "failure is the coverage threshold, not an unrelated error"
  fi

  d=$(cov_sandbox '    assert mod.g() == 2\n')
  expect zero "suite at 100% line coverage PASSES" "$(run_cov "$d")"
fi

echo
echo "checks gate integrity: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
