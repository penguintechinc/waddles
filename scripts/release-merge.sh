#!/usr/bin/env bash
# release-merge.sh - wait for a PR's WHOLE check set to finish, then merge it (non-admin, head-pinned)
# into a release/* branch.
#
# Usage: scripts/release-merge.sh <pr> [<full-head-sha>] [max_minutes] [--dry-run]
#   <full-head-sha>  40-hex head the reviewer saw; the merge is pinned to it (`--match-head-commit`)
#                    and the script aborts if the PR head moves. Omitted: the head is read ONCE at
#                    start and pinned.
#   max_minutes      polls before giving up (default 90; one poll per POLL_SECONDS, default 60)
#   --dry-run        one poll: print bucket counts and what it would do; never merges
# Env: REPO (default: `gh repo view`), IGNORE_CHECKS (space-separated, default "submit-queue"),
#      REQUIRED_CHECK (default "ci-gate"), POLL_SECONDS, GH_RETRIES (5), GH_RETRY_SLEEP (10 * attempt)
#
# Why it waits itself: the release ruleset has NO required status checks, so `gh pr merge --auto`
# would merge immediately. Green = REQUIRED_CHECK passes AND zero pending AND zero failed checks.
# submit-queue is an external status mirroring release-branch health; ci-gate.yml ignores it too
# (IGNORE_CONTEXT), so it is ignored here by default. Never uses --admin; never merges into main
# (base must match release/*). Check buckets come from `gh pr view --json statusCheckRollup`
# (this gh has no `gh pr checks --json`; masking that with `|| true` once looped silently to timeout).
# Exit: 0 merged (or dry-run ok), 1 a check failed / refused, 2 head moved, 3 timeout,
#       4 bad usage / non-release base.
set -euo pipefail

IGNORE_CHECKS="${IGNORE_CHECKS:-submit-queue}"
REQUIRED_CHECK="${REQUIRED_CHECK:-ci-gate}"
POLL_SECONDS="${POLL_SECONDS:-60}"
GH_RETRIES="${GH_RETRIES:-5}"

# usage prints the CLI shape to stderr and exits 4.
usage() { echo "usage: release-merge.sh <pr> [<full-head-sha>] [max_minutes] [--dry-run]" >&2; exit 4; }

DRY=0; POS=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -*) echo "unknown flag $a" >&2; usage ;;
    *) POS+=("$a") ;;
  esac
done
{ [ "${#POS[@]}" -ge 1 ] && [ "${#POS[@]}" -le 3 ]; } || usage
pr="${POS[0]}"; sha=""; max=90
case "$pr" in ''|*[!0-9]*) usage ;; esac
if [ "${#POS[@]}" -ge 2 ]; then
  if [[ "${POS[1]}" =~ ^[0-9a-fA-F]{40}$ ]]; then
    sha="${POS[1],,}"
    if [ "${#POS[@]}" -ge 3 ]; then max="${POS[2]}"; fi
  elif [ "${#POS[@]}" -eq 2 ] && [[ "${POS[1]}" =~ ^[0-9]{1,5}$ ]]; then
    max="${POS[1]}"   # sha omitted
  else
    echo "head sha must be the full 40-hex commit" >&2; usage
  fi
fi
case "$max" in ''|*[!0-9]*) usage ;; esac

# ghr runs gh with retries (api.github.com i/o timeouts are frequent from this host); a final failure is loud.
ghr() {
  local n out err
  err="$(mktemp)"
  for ((n = 1; n <= GH_RETRIES; n++)); do
    if out="$(gh "$@" 2>"$err")"; then rm -f "$err"; printf '%s' "$out"; return 0; fi
    echo "gh retry $n/$GH_RETRIES: $(head -c 120 "$err")" >&2
    if [ "$n" -lt "$GH_RETRIES" ]; then sleep "${GH_RETRY_SLEEP:-$((n * 10))}"; fi
  done
  rm -f "$err"; echo "gh failed ${GH_RETRIES}x: $*" >&2; return 1
}

