#!/usr/bin/env bash
# Shared plumbing for the lint and security gates.
#
# Exists because the previous targets could not fail: every tool was wrapped in
# `|| true`, and a tool that was not installed was skipped silently. A run with
# nothing installed reported success having examined nothing.
#
# The distinction this file exists to make:
#
#   not applicable  no files of that kind in the repo    -> skip, harmless
#   missing tool    files exist but the tool does not    -> FAILURE, they went unchecked
#   ran and failed  the tool found something             -> FAILURE
#
# Conflating the first two is how a gate goes quietly dead. Bash 3.2 compatible
# per general.md — no associative arrays, no mapfile.

set -euo pipefail

CHECKS_RAN=0
CHECKS_PASSED=0
CHECKS_FAILED=0
CHECKS_SKIPPED=0
CHECKS_MISSING=0
CHECKS_FAILED_NAMES=""
CHECKS_MISSING_NAMES=""

# Paths every check must ignore. `.worktrees/` holds full checkouts of other
# branches; scanning them reports another branch's problems against this one.
# `.claude/worktrees/` is the same thing under a different root (agent-session
# worktrees), and is pruned for the same reason -- without it the denominators
# and finding counts include tens of thousands of other branches' files.
CHECK_PRUNE_ARGS=(
  -not -path "./.git/*"
  -not -path "./.worktrees/*"
  -not -path "./.claude/worktrees/*"
  -not -path "*/node_modules/*"
  -not -path "*/venv/*"
  -not -path "*/.venv/*"
  -not -path "*/vendor/*"
  -not -path "*/__pycache__/*"
)

# discover <find-args...> — print matching paths, pruned. Never fails the script;
# an empty result is a legitimate "not applicable".
discover() {
  find . "$@" "${CHECK_PRUNE_ARGS[@]}" 2>/dev/null || true
}

# count_lines <string> — number of non-empty lines.
count_lines() {
  [ -z "$1" ] && { echo 0; return; }
  printf '%s\n' "$1" | grep -c . || true
}

# run_check <label> <tool> <file-count> <command...>
#
# file_count is the denominator: how many things this check is about to examine.
# Zero means the check does not apply here and is skipped without penalty.
run_check() {
  local label="$1" tool="$2" count="$3"
  shift 3

  if [ "$count" -eq 0 ]; then
    printf '  SKIP    %-16s no applicable files\n' "$label"
    CHECKS_SKIPPED=$((CHECKS_SKIPPED + 1))
    return 0
  fi

  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '  MISSING %-16s %s not installed — %s file(s) went unchecked\n' \
      "$label" "$tool" "$count"
    CHECKS_MISSING=$((CHECKS_MISSING + 1))
    CHECKS_MISSING_NAMES="$CHECKS_MISSING_NAMES $label"
    return 0
  fi

  CHECKS_RAN=$((CHECKS_RAN + 1))
  if "$@"; then
    printf '  PASS    %-16s %s file(s)\n' "$label" "$count"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
  else
    printf '  FAIL    %-16s %s file(s)\n' "$label" "$count"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
    CHECKS_FAILED_NAMES="$CHECKS_FAILED_NAMES $label"
  fi
}

# --- Baselines -------------------------------------------------------------
#
# Turning these gates on revealed roughly 8,700 pre-existing findings, because
# every tool had been wrapped in `|| true`. Two bad options and one good one:
# block all work until the backlog is cleared, switch the gate back off, or
# freeze the backlog at today's number and refuse to let it grow.
#
# The baseline is the third. A new finding fails the build; the counts only
# ever move down. A check whose count has dropped says so, loudly, because a
# baseline nobody lowers is just a higher `|| true`.

CHECKS_BASELINE_FILE="${CHECKS_BASELINE_FILE:-.checks-baseline}"
CHECKS_RATCHET_NOTES=""

