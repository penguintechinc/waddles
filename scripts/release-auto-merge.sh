#!/usr/bin/env bash
# release-auto-merge.sh - watch an ORDERED list of PRs and merge each into release/* once green.
# Usage: scripts/release-auto-merge.sh [--dry-run] [--exit-on-stuck] [--interval S] [--max-hours H] <pr|pr@fullsha> ...
#   --dry-run        one tick, report only; never reruns or merges
#   --exit-on-stuck  exit 1 as soon as every open PR is stuck (real failure, rerun cap reached, zero
#                    checks, head drift, conflicts, non-release base) instead of waiting --max-hours
#                    (`make merge-when-green` uses this for a single PR)
#   -h, --help       print this header and exit
# PR@SHA pins the reviewed head: if the PR head differs it is skipped (needs human), never merged.
# Merges via scripts/release-merge.sh <pr> <full-sha> 1 (head-pinned, non-admin; all its guards apply).
# Reruns infra flakes once per run (FLAKE_PATTERN: apt-mirror "timed out after 10 minutes", sccache
# "Timed out waiting for server startup") and a stale ci-gate (it aggregates other runs and does not
# re-evaluate after reruns). Never resolves conflicts, never forces, never touches non-release bases.
# Env: RELEASE_AUTO_MERGE_REPO or REPO (default `gh repo view`), FLAKE_PATTERN (ERE over job logs),
#      NO_CHECKS_TICKS (default 10: consecutive ticks with ZERO checks examined before a PR is stuck).
set -euo pipefail

REPO="${RELEASE_AUTO_MERGE_REPO:-${REPO:-}}"
FLAKE_PATTERN="${FLAKE_PATTERN:-timed out after 10 minutes|apt attempt .*(failed|timed out)|sccache: error: Timed out waiting for server startup}"
NO_CHECKS_TICKS="${NO_CHECKS_TICKS:-10}"
EXIT_ON_STUCK=0
IGNORE_CONTEXT="submit-queue"
MAX_RERUNS=2
MAX_API_FAIL_TICKS=10   # consecutive ticks with any GitHub API read failure before aborting
INTERVAL=60; MAX_HOURS=6; DRY=0; PRS=""; PINS=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "ERROR: $*" >&2; exit 2; }
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --exit-on-stuck) EXIT_ON_STUCK=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    --interval) [ $# -ge 2 ] || die "--interval needs a value"; INTERVAL="$2"; shift 2 ;;
    --max-hours) [ $# -ge 2 ] || die "--max-hours needs a value"; MAX_HOURS="$2"; shift 2 ;;
    -*) die "unknown flag $1" ;;
    *) a_pr="${1%%@*}"; a_sha=""
       case "$1" in *@*) a_sha="${1#*@}" ;; esac
       case "$a_pr" in ''|*[!0-9]*) die "bad PR number '$1'" ;; esac
       if [ -n "$a_sha" ]; then
         [ "${#a_sha}" -eq 40 ] || die "pin for PR $a_pr must be a full 40-char SHA"
         case "$a_sha" in *[!0-9a-fA-F]*) die "pin for PR $a_pr is not hex" ;; esac
         PINS="$PINS $a_pr=$a_sha"
       fi
       PRS="$PRS $a_pr"; shift ;;
  esac
done
[ -n "$PRS" ] || die "usage: release-auto-merge.sh [--dry-run] [--interval S] [--max-hours H] <pr> ..."
case "$INTERVAL$MAX_HOURS" in *[!0-9]*) die "interval/max-hours must be integers" ;; esac
case "$NO_CHECKS_TICKS" in ''|*[!0-9]*|0) die "NO_CHECKS_TICKS must be a positive integer" ;; esac
if [ -z "$REPO" ]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)" || die "cannot determine repo (set REPO)"
fi
case "$REPO" in */*) ;; *) die "bad repo '$REPO' (want owner/name)" ;; esac
STATE="${RELEASE_AUTO_MERGE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/release-auto-merge/${REPO##*/}}"
# Private state dir: 0700, owned by us, never a symlink (blocks planted-file/symlink attacks).
if [ -L "$STATE" ]; then die "state dir $STATE is a symlink"; fi
mkdir -p "$(dirname "$STATE")"
[ -d "$STATE" ] || mkdir -m 700 "$STATE"
if [ -L "$STATE" ] || [ ! -d "$STATE" ] || [ ! -O "$STATE" ]; then die "state dir $STATE not a directory owned by current user"; fi
chmod 700 "$STATE"

