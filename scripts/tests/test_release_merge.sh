#!/usr/bin/env bash
# Self-test for scripts/release-merge.sh: stubs `gh` via a PATH shim with canned JSON.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../release-merge.sh"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/release-merge-test.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/bin"

# gh shim: `gh pr view` prints $FIXTURE; any other subcommand fails loudly (dry-run must never merge).
cat > "$SCRATCH/bin/gh" <<'SHIM'
#!/usr/bin/env bash
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n=0; [ -f "$FIXTURE.n" ] && n="$(cat "$FIXTURE.n")"
  n=$((n + 1)); echo "$n" > "$FIXTURE.n"
  if [ "$n" -ge 2 ] && [ -f "$FIXTURE2" ]; then cat "$FIXTURE2"; else cat "$FIXTURE"; fi
  exit 0
fi
echo "gh shim: unexpected call: $*" >&2; exit 99
SHIM
chmod +x "$SCRATCH/bin/gh"

PASS=0; FAIL=0
H="abcdef1234567890abcdef1234567890abcdef12"
GATE='{"__typename":"CheckRun","name":"ci-gate","status":"COMPLETED","conclusion":"SUCCESS"}'
OKCHK="$GATE,"'{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}'

# mk <base> <state> <mergeable> <checks-json-array>
mk() {
  rm -f "$SCRATCH/f.json.n" "$SCRATCH/f2.json"
  printf '{"baseRefName":"%s","headRefOid":"%s","mergeable":"%s","mergeStateStatus":"CLEAN","state":"%s","statusCheckRollup":%s}\n' \
    "$1" "$H" "$3" "$2" "$4" > "$SCRATCH/f.json"
}

# expect <name> <want-exit 0|1> <want-msg-regex> [script args...]
expect() {
  local name="$1" want="$2" rx="$3" out rc=0
  shift 3
  out="$(FIXTURE="$SCRATCH/f.json" FIXTURE2="$SCRATCH/f2.json" RELEASE_MERGE_SNAPSHOT_DELAY=0 PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" "$@" 2>&1)" || rc=$?
  if [ "$rc" -eq "$want" ] && printf '%s' "$out" | grep -Eq "$rx"; then
    PASS=$((PASS + 1)); echo "ok   $name"
  else
    FAIL=$((FAIL + 1)); echo "FAIL $name (rc=$rc want=$want rx=$rx)"; printf '%s\n' "$out" | sed 's/^/     /'
  fi
}

mk main OPEN MERGEABLE "[$OKCHK]"
expect "base main refused" 1 "not release/\*" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[$OKCHK,{\"__typename\":\"CheckRun\",\"name\":\"unit-tests\",\"status\":\"COMPLETED\",\"conclusion\":\"FAILURE\"}]"
expect "failing check named" 1 "unit-tests\(FAILURE\)" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[$OKCHK,{\"__typename\":\"CheckRun\",\"name\":\"slow\",\"status\":\"IN_PROGRESS\",\"conclusion\":null}]"
expect "pending check named" 1 "slow\(PENDING\)" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[$OKCHK,{\"__typename\":\"StatusContext\",\"context\":\"submit-queue\",\"state\":\"FAILURE\"}]"
expect "submit-queue-only failure passes dry-run" 0 "DRY-RUN OK" 1 --dry-run

mk release/v3.0.X OPEN CONFLICTING "[$OKCHK]"
expect "conflicting refused" 1 "CONFLICTING" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[]"
expect "zero checks refused" 1 "zero checks" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[{\"__typename\":\"StatusContext\",\"context\":\"submit-queue\",\"state\":\"SUCCESS\"}]"
expect "only-ignored context = zero examined" 1 "zero checks" 1 --dry-run

mk release/v3.0.X OPEN MERGEABLE "[$OKCHK]"
expect "head mismatch refused" 1 "does not start with" 1 --dry-run --expect-head deadbee

mk release/v3.0.X OPEN MERGEABLE "[$OKCHK]"
expect "head prefix match passes" 0 "DRY-RUN OK" 1 --dry-run --expect-head abcdef1

mk release/v3.0.X MERGED MERGEABLE "[$OKCHK]"
expect "non-OPEN refused" 1 "not OPEN" 1 --dry-run

# ci-gate absent
mk release/v3.0.X OPEN MERGEABLE '[{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}]'
expect "ci-gate absent refused" 1 "ci-gate\(absent\)" 1 --dry-run

# ci-gate failed / pending
mk release/v3.0.X OPEN MERGEABLE '[{"__typename":"CheckRun","name":"ci-gate","status":"COMPLETED","conclusion":"FAILURE"},{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}]'
expect "ci-gate failed refused" 1 "ci-gate\(not-SUCCESS\)" 1 --dry-run
mk release/v3.0.X OPEN MERGEABLE '[{"__typename":"CheckRun","name":"ci-gate","status":"IN_PROGRESS","conclusion":null},{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}]'
expect "ci-gate pending refused" 1 "ci-gate\(not-SUCCESS\)" 1 --dry-run

# all skipped
mk release/v3.0.X OPEN MERGEABLE '[{"__typename":"CheckRun","name":"ci-gate","status":"COMPLETED","conclusion":"SKIPPED"},{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SKIPPED"}]'
expect "all-skipped refused" 1 "zero checks examined" 1 --dry-run

# snapshot mismatch (check set changes between snapshots)
mk release/v3.0.X OPEN MERGEABLE "[$OKCHK]"
printf '{"baseRefName":"release/v3.0.X","headRefOid":"%s","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","state":"OPEN","statusCheckRollup":[%s,{"__typename":"CheckRun","name":"extra","status":"COMPLETED","conclusion":"SUCCESS"}]}\n' "$H" "$OKCHK" > "$SCRATCH/f2.json"
expect "snapshot mismatch refused" 1 "check set changed" 1 --dry-run

# head moved between snapshots
mk release/v3.0.X OPEN MERGEABLE "[$OKCHK]"
printf '{"baseRefName":"release/v3.0.X","headRefOid":"ffffffffffffffffffffffffffffffffffffffff","mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","state":"OPEN","statusCheckRollup":[%s]}\n' "$OKCHK" > "$SCRATCH/f2.json"
expect "head moved refused" 1 "head moved" 1 --dry-run

# merge command pins head
mk release/v3.0.X OPEN MERGEABLE "[$OKCHK]"
expect "merge pins --match-head-commit" 0 "match-head-commit abcdef123456" 1 --dry-run

echo "release-merge self-test: pass=$PASS fail=$FAIL (cases=$((PASS + FAIL)))"
[ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]