repo="${REPO:-$(ghr repo view --json nameWithOwner --jq .nameWithOwner)}"
[ -n "$repo" ] || { echo "cannot determine repo (set REPO)" >&2; exit 4; }

# view <json-field> <jq-expr>: one field of the PR via the retry wrapper.
view() { ghr pr view "$pr" -R "$repo" --json "$1" --jq "$2"; }

# Base guard: never merge anything but release/* (never main); PR must be OPEN.
base="$(view baseRefName .baseRefName)"
state="$(view state .state)"
case "$base" in release/*) ;; *) echo "REFUSE: base '$base' is not release/* (never merge into main)"; exit 4 ;; esac
[ "$state" = "OPEN" ] || { echo "REFUSE: PR #$pr is $state, not OPEN"; exit 1; }

if [ -z "$sha" ]; then
  sha="$(view headRefOid .headRefOid)"
  echo "no sha given: pinning to current head $sha"
fi

BUCKETS='[.statusCheckRollup[]?
  | {name:(.name // .context), s:((.conclusion // .state // .status // "") | ascii_downcase)}
  | select(([.name] | inside($ign | split(" "))) | not)
  | .bucket = (if (.s|test("^(success|neutral)$")) then "pass"
               elif .s=="skipped" then "skipping"
               elif (.s|test("fail|error|timed_out|action_required|startup_failure")) then "fail"
               elif .s=="cancelled" then "cancel"
               else "pending" end)]'

for ((i = 0; i < max; i++)); do
  head="$(view headRefOid .headRefOid)"
  if [ "$head" != "$sha" ]; then echo "ABORT: head moved to $head (pinned $sha)"; exit 2; fi
  raw="$(ghr pr view "$pr" -R "$repo" --json statusCheckRollup)"
  checks="$(jq --arg ign "$IGNORE_CHECKS" "$BUCKETS" <<<"$raw")"
  total="$(jq length <<<"$checks")"
  if [ "$total" -gt 0 ]; then
    pending="$(jq '[.[]|select(.bucket=="pending")]|length' <<<"$checks")"
    passed="$(jq '[.[]|select(.bucket=="pass")]|length' <<<"$checks")"
    failed="$(jq -r '[.[]|select(.bucket=="fail" or .bucket=="cancel")|.name]|join(", ")' <<<"$checks")"
    nfail="$(jq '[.[]|select(.bucket=="fail" or .bucket=="cancel")]|length' <<<"$checks")"
    gate="$(jq -r --arg g "$REQUIRED_CHECK" '[.[]|select(.name==$g)|.bucket]|first // "missing"' <<<"$checks")"
    echo "$(date -u +%H:%MZ) total=$total pass=$passed pending=$pending fail=$nfail $REQUIRED_CHECK=$gate (ignored: $IGNORE_CHECKS)"
    if [ -n "$failed" ]; then echo "FAIL: $failed"; exit 1; fi
    if [ "$pending" -eq 0 ] && [ "$gate" = "pass" ]; then
      if [ "$DRY" -eq 1 ]; then
        echo "DRY-RUN: green; would run: gh pr merge $pr -R $repo --merge --match-head-commit $sha"; exit 0
      fi
      if ! merr="$(gh pr merge "$pr" -R "$repo" --merge --match-head-commit "$sha" 2>&1)"; then
        now="$(view state .state)"
        if [ "$now" != "MERGED" ]; then
          head2="$(view headRefOid .headRefOid)"
          if [ "$head2" != "$sha" ]; then echo "ABORT: head moved to $head2 (pinned $sha)"; exit 2; fi
          echo "merge failed: $merr" >&2; exit 1
        fi
      fi
      echo "MERGED: $(view mergeCommit .mergeCommit.oid)"
      exit 0
    fi
    if [ "$DRY" -eq 1 ]; then echo "DRY-RUN: not green yet; would keep waiting (up to $max polls)"; exit 0; fi
  else
    echo "no checks yet"
    if [ "$DRY" -eq 1 ]; then echo "DRY-RUN: no checks yet; would keep waiting"; exit 0; fi
  fi
  sleep "$POLL_SECONDS"
done
echo "TIMEOUT after $max polls"; exit 3
