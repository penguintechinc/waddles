#!/usr/bin/env bash
# release-auto-merge.sh - watch an ORDERED list of PRs and merge each into release/* once green.
# Usage: scripts/release-auto-merge.sh [--dry-run] [--interval S] [--max-hours H] <pr> [<pr> ...]
# Merges via scripts/release-merge.sh (all its guards apply). Reruns apt-mirror flakes
# ("timed out after 10 minutes") and a ci-gate that ran before reruns finished. Never resolves
# conflicts, never forces, never touches non-release bases.
set -euo pipefail

REPO="${RELEASE_AUTO_MERGE_REPO:-penguintechinc/waddles}"
IGNORE_CONTEXT="submit-queue"
MAX_RERUNS=2
INTERVAL=60; MAX_HOURS=6; DRY=0; PRS=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${TMPDIR:-/tmp}/release-auto-merge-${REPO##*/}"

die() { echo "ERROR: $*" >&2; exit 2; }
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --interval) [ $# -ge 2 ] || die "--interval needs a value"; INTERVAL="$2"; shift 2 ;;
    --max-hours) [ $# -ge 2 ] || die "--max-hours needs a value"; MAX_HOURS="$2"; shift 2 ;;
    -*) die "unknown flag $1" ;;
    *) case "$1" in ''|*[!0-9]*) die "bad PR number '$1'" ;; esac; PRS="$PRS $1"; shift ;;
  esac
done
[ -n "$PRS" ] || die "usage: release-auto-merge.sh [--dry-run] [--interval S] [--max-hours H] <pr> ..."
case "$INTERVAL$MAX_HOURS" in *[!0-9]*) die "interval/max-hours must be integers" ;; esac
mkdir -p "$STATE"

# once_flag <key>: returns 0 the first time a key is seen (for log-once), 1 afterwards.
once_flag() { if [ -e "$STATE/flag-$1" ]; then return 1; fi; : >"$STATE/flag-$1"; }
# rerun_count <runid> / bump_rerun <runid>: per-run rerun counter persisted in the state dir.
rerun_count() { if [ -e "$STATE/rerun-$1" ]; then cat "$STATE/rerun-$1"; else echo 0; fi; }
bump_rerun() { echo $(( $(rerun_count "$1") + 1 )) >"$STATE/rerun-$1"; }

# try_rerun <pr> <head> <runid> <why>: rerun failed jobs of a run, bounded by MAX_RERUNS.
try_rerun() {
  local n; n="$(rerun_count "$3")"
  if [ "$n" -ge "$MAX_RERUNS" ]; then
    if once_flag "cap-$3"; then log "pr=$1 head=${2:0:12} run=$3 SKIP rerun cap ($MAX_RERUNS) reached"; fi
    return 0
  fi
  if [ "$DRY" = 1 ]; then log "pr=$1 head=${2:0:12} run=$3 DRY-RUN would rerun --failed ($4)"; return 0; fi
  log "pr=$1 head=${2:0:12} run=$3 RERUN --failed ($4) attempt=$((n + 1))"
  bump_rerun "$3"
  gh run rerun "$3" --failed -R "$REPO" >/dev/null
}