# pin_of <pr>: prints the pinned full SHA for a PR, or nothing if unpinned.
pin_of() { local e; for e in $PINS; do if [ "${e%%=*}" = "$1" ]; then echo "${e#*=}"; return 0; fi; done; return 0; }

# once_flag <key>: returns 0 the first time a key is seen (for log-once), 1 afterwards.
once_flag() { if [ -e "$STATE/flag-$1" ]; then return 1; fi; : >"$STATE/flag-$1"; }
# rerun_count <runid> / bump_rerun <runid>: per-run rerun counter persisted in the state dir.
rerun_count() { if [ -e "$STATE/rerun-$1" ]; then cat "$STATE/rerun-$1"; else echo 0; fi; }
bump_rerun() { echo $(( $(rerun_count "$1") + 1 )) >"$STATE/rerun-$1"; }

# try_rerun <pr> <head> <runid> <why>: rerun failed jobs of a run, bounded by MAX_RERUNS; returns 1 once capped.
try_rerun() {
  local n; n="$(rerun_count "$3")"
  if [ "$n" -ge "$MAX_RERUNS" ]; then
    if once_flag "cap-$3"; then log "pr=$1 head=${2:0:12} run=$3 SKIP rerun cap ($MAX_RERUNS) reached"; fi
    return 1
  fi
  if [ "$DRY" = 1 ]; then log "pr=$1 head=${2:0:12} run=$3 DRY-RUN would rerun --failed ($4)"; return 0; fi
  log "pr=$1 head=${2:0:12} run=$3 RERUN --failed ($4) attempt=$((n + 1))"
  bump_rerun "$3"
  if ! gh run rerun "$3" --failed -R "$REPO" >/dev/null 2>"$STATE/rerun-err-$3"; then
    log "pr=$1 head=${2:0:12} run=$3 RERUN refused by GitHub (run likely still in progress): $(head -c 200 "$STATE/rerun-err-$3"); will retry next tick"
    echo $(( $(rerun_count "$3") - 1 )) >"$STATE/rerun-$3"
  fi
}

# is_flake_job <jobid>: 0 if the job log matches FLAKE_PATTERN (apt-mirror timeout, sccache startup timeout).
# The log is captured into a variable first, never piped, so grep -q cannot SIGPIPE gh under pipefail. An unreadable log counts as NOT a flake (fail closed).
is_flake_job() {
  local logs
  if ! logs="$(gh api "repos/$REPO/actions/jobs/$1/logs" 2>/dev/null)"; then
    if once_flag "logfail-$1"; then log "job=$1 WARN could not fetch job log; treating as real failure"; fi
    return 1
  fi
  grep -Eq -e "$FLAKE_PATTERN" <<<"$logs"
}

