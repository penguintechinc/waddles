#!/usr/bin/env bash
# Self-test for scripts/release-merge.sh: a fake `gh` on PATH serves canned PR JSON; no network.
# Covers every exit code (0 merged, 1 failed, 2 head moved, 3 timeout, 4 non-release base), the
# submit-queue ignore, pending blocking the merge, dry-run, and the gh retry path.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../release-merge.sh"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/release-merge-test.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/bin"

# gh shim. State in $D: pr.json (served), pr2.json (served once headRefOid was read $SWITCH_AFTER times),
# fails_left (calls that fail before succeeding), merge.log (merge argv), calls (every invocation).
cat > "$SCRATCH/bin/gh" <<'SHIM'
#!/usr/bin/env bash
echo "$*" >> "$D/calls"
if [ -f "$D/fails_left" ] && [ "$(cat "$D/fails_left")" -gt 0 ]; then
  echo $(($(cat "$D/fails_left") - 1)) > "$D/fails_left"
  echo "dial tcp: i/o timeout" >&2; exit 1
fi
if [ "$1" = "repo" ] && [ "$2" = "view" ]; then echo "acme/widgets"; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  echo "$*" > "$D/merge.log"; exit "${MERGE_RC:-0}"
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  file="$D/pr.json"
  if [[ "$*" == *headRefOid* ]]; then
    n=0; [ -f "$D/hn" ] && n="$(cat "$D/hn")"; n=$((n + 1)); echo "$n" > "$D/hn"
    if [ -n "${SWITCH_AFTER:-}" ] && [ "$n" -gt "$SWITCH_AFTER" ] && [ -f "$D/pr2.json" ]; then file="$D/pr2.json"; fi
  fi
  jqexpr=""; prev=""
  for a in "$@"; do [ "$prev" = "--jq" ] && jqexpr="$a"; prev="$a"; done
  if [ -n "$jqexpr" ]; then jq -r "$jqexpr" "$file"; else cat "$file"; fi
  exit 0
fi
echo "gh shim: unexpected call: $*" >&2; exit 99
SHIM
chmod +x "$SCRATCH/bin/gh"

PASS=0; FAIL=0
H="abcdef1234567890abcdef1234567890abcdef12"
H2="ffffffffffffffffffffffffffffffffffffffff"
D="$SCRATCH/d"
ck() { printf '{"__typename":"CheckRun","name":"%s","status":"%s","conclusion":%s}' "$1" "$2" "$3"; }
OK_GATE="$(ck ci-gate COMPLETED '"SUCCESS"')"
OK_BUILD="$(ck build COMPLETED '"SUCCESS"')"
SQ_FAIL='{"__typename":"StatusContext","context":"submit-queue","state":"FAILURE"}'

# mk <base> <head> <checks-json-array>: reset the scratch state and write pr.json.
mk() {
  rm -rf "$D"; mkdir -p "$D"
  printf '{"baseRefName":"%s","state":"OPEN","headRefOid":"%s","mergeCommit":{"oid":"mmm111"},"statusCheckRollup":%s}\n' \
    "$1" "$2" "$3" > "$D/pr.json"
}

# expect <name> <want-exit> <want-msg-regex> [script args...]; extra env via ENVX (space-separated K=V).
ENVX=""
expect() {
  local name="$1" want="$2" rx="$3" out rc=0
  shift 3
  # shellcheck disable=SC2086
  out="$(env D="$D" REPO=acme/widgets POLL_SECONDS=0 GH_RETRY_SLEEP=0 $ENVX PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" "$@" 2>&1)" || rc=$?
  if [ "$rc" -eq "$want" ] && printf '%s' "$out" | tr '\n' ' ' | grep -Eq -e "$rx"; then
    PASS=$((PASS + 1)); echo "ok   $name"
  else
    FAIL=$((FAIL + 1)); echo "FAIL $name (rc=$rc want=$want rx=$rx)"; printf '%s\n' "$out" | sed 's/^/     /'
  fi
  ENVX=""
}
# no_call <name> <regex>: the gh call log must NOT contain the pattern.
no_call() {
  if [ -f "$D/calls" ] && grep -Eq -e "$2" "$D/calls"; then FAIL=$((FAIL + 1)); echo "FAIL $1"; else PASS=$((PASS + 1)); echo "ok   $1"; fi
}
has_call() {
  if [ -f "$D/calls" ] && grep -Eq -e "$2" "$D/calls"; then PASS=$((PASS + 1)); echo "ok   $1"; else FAIL=$((FAIL + 1)); echo "FAIL $1"; fi
}

