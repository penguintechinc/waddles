#!/usr/bin/env bash
# Self-test for scripts/release-auto-merge.sh: gh shim + stub release-merge; dry-run one tick, no network.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../release-auto-merge.sh"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/release-auto-merge-test.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/bin"
H="abcdef1234567890abcdef1234567890abcdef12"
OTHER="1111111111111111111111111111111111111111"

# gh shim: pr view -> $FIXTURE; api jobs/<id>/logs -> $SCRATCH/log-<id>; everything else fails loudly.
cat > "$SCRATCH/bin/gh" <<'SHIM'
#!/usr/bin/env bash
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  if [ -n "${FAIL_PRVIEW:-}" ]; then
    n=0; [ -f "$SCRATCH/prview.n" ] && n="$(cat "$SCRATCH/prview.n")"
    n=$((n + 1)); echo "$n" > "$SCRATCH/prview.n"
    if [ "$FAIL_PRVIEW" = always ] || [ "$n" -le "$FAIL_PRVIEW" ]; then
      echo "Post https://api.github.com/graphql: dial tcp: i/o timeout" >&2; exit 1
    fi
  fi
  cat "$FIXTURE"; exit 0
fi
if [ "$1" = "api" ]; then
  id="${2#repos/*/actions/jobs/}"; id="${id%/logs}"
  [ -f "$SCRATCH/log-$id" ] || exit 1
  cat "$SCRATCH/log-$id"; exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "rerun" ]; then echo "run $3 cannot be rerun; its workflow file may be broken" >&2; exit 1; fi
echo "gh shim: unexpected call: $*" >&2; exit 99
SHIM
chmod +x "$SCRATCH/bin/gh"

PASS=0; FAIL=0
url() { echo "https://github.com/o/r/actions/runs/$1/job/$2"; }
# mk <head> <checks-json>
mk() {
  printf '{"baseRefName":"release/v3.0.X","headRefName":"b","headRefOid":"%s","mergeStateStatus":"UNSTABLE","state":"OPEN","statusCheckRollup":%s}\n' "$1" "$2" > "$SCRATCH/f.json"
}
fail_chk() { printf '{"__typename":"CheckRun","name":"%s","status":"COMPLETED","conclusion":"FAILURE","detailsUrl":"%s"}' "$1" "$(url "$2" "$3")"; }
GATE_OK='{"__typename":"CheckRun","name":"ci-gate","status":"COMPLETED","conclusion":"SUCCESS"}'
GATE_BAD="$(fail_chk ci-gate 9 99)"

# expect <name> <rx> <args...>
expect() {
  local name="$1" rx="$2" out rc=0; shift 2
  out="$(env SCRATCH="$SCRATCH" FIXTURE="$SCRATCH/f.json" RELEASE_AUTO_MERGE_STATE="$SCRATCH/state" PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" --dry-run "$@" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ] && grep -Eq "$rx" <<<"$out"; then PASS=$((PASS + 1)); echo "ok   $name"
  else FAIL=$((FAIL + 1)); echo "FAIL $name (rc=$rc, want /$rx/)"; printf "%s\n" "${out//$'\n'/$'\n     '}"; fi
  rm -rf "$SCRATCH/state"
}

echo "step timed out after 10 minutes" > "$SCRATCH/log-11"; echo "apt attempt 3 failed after 60s" > "$SCRATCH/log-12"
echo "error[E0308]: mismatched types" > "$SCRATCH/log-13"

mk "$H" "[$GATE_OK]";                                  expect "green unpinned: warn + full-sha merge" "would merge via release-merge.sh --expect-head $H" 5
mk "$H" "[$GATE_OK]";                                  expect "green pinned matches" "would merge via release-merge.sh --expect-head $H" "5@$H"
mk "$OTHER" "[$GATE_OK]";                              expect "pinned head drift: skip" "head changed, needs human" "5@$H"
mk "$H" "[$GATE_BAD,$(fail_chk a 7 11)]";              expect "timeout job -> rerun" "would rerun --failed \(all failed jobs" 5
mk "$H" "[$GATE_BAD,$(fail_chk a 7 12)]";              expect "apt attempt job -> rerun" "would rerun --failed \(all failed jobs" 5
mk "$H" "[$GATE_BAD,$(fail_chk a 7 11),$(fail_chk b 7 13)]"; expect "mixed run -> REAL" "REAL FAILURE" 5
mk "$H" "[$GATE_BAD,$(fail_chk a 7 14)]";              expect "unreadable log -> REAL" "REAL FAILURE" 5

