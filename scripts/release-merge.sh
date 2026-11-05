#!/usr/bin/env bash
# release-merge.sh - verify a PR is green on its current head, then merge it into a release/* branch.
# Usage: scripts/release-merge.sh <pr-number> [--expect-head <sha-prefix>] [--dry-run]
# Refuses non-release bases, non-OPEN PRs, head drift, conflicts, pending/failed checks, and zero checks.
set -euo pipefail

IGNORE_CONTEXT="submit-queue"   # mirrors ci-gate.yml IGNORE_CONTEXT (external advisory status)
REQUIRED_CONTEXTS="${RELEASE_MERGE_REQUIRED:-ci-gate}"   # space-separated; must be present + SUCCESS on head
SNAPSHOT_DELAY="${RELEASE_MERGE_SNAPSHOT_DELAY:-5}"

die() { echo "REFUSE: $*" >&2; exit 1; }

PR=""; EXPECT=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-head) [ $# -ge 2 ] || die "--expect-head needs a value"; EXPECT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -*) die "unknown flag $1" ;;
    *) [ -z "$PR" ] || die "unexpected arg $1"; PR="$1"; shift ;;
  esac
done
case "$PR" in ''|*[!0-9]*) die "usage: release-merge.sh <pr-number> [--expect-head <sha>] [--dry-run]" ;; esac

JSON="$(gh pr view "$PR" --json baseRefName,headRefOid,mergeable,mergeStateStatus,state,statusCheckRollup)"
BASE="$(jq -r '.baseRefName' <<<"$JSON")"
HEAD="$(jq -r '.headRefOid' <<<"$JSON")"
STATE="$(jq -r '.state' <<<"$JSON")"
MERGEABLE="$(jq -r '.mergeable' <<<"$JSON")"
MSTATE="$(jq -r '.mergeStateStatus' <<<"$JSON")"