# exit 0: merged, pinned to head, no --admin
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
expect "green merges (exit 0)" 0 "MERGED: mmm111" 7 "$H" 1
has_call "merge pinned with --match-head-commit" "pr merge 7 -R acme/widgets --merge --match-head-commit $H"
no_call "never uses --admin" "--admin"
no_call "never uses --auto" "--auto"

# exit 1: failed check named, no merge
mk release/v3.0.X "$H" "[$OK_GATE,$(ck unit-tests COMPLETED '"FAILURE"')]"
expect "failed check (exit 1)" 1 "FAIL: unit-tests" 7 "$H" 1
no_call "failed check does not merge" "pr merge"

# exit 1: ci-gate itself failed
mk release/v3.0.X "$H" "[$(ck ci-gate COMPLETED '"FAILURE"'),$OK_BUILD]"
expect "ci-gate failed (exit 1)" 1 "FAIL: ci-gate" 7 "$H" 1

# failure wins even while others are pending
mk release/v3.0.X "$H" "[$(ck slow IN_PROGRESS null),$(ck unit-tests COMPLETED '"FAILURE"')]"
expect "fail + pending -> fail fast" 1 "FAIL: unit-tests" 7 "$H" 1

# pending blocks the merge -> timeout (exit 3), never merges
mk release/v3.0.X "$H" "[$OK_GATE,$(ck slow IN_PROGRESS null)]"
expect "pending blocks merge: timeout (exit 3)" 3 "pending=1.*TIMEOUT after 1 polls" 7 "$H" 1
no_call "pending never merges" "pr merge"

# ci-gate missing / still pending also blocks
mk release/v3.0.X "$H" "[$OK_BUILD]"
expect "ci-gate absent blocks (exit 3)" 3 "ci-gate=missing" 7 "$H" 1
no_call "ci-gate absent never merges" "pr merge"
mk release/v3.0.X "$H" "[$(ck ci-gate IN_PROGRESS null),$OK_BUILD]"
expect "ci-gate pending blocks (exit 3)" 3 "ci-gate=pending" 7 "$H" 1

# no checks yet -> timeout
mk release/v3.0.X "$H" "[]"
expect "no checks yet: timeout (exit 3)" 3 "no checks yet" 7 "$H" 1

# exit 2: head already differs from the pin
mk release/v3.0.X "$H2" "[$OK_GATE,$OK_BUILD]"
expect "head differs from pin (exit 2)" 2 "ABORT: head moved to $H2" 7 "$H" 1
no_call "moved head never merges" "pr merge"

# exit 2: head moves between polls
mk release/v3.0.X "$H" "[$OK_GATE,$(ck slow IN_PROGRESS null)]"
printf '{"baseRefName":"release/v3.0.X","state":"OPEN","headRefOid":"%s","statusCheckRollup":[]}\n' "$H2" > "$D/pr2.json"
ENVX="SWITCH_AFTER=2"
expect "head moves mid-wait (exit 2)" 2 "ABORT: head moved" 7 "$H" 5
no_call "mid-wait move never merges" "pr merge"

# exit 2: merge refused by GitHub because the head moved
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
printf '{"baseRefName":"release/v3.0.X","state":"OPEN","headRefOid":"%s","statusCheckRollup":[]}\n' "$H2" > "$D/pr2.json"
ENVX="MERGE_RC=1 SWITCH_AFTER=1"
expect "merge refused, head moved (exit 2)" 2 "ABORT: head moved" 7 "$H" 5

