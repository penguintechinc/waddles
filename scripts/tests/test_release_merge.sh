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
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then cat "$FIXTURE"; exit 0; fi
echo "gh shim: unexpected call: $*" >&2; exit 99
SHIM
chmod +x "$SCRATCH/bin/gh"

PASS=0; FAIL=0
H="abcdef1234567890abcdef1234567890abcdef12"
OKCHK='{"__typename":"CheckRun","name":"build","status":"COMPLETED","conclusion":"SUCCESS"}'

# mk <base> <state> <mergeable> <checks-json-array>
mk() {
  printf '{"baseRefName":"%s","headRefOid":"%s","mergeable":"%s","mergeStateStatus":"CLEAN","state":"%s","statusCheckRollup":%s}\n' \
    "$1" "$H" "$3" "$2" "$4" > "$SCRATCH/f.json"
}

# expect <name> <want-exit 0|1> <want-msg-regex> [script args...]
expect() {
  local name="$1" want="$2" rx="$3" out rc=0
  shift 3
  out="$(FIXTURE="$SCRATCH/f.json" PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" "$@" 2>&1)" || rc=$?
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

echo "release-merge self-test: pass=$PASS fail=$FAIL (cases=$((PASS + FAIL)))"
[ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]
