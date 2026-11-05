#!/usr/bin/env bash
# release-merge.sh - verify a PR is green on its current head, then merge it into a release/* branch.
# Usage: scripts/release-merge.sh <pr-number> [--expect-head <sha-prefix>] [--dry-run]
# Refuses non-release bases, non-OPEN PRs, head drift, conflicts, pending/failed checks, and zero checks.
set -euo pipefail

IGNORE_CONTEXT="submit-queue"   # mirrors ci-gate.yml IGNORE_CONTEXT (external advisory status)

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
NORM="$(jq -r '.statusCheckRollup[]? | [(.name // .context // "?"),
  (if .__typename == "StatusContext" then
     (if .state == "SUCCESS" then "SUCCESS" elif (.state == "PENDING" or .state == "EXPECTED") then "PENDING" else "FAILURE" end)
   elif .status != "COMPLETED" then "PENDING"
   else (.conclusion // "PENDING") end)] | join("|")' <<<"$JSON")"
EXAMINED=0; OK=0; SKIPPED=0; BAD=""; IGN=""
while IFS='|' read -r name bucket; do
  [ -n "$name" ] || continue
  if [ "$name" = "$IGNORE_CONTEXT" ]; then IGN="$IGN $name=$bucket"; continue; fi
  EXAMINED=$((EXAMINED + 1))
  case "$bucket" in
    SUCCESS|NEUTRAL) OK=$((OK + 1)) ;;
    SKIPPED) SKIPPED=$((SKIPPED + 1)) ;;
    *) BAD="$BAD $name($bucket)" ;;
  esac
done <<<"$NORM"
echo "2 checks examined=$EXAMINED ok=$OK skipped=$SKIPPED ignored:${IGN:- none}"
[ "$EXAMINED" -gt 0 ] || die "zero checks examined - cannot verify green"
[ -z "$BAD" ] || die "failing/pending checks:$BAD"

# 3. conflicts
[ "$MERGEABLE" != "CONFLICTING" ] || die "CONFLICTING (mergeStateStatus=$MSTATE) - resolve manually"
echo "3 mergeable=$MERGEABLE mergeStateStatus=$MSTATE"

# 4. merge
if [ "$DRY" -eq 1 ]; then
  echo "4 dry-run: would run gh pr merge $PR --admin --merge"
  echo "DRY-RUN OK"
  exit 0
fi
if ! MERR="$(gh pr merge "$PR" --admin --merge 2>&1)"; then
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