# merge fails for another reason -> exit 1
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
ENVX="MERGE_RC=1"
expect "merge failure (exit 1)" 1 "merge failed" 7 "$H" 1

# submit-queue ignored by default (failing or pending) but a real check still merges
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD,$SQ_FAIL]"
expect "submit-queue failure ignored" 0 "MERGED" 7 "$H" 1
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD,{\"__typename\":\"StatusContext\",\"context\":\"submit-queue\",\"state\":\"PENDING\"}]"
expect "submit-queue pending ignored" 0 "MERGED" 7 "$H" 1
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD,$SQ_FAIL]"
ENVX="IGNORE_CHECKS=none"
expect "IGNORE_CHECKS override: submit-queue counts" 1 "FAIL: submit-queue" 7 "$H" 1

# skipped checks neither block nor count as pass
mk release/v3.0.X "$H" "[$OK_GATE,$(ck e2e COMPLETED '"SKIPPED"')]"
expect "skipped check ignored" 0 "MERGED" 7 "$H" 1

# exit 4: non-release base refused, no merge
mk main "$H" "[$OK_GATE,$OK_BUILD]"
expect "base main refused (exit 4)" 4 "not release/\*" 7 "$H" 1
no_call "main base never merges" "pr merge"
mk develop "$H" "[$OK_GATE,$OK_BUILD]"
expect "base develop refused (exit 4)" 4 "not release/\*" 7 "$H" 1

# usage errors (exit 4)
mk release/v3.0.X "$H" "[$OK_GATE]"
expect "short sha rejected (exit 4)" 4 "full 40-hex" 7 abc1234 1
expect "no args rejected (exit 4)" 4 "usage:"
expect "bad flag rejected (exit 4)" 4 "unknown flag" 7 --admin

# sha omitted: pinned once from the current head
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
expect "sha omitted pins current head" 0 "pinning to current head $H" 7
has_call "omitted sha merge uses read head" "match-head-commit $H"

# dry-run: reports counts, never merges
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
expect "dry-run green reports plan" 0 "DRY-RUN: green; would run: gh pr merge 7 .*--match-head-commit $H" 7 "$H" --dry-run
no_call "dry-run green never merges" "pr merge"
mk release/v3.0.X "$H" "[$OK_GATE,$(ck slow IN_PROGRESS null)]"
expect "dry-run pending reports counts" 0 "total=2 pass=1 pending=1 fail=0.*not green yet" 7 "$H" --dry-run
no_call "dry-run pending never merges" "pr merge"
mk release/v3.0.X "$H" "[$OK_GATE,$(ck unit-tests COMPLETED '"FAILURE"')]"
expect "dry-run failed exits 1" 1 "FAIL: unit-tests" 7 "$H" --dry-run

# gh retry path: transient failures are retried; exhausted retries fail loudly
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
printf '2' > "$D/fails_left"
expect "transient gh failures retried" 0 "gh retry 2/5.*MERGED" 7 "$H" 1
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
printf '99' > "$D/fails_left"
ENVX="GH_RETRIES=3"
expect "gh failing forever is loud (nonzero)" 1 "gh failed 3x" 7 "$H" 1
no_call "retry exhaustion never merges" "pr merge"

# repo derived from `gh repo view` when REPO is unset
mk release/v3.0.X "$H" "[$OK_GATE,$OK_BUILD]"
out="$(env -u REPO D="$D" POLL_SECONDS=0 PATH="$SCRATCH/bin:$PATH" bash "$SCRIPT" 7 "$H" 1 2>&1)" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && grep -q "pr merge 7 -R acme/widgets" "$D/calls"; then PASS=$((PASS + 1)); echo "ok   repo from gh repo view"; else FAIL=$((FAIL + 1)); echo "FAIL repo from gh repo view (rc=$rc)"; printf '%s\n' "$out"; fi

echo "release-merge self-test: pass=$PASS fail=$FAIL (cases=$((PASS + FAIL)))"
[ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]