# baseline_for <key> — the allowed finding count, or 0 if unlisted.
baseline_for() {
  local key="$1" line
  [ -f "$CHECKS_BASELINE_FILE" ] || { echo 0; return; }
  line=$(grep -E "^${key}=" "$CHECKS_BASELINE_FILE" 2>/dev/null | tail -1 || true)
  [ -z "$line" ] && { echo 0; return; }
  echo "${line#*=}"
}

# run_counted_check <label> <tool> <file-count> <count-command...>
#
# The command prints the number of findings on stdout. Compared against the
# baseline rather than against zero.
run_counted_check() {
  local label="$1" tool="$2" count="$3"
  shift 3

  if [ "$count" -eq 0 ]; then
    printf '  SKIP    %-16s no applicable files\n' "$label"
    CHECKS_SKIPPED=$((CHECKS_SKIPPED + 1))
    return 0
  fi

  if ! command -v "$tool" >/dev/null 2>&1; then
    printf '  MISSING %-16s %s not installed — %s file(s) went unchecked\n' \
      "$label" "$tool" "$count"
    CHECKS_MISSING=$((CHECKS_MISSING + 1))
    CHECKS_MISSING_NAMES="$CHECKS_MISSING_NAMES $label"
    return 0
  fi

  local budget findings
  budget=$(baseline_for "$label")
  findings=$("$@" || true)
  case "$findings" in ''|*[!0-9]*) findings=0 ;; esac

  CHECKS_RAN=$((CHECKS_RAN + 1))
  if [ "$findings" -gt "$budget" ]; then
    printf '  FAIL    %-16s %s finding(s), baseline %s — %s new\n' \
      "$label" "$findings" "$budget" "$((findings - budget))"
    CHECKS_FAILED=$((CHECKS_FAILED + 1))
    CHECKS_FAILED_NAMES="$CHECKS_FAILED_NAMES $label"
  else
    printf '  PASS    %-16s %s finding(s), baseline %s (%s file(s))\n' \
      "$label" "$findings" "$budget" "$count"
    CHECKS_PASSED=$((CHECKS_PASSED + 1))
    if [ "$findings" -lt "$budget" ]; then
      CHECKS_RATCHET_NOTES="$CHECKS_RATCHET_NOTES
  $label: $budget -> $findings"
    fi
  fi
}

# finish_checks <suite-name>
#
# Reports the tally and decides the exit status. Prints counts always: "no
# findings" without a denominator is indistinguishable from "nothing ran".
finish_checks() {
  local suite="$1"
  local applicable=$((CHECKS_RAN + CHECKS_MISSING))

  echo
  echo "$suite: ${CHECKS_RAN} ran, ${CHECKS_PASSED} passed, ${CHECKS_FAILED} failed, ${CHECKS_MISSING} tool(s) missing, ${CHECKS_SKIPPED} not applicable"

  if [ "$applicable" -eq 0 ]; then
    echo "FAIL: no check applied to anything — the suite is misconfigured, not clean" >&2
    return 1
  fi

  if [ "$CHECKS_FAILED" -gt 0 ]; then
    echo "FAIL:$CHECKS_FAILED_NAMES" >&2
    return 1
  fi

  if [ "$CHECKS_MISSING" -gt 0 ]; then
    if [ "${ALLOW_MISSING_TOOLS:-0}" = "1" ]; then
      echo "WARNING: tools missing, files unchecked:$CHECKS_MISSING_NAMES" >&2
      echo "         (ALLOW_MISSING_TOOLS=1 — never set this in CI)" >&2
      return 0
    fi
    echo "FAIL: tools missing, files unchecked:$CHECKS_MISSING_NAMES" >&2
    echo "      install them, or set ALLOW_MISSING_TOOLS=1 for local runs" >&2
    return 1
  fi

  if [ -n "$CHECKS_RATCHET_NOTES" ]; then
    echo
    echo "RATCHET — findings dropped; lower these in $CHECKS_BASELINE_FILE in this PR:$CHECKS_RATCHET_NOTES"
  fi

  return 0
}
