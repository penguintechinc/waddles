#!/usr/bin/env bash
# Self-test for scripts/lib/checks.sh and scripts/lint.sh: proves the lint /
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
  printf 'x = 1\n' > "$d/app.py"
  # The copied scripts/*.sh make shellcheck applicable; a clean fake keeps this
  # sandbox about flake8 alone (and needs no real shellcheck on the runner).
  printf '#!/bin/sh\nexit 0\n' > "$d/bin/shellcheck"
  chmod +x "$d/bin/shellcheck"
  echo "$d"
}

run_lint() {
  # run_lint <sandbox>; prints exit status, output goes to <sandbox>/out.
  local d="$1" rc=0
  ( cd "$d" && PATH="$d/bin:$PATH" bash scripts/lint.sh ) > "$d/out" 2>&1 || rc=$?
  echo "$rc"
}

d=$(make_sandbox)
printf '#!/bin/sh\necho "Traceback: plugin import failed" >&2\nexit 1\n' > "$d/bin/flake8"
chmod +x "$d/bin/flake8"
expect nonzero "flake8 that crashes (exit 1, no stdout) FAILS lint" "$(run_lint "$d")"

d=$(make_sandbox)
printf '#!/bin/sh\necho "app.py:1:1: E999 seeded"\nexit 0\n' > "$d/bin/flake8"
chmod +x "$d/bin/flake8"
expect nonzero "flake8 reporting a new finding (baseline 0) FAILS lint" "$(run_lint "$d")"

d=$(make_sandbox)
printf '#!/bin/sh\nexit 0\n' > "$d/bin/flake8"
chmod +x "$d/bin/flake8"
rc=$(run_lint "$d")
expect zero "flake8 clean run PASSES lint" "$rc"
if grep -q '1 file(s)' "$d/out" || grep -q 'PASS    flake8' "$d/out"; then
  ok "clean run prints a denominator (file count)"
else
  bad "clean run prints a denominator (file count)"
fi

echo
echo "checks gate integrity: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