# eval_pr <pr>: examine one PR; sets RESULT to merged|open|closed|waiting|skipped|apierr.
eval_pr() {
  local pr="$1" json base head hname state mstate norm name bucket run job pin
  RESULT=open
  if ! json="$(gh pr view "$pr" -R "$REPO" --json baseRefName,headRefName,headRefOid,mergeStateStatus,state,statusCheckRollup 2>"$STATE/api-err")"; then
    RESULT=apierr; log "pr=$pr API error, retry next tick: $(head -c 200 "$STATE/api-err")"; return 0
  fi
  state="$(jq -r .state <<<"$json")"; base="$(jq -r .baseRefName <<<"$json")"
  head="$(jq -r .headRefOid <<<"$json")"; hname="$(jq -r .headRefName <<<"$json")"
  mstate="$(jq -r .mergeStateStatus <<<"$json")"
  case "$state" in MERGED) RESULT=merged; log "pr=$pr MERGED"; return 0 ;; CLOSED) RESULT=closed; log "pr=$pr CLOSED"; return 0 ;; esac
  echo "$hname" >"$STATE/branch-$pr"
  pin="$(pin_of "$pr")"
  if [ -n "$pin" ]; then
    if [ "$pin" != "$head" ]; then
      RESULT=skipped
      if once_flag "pinchg-$pr-$head"; then log "pr=$pr head=$head pinned=$pin head changed, needs human (not merging)"; fi
      return 0
    fi
  elif once_flag "unpinned-$pr"; then log "pr=$pr head=$head WARN unpinned: merges whatever head is green (use PR@SHA to pin)"; fi

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
    ((.detailsUrl // "") | capture("/runs/(?<r>[0-9]+)") | .r // ""),
    ((.detailsUrl // "") | capture("/job/(?<j>[0-9]+)") | .j // "")] | join("|")' <<<"$json")"

  local gate="" gate_run="" n_pending=0 n_bad=0 n_other_ok=0 n_exam=0 bad_jobs="" bad_names=""
  while IFS='|' read -r name bucket run job; do
    [ -n "$name" ] || continue
    [ "$name" = "$IGNORE_CONTEXT" ] && continue
    [ "$bucket" = "SKIPPED" ] && continue
    n_exam=$((n_exam + 1))
    if [ "$name" = "ci-gate" ]; then gate="$bucket"; gate_run="$run"; continue; fi
    case "$bucket" in
      SUCCESS|NEUTRAL) n_other_ok=$((n_other_ok + 1)) ;;
      PENDING) n_pending=$((n_pending + 1)) ;;
      *) n_bad=$((n_bad + 1)); bad_jobs="$bad_jobs ${run:-none}:${job:-none}"; bad_names="$bad_names [$name]" ;;
    esac
  done <<<"$norm"
  log "pr=$pr head=${head:0:12} mstate=$mstate gate=${gate:-none} checks=$n_exam ok=$n_other_ok pending=$n_pending failed=$n_bad"

  # Zero checks examined is never green: wait NO_CHECKS_TICKS ticks for CI to register, then the PR is stuck.
  if [ "$n_exam" -eq 0 ]; then
    local z=0; [ -e "$STATE/zero-$pr" ] && z="$(cat "$STATE/zero-$pr")"; z=$((z + 1)); echo "$z" >"$STATE/zero-$pr"
    if [ "$z" -ge "$NO_CHECKS_TICKS" ]; then
      RESULT=skipped; log "pr=$pr head=${head:0:12} FAIL zero checks examined for $z ticks (needs human)"
    else log "pr=$pr head=${head:0:12} WAIT zero checks examined ($z/$NO_CHECKS_TICKS)"; fi
    return 0
  fi
  rm -f "$STATE/zero-$pr"

  if [ "$gate" = "SUCCESS" ]; then
    if [ "$DRY" = 1 ]; then log "pr=$pr head=${head:0:12} DRY-RUN would merge via release-merge.sh pinned to $head"; return 0; fi
    log "pr=$pr head=${head:0:12} MERGE via release-merge.sh"
    if "$HERE/release-merge.sh" "$pr" "$head" 1; then RESULT=merged; log "pr=$pr head=${head:0:12} MERGED"
    else log "pr=$pr head=${head:0:12} release-merge refused (see above); will retry next tick"; fi
    return 0
  fi

  if [ "$n_bad" -gt 0 ]; then
    local rj r jb is_flake runs flake=0 real=0 run_real
    runs="$(for rj in $bad_jobs; do echo "${rj%%:*}"; done | sort -u)"
    for r in $runs; do
      run_real=0
      for rj in $bad_jobs; do
        [ "${rj%%:*}" = "$r" ] || continue
        jb="${rj#*:}"; is_flake=0
        if [ "$r" != none ] && [ "$jb" != none ]; then
          if is_flake_job "$jb"; then is_flake=1; fi
        fi
        [ "$is_flake" = 1 ] || run_real=1
      done
      if [ "$run_real" = 0 ]; then
        if try_rerun "$pr" "$head" "$r" "all failed jobs are infra flakes"; then flake=1; fi
      else real=1; fi
    done
    if [ "$real" = 1 ] && once_flag "real-$pr-$head"; then log "pr=$pr head=${head:0:12} REAL FAILURE:$bad_names (non-flake failed job; no rerun; needs human)"; fi
    [ "$flake" = 1 ] || RESULT=skipped
    return 0
  fi

  if [ "$gate" = "FAILURE" ] && [ "$n_pending" -eq 0 ] && [ "$n_other_ok" -gt 0 ] && [ -n "$gate_run" ]; then
    if ! try_rerun "$pr" "$head" "$gate_run" "ci-gate ran before reruns finished; other checks now green"; then RESULT=skipped; fi
  fi
  return 0
}