# 1. base/state/head guards
case "$BASE" in release/*) ;; *) die "base '$BASE' is not release/* (never main/other)" ;; esac
[ "$STATE" = "OPEN" ] || die "state is $STATE, not OPEN"
echo "1 base=$BASE state=$STATE head=${HEAD:0:12}"
if [ -n "$EXPECT" ]; then
  case "$HEAD" in
    "$EXPECT"*) ;;
    *) echo "new head commits (last 5):" >&2
       gh pr view "$PR" --json commits --jq '.commits[-5:][] | "  \(.oid[0:12]) \(.messageHeadline)"' >&2
       die "head $HEAD does not start with expected $EXPECT" ;;
  esac
fi

# 2. checks on current head. Normalize CheckRun + StatusContext into name|bucket.
# eval_checks <pr-json>: sets EXAMINED OK SKIPPED BAD IGN MISSING SIG (SIG = sorted name|bucket set).
eval_checks() {
  local norm name bucket req seen bad_req
  norm="$(jq -r '.statusCheckRollup[]? | [(.name // .context // "?"),
    (if .__typename == "StatusContext" then
       (if .state == "SUCCESS" then "SUCCESS" elif (.state == "PENDING" or .state == "EXPECTED") then "PENDING" else "FAILURE" end)
     elif .status != "COMPLETED" then "PENDING"
     else (.conclusion // "PENDING") end)] | join("|")' <<<"$1")"
  EXAMINED=0; OK=0; SKIPPED=0; BAD=""; IGN=""; MISSING=""
  SIG="$(printf '%s\n' "$norm" | sort)"
  while IFS='|' read -r name bucket; do
    [ -n "$name" ] || continue
    if [ "$name" = "$IGNORE_CONTEXT" ]; then IGN="$IGN $name=$bucket"; continue; fi
    case "$bucket" in
      SKIPPED) SKIPPED=$((SKIPPED + 1)); continue ;;  # skipped never counts toward the denominator
    esac
    EXAMINED=$((EXAMINED + 1))
    case "$bucket" in
      SUCCESS|NEUTRAL) OK=$((OK + 1)) ;;
      *) BAD="$BAD $name($bucket)" ;;
    esac
  done <<<"$norm"
  # required contexts must be present on this head and every instance SUCCESS
  for req in $REQUIRED_CONTEXTS; do
    seen=0; bad_req=0
    while IFS='|' read -r name bucket; do
      [ "$name" = "$req" ] || continue
      seen=1
      [ "$bucket" = "SUCCESS" ] || bad_req=1
    done <<<"$norm"
    if [ "$seen" -eq 0 ]; then MISSING="$MISSING $req(absent)"
    elif [ "$bad_req" -eq 1 ]; then MISSING="$MISSING $req(not-SUCCESS)"; fi
  done
}

assert_green() {
  echo "2 checks examined=$EXAMINED ok=$OK skipped=$SKIPPED ignored:${IGN:- none}"
  [ "$EXAMINED" -gt 0 ] || die "zero checks examined (skipped=$SKIPPED) - cannot verify green"
  [ "$OK" -gt 0 ] || die "zero successful checks - cannot verify green"
  [ -z "$MISSING" ] || die "required check(s) not green on head:$MISSING"
  [ -z "$BAD" ] || die "failing/pending checks:$BAD"
}

eval_checks "$JSON"
assert_green

# 3. conflicts
[ "$MERGEABLE" != "CONFLICTING" ] || die "CONFLICTING (mergeStateStatus=$MSTATE) - resolve manually"
echo "3 mergeable=$MERGEABLE mergeStateStatus=$MSTATE"

# 3b. second snapshot a few seconds later: head and check set must be identical
FIRST_SIG="$SIG"
sleep "$SNAPSHOT_DELAY"
JSON2="$(gh pr view "$PR" --json baseRefName,headRefOid,mergeable,mergeStateStatus,state,statusCheckRollup)"
HEAD2="$(jq -r '.headRefOid' <<<"$JSON2")"
[ "$HEAD2" = "$HEAD" ] || die "head moved during verification: $HEAD -> $HEAD2"
[ "$(jq -r '.state' <<<"$JSON2")" = "OPEN" ] || die "state changed during verification"
eval_checks "$JSON2"
[ "$SIG" = "$FIRST_SIG" ] || die "check set changed between snapshots (names/conclusions differ)"
assert_green >/dev/null
echo "3b snapshots identical, head stable"

# 4. merge (GitHub refuses if head moved: --match-head-commit)
if [ "$DRY" -eq 1 ]; then
  echo "4 dry-run: would run gh pr merge $PR --admin --merge --match-head-commit ${HEAD:0:12}"
  echo "DRY-RUN OK"
  exit 0
fi
if ! MERR="$(gh pr merge "$PR" --admin --merge --match-head-commit "$HEAD" 2>&1)"; then
  case "$MERR" in
    *"Merge already in progress"*) ;;
    *) echo "$MERR" >&2; die "gh pr merge failed" ;;
  esac
fi
echo "4 merge requested"

# 5. poll until merged
i=0; MSHA=""
while [ "$i" -lt 24 ]; do
  V="$(gh pr view "$PR" --json state,mergeCommit)"
  if [ "$(jq -r '.state' <<<"$V")" = "MERGED" ]; then
    MSHA="$(jq -r '.mergeCommit.oid' <<<"$V")"
    break
  fi
  sleep 5
  i=$((i + 1))
done
[ -n "$MSHA" ] || die "PR not MERGED after ~2min"
git fetch -q origin "$BASE"
git merge-base --is-ancestor "$MSHA" "origin/$BASE" || die "merge commit $MSHA not ancestor of origin/$BASE"
echo "5 MERGED $MSHA ancestor of origin/$BASE"

# 6. latest runs on base (no wait)
echo "6 runs on $BASE:"
gh run list --branch "$BASE" --limit 10 --json name,status,conclusion,headSha \
  --jq '.[] | "  \(.headSha[0:8]) \(.name) \(.status)/\(.conclusion)"'