# eval_pr <pr>: examine one PR; sets RESULT to merged|open|closed|waiting|skipped.
eval_pr() {
  local pr="$1" json base head hname state mstate norm name bucket run
  RESULT=open
  json="$(gh pr view "$pr" -R "$REPO" --json baseRefName,headRefName,headRefOid,mergeStateStatus,state,statusCheckRollup)"
  state="$(jq -r .state <<<"$json")"; base="$(jq -r .baseRefName <<<"$json")"
  head="$(jq -r .headRefOid <<<"$json")"; hname="$(jq -r .headRefName <<<"$json")"
  mstate="$(jq -r .mergeStateStatus <<<"$json")"
  case "$state" in MERGED) RESULT=merged; log "pr=$pr MERGED"; return 0 ;; CLOSED) RESULT=closed; log "pr=$pr CLOSED"; return 0 ;; esac
  echo "$hname" >"$STATE/branch-$pr"

  case "$base" in
    release/*) ;;
    *)
      # Order dependency: stacked on an earlier listed PR's branch -> wait for it.
      local p
      for p in $PRS; do
        [ "$p" = "$pr" ] && break
        if [ -e "$STATE/branch-$p" ] && [ "$(cat "$STATE/branch-$p")" = "$base" ]; then
          RESULT=waiting; log "pr=$pr head=${head:0:12} WAIT base '$base' is open PR #$p's branch"; return 0
        fi
      done
      RESULT=skipped
      if once_flag "base-$pr"; then log "pr=$pr head=${head:0:12} REFUSE base '$base' is not release/* (never merging)"; fi
      return 0 ;;
  esac

  if [ "$mstate" = "DIRTY" ]; then
    RESULT=skipped
    if once_flag "dirty-$pr-$head"; then log "pr=$pr head=${head:0:12} SKIP mergeStateStatus=DIRTY (conflicts; needs human)"; fi
    return 0
  fi

  # name|bucket|runid per check (CheckRun + StatusContext normalized).
  norm="$(jq -r '.statusCheckRollup[]? | [(.name // .context // "?"),
    (if .__typename == "StatusContext" then
       (if .state == "SUCCESS" then "SUCCESS" elif (.state == "PENDING" or .state == "EXPECTED") then "PENDING" else "FAILURE" end)
     elif .status != "COMPLETED" then "PENDING"
     else (.conclusion // "PENDING") end),
    ((.detailsUrl // "") | capture("/runs/(?<r>[0-9]+)") | .r // "")] | join("|")' <<<"$json")"

  local gate="" gate_run="" n_pending=0 n_bad=0 n_other_ok=0 n_exam=0 bad_runs="" bad_names=""
  while IFS='|' read -r name bucket run; do
    [ -n "$name" ] || continue
    [ "$name" = "$IGNORE_CONTEXT" ] && continue
    [ "$bucket" = "SKIPPED" ] && continue
    n_exam=$((n_exam + 1))
    if [ "$name" = "ci-gate" ]; then gate="$bucket"; gate_run="$run"; continue; fi
    case "$bucket" in
      SUCCESS|NEUTRAL) n_other_ok=$((n_other_ok + 1)) ;;
      PENDING) n_pending=$((n_pending + 1)) ;;
      *) n_bad=$((n_bad + 1)); bad_runs="$bad_runs $run"; bad_names="$bad_names [$name]" ;;
    esac
  done <<<"$norm"
  log "pr=$pr head=${head:0:12} mstate=$mstate gate=${gate:-none} checks=$n_exam ok=$n_other_ok pending=$n_pending failed=$n_bad"

  if [ "$gate" = "SUCCESS" ]; then
    if [ "$DRY" = 1 ]; then log "pr=$pr head=${head:0:12} DRY-RUN would merge via release-merge.sh --expect-head ${head:0:12}"; return 0; fi
    log "pr=$pr head=${head:0:12} MERGE via release-merge.sh"
    if "$HERE/release-merge.sh" "$pr" --expect-head "${head:0:12}"; then RESULT=merged; log "pr=$pr head=${head:0:12} MERGED"
    else log "pr=$pr head=${head:0:12} release-merge refused (see above); will retry next tick"; fi
    return 0
  fi

  if [ "$n_bad" -gt 0 ]; then
    local r flake=0 real=0
    for r in $(printf '%s\n' "$bad_runs" | sort -u); do
      [ -n "$r" ] || { real=1; continue; }
      if gh run view "$r" -R "$REPO" --log-failed 2>/dev/null | grep -q 'timed out after 10 minutes'; then
        flake=1; try_rerun "$pr" "$head" "$r" "apt-mirror timeout"
      else real=1; fi
    done
    if [ "$real" = 1 ] && once_flag "real-$pr-$head"; then log "pr=$pr head=${head:0:12} REAL FAILURE:$bad_names (no rerun; needs human)"; fi
    [ "$flake" = 1 ] || RESULT=skipped
    return 0
  fi

  if [ "$gate" = "FAILURE" ] && [ "$n_pending" -eq 0 ] && [ "$n_other_ok" -gt 0 ] && [ -n "$gate_run" ]; then
    try_rerun "$pr" "$head" "$gate_run" "ci-gate ran before reruns finished; other checks now green"
  fi
  return 0
}

START="$(date +%s)"; DEADLINE=$((START + MAX_HOURS * 3600))
log "start repo=$REPO prs=[${PRS# }] dry_run=$DRY interval=${INTERVAL}s max=${MAX_HOURS}h state=$STATE"
while :; do
  examined=0; n_open=0; n_merged=0; n_closed=0; n_skip=0; n_wait=0; merged_now=0
  for pr in $PRS; do
    eval_pr "$pr"; examined=$((examined + 1))
    case "$RESULT" in
      merged) n_merged=$((n_merged + 1)); [ -e "$STATE/done-$pr" ] || { : >"$STATE/done-$pr"; [ "$DRY" = 1 ] || merged_now=1; } ;;
      closed) n_closed=$((n_closed + 1)) ;;
      skipped) n_skip=$((n_skip + 1)); n_open=$((n_open + 1)) ;;
      waiting) n_wait=$((n_wait + 1)); n_open=$((n_open + 1)) ;;
      *) n_open=$((n_open + 1)) ;;
    esac
    # After a merge, later PRs need re-evaluation against the new base head: restart the tick.
    [ "$merged_now" = 1 ] && break
  done
  [ "$examined" -gt 0 ] || die "zero PRs examined"
  log "TICK examined=$examined open=$n_open merged=$n_merged closed=$n_closed skipped=$n_skip waiting=$n_wait"
  if [ "$n_open" -eq 0 ]; then log "all PRs merged/closed; done"; exit 0; fi
  if [ "$DRY" = 1 ]; then log "dry-run: single tick only"; exit 0; fi
  [ "$merged_now" = 1 ] && continue
  [ "$(date +%s)" -lt "$DEADLINE" ] || { log "max duration ${MAX_HOURS}h reached with $n_open open"; exit 1; }
  sleep "$INTERVAL"
done