API_FAIL_TICKS=0
START="$(date +%s)"; DEADLINE=$((START + MAX_HOURS * 3600))
log "start repo=$REPO prs=[${PRS# }] dry_run=$DRY interval=${INTERVAL}s max=${MAX_HOURS}h state=$STATE"
while :; do
  examined=0; n_open=0; n_merged=0; n_closed=0; n_skip=0; n_wait=0; merged_now=0; n_api=0
  for pr in $PRS; do
    eval_pr "$pr"; examined=$((examined + 1))
    case "$RESULT" in
      merged) n_merged=$((n_merged + 1)); [ -e "$STATE/done-$pr" ] || { : >"$STATE/done-$pr"; [ "$DRY" = 1 ] || merged_now=1; } ;;
      closed) n_closed=$((n_closed + 1)) ;;
      skipped) n_skip=$((n_skip + 1)); n_open=$((n_open + 1)) ;;
      apierr) n_api=$((n_api + 1)); n_open=$((n_open + 1)) ;;
      waiting) n_wait=$((n_wait + 1)); n_open=$((n_open + 1)) ;;
      *) n_open=$((n_open + 1)) ;;
    esac
    # After a merge, later PRs need re-evaluation against the new base head: restart the tick.
    [ "$merged_now" = 1 ] && break
  done
  [ "$examined" -gt 0 ] || die "zero PRs examined"
  log "TICK examined=$examined open=$n_open merged=$n_merged closed=$n_closed skipped=$n_skip waiting=$n_wait api_errors=$n_api"
  if [ "$n_api" -gt 0 ]; then
    API_FAIL_TICKS=$((API_FAIL_TICKS + 1))
    if [ "$API_FAIL_TICKS" -ge "$MAX_API_FAIL_TICKS" ]; then die "GitHub API failed on $API_FAIL_TICKS consecutive ticks; aborting"; fi
  else API_FAIL_TICKS=0; fi
  # A merge breaks the PR loop early, so the tally is partial: re-evaluate everything, never declare done.
  if [ "$merged_now" = 1 ]; then continue; fi
  if [ "$n_open" -eq 0 ]; then log "all PRs merged/closed; done"; exit 0; fi
  if [ "$EXIT_ON_STUCK" = 1 ] && [ "$n_skip" -eq "$n_open" ]; then log "FAIL: every open PR is stuck ($n_skip); --exit-on-stuck"; exit 1; fi
  if [ "$DRY" = 1 ]; then log "dry-run: single tick only"; exit 0; fi
  [ "$(date +%s)" -lt "$DEADLINE" ] || { log "max duration ${MAX_HOURS}h reached with $n_open open"; exit 1; }
  sleep "$INTERVAL"
done