# live (non-dry) rerun refused by GitHub: watcher must keep running, retry next tick, not consume the counter
mk "$H" "[$GATE_BAD,$(fail_chk a 7 11)]"; rc=0
out="$(env SCRATCH="$SCRATCH" FIXTURE="$SCRATCH/f.json" RELEASE_AUTO_MERGE_STATE="$SCRATCH/state2" PATH="$SCRATCH/bin:$PATH" timeout 5 bash "$SCRIPT" --interval 1 5 2>&1)" || rc=$?
n="$(grep -c 'RERUN refused' <<<"$out" || true)"
if [ "$rc" -eq 124 ] && [ "$n" -ge 2 ] && [ "$(cat "$SCRATCH/state2/rerun-7")" = 0 ]; then
  PASS=$((PASS + 1)); echo "ok   refused rerun: watcher survives, retries ($n), counter unspent"
else FAIL=$((FAIL + 1)); echo "FAIL refused rerun (rc=$rc refused=$n)"; printf '%s\n' "$out"; fi

# transient API error: first gh pr view fails, watcher logs + retries, then evaluates normally
mk "$H" "[$GATE_BAD,$(fail_chk a 7 13)]"; rc=0; rm -f "$SCRATCH/prview.n"
out="$(env SCRATCH="$SCRATCH" FAIL_PRVIEW=1 FIXTURE="$SCRATCH/f.json" RELEASE_AUTO_MERGE_STATE="$SCRATCH/state3" PATH="$SCRATCH/bin:$PATH" timeout 5 bash "$SCRIPT" --interval 1 5 2>&1)" || rc=$?
if [ "$rc" -eq 124 ] && grep -q 'API error, retry next tick' <<<"$out" && grep -q 'REAL FAILURE' <<<"$out"; then
  PASS=$((PASS + 1)); echo "ok   API error once: watcher survives and recovers"
else FAIL=$((FAIL + 1)); echo "FAIL api retry (rc=$rc)"; printf '%s\n' "$out"; fi

# persistent API failure aborts loudly after 10 consecutive ticks
rc=0; rm -f "$SCRATCH/prview.n"
out="$(env SCRATCH="$SCRATCH" FAIL_PRVIEW=always FIXTURE="$SCRATCH/f.json" RELEASE_AUTO_MERGE_STATE="$SCRATCH/state4" PATH="$SCRATCH/bin:$PATH" timeout 20 bash "$SCRIPT" --interval 0 5 2>&1)" || rc=$?
if [ "$rc" -eq 2 ] && grep -q 'consecutive ticks; aborting' <<<"$out"; then
  PASS=$((PASS + 1)); echo "ok   persistent API failure aborts after 10 ticks"
else FAIL=$((FAIL + 1)); echo "FAIL api abort (rc=$rc)"; printf '%s\n' "$out" | tail -5; fi

# bad pin / symlinked state must be refused (non-zero exit)
rc=0; PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" --dry-run "5@abc" >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then PASS=$((PASS + 1)); echo "ok   short pin refused"; else FAIL=$((FAIL + 1)); echo "FAIL short pin accepted"; fi
ln -s "$SCRATCH" "$SCRATCH/link"; rc=0
RELEASE_AUTO_MERGE_STATE="$SCRATCH/link" PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" --dry-run 5 >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then PASS=$((PASS + 1)); echo "ok   symlink state refused"; else FAIL=$((FAIL + 1)); echo "FAIL symlink state accepted"; fi

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]
