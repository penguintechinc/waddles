#!/usr/bin/env bash
# Self-test for ci-gate-fetch.sh (bounded retry + array validation).
# regression: ci-gate HTTP 502 -> jq "array and object cannot be added"
# (PR #1469, run 44789521330). Uses a fake `gh` via GH_BIN; no network.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
FETCH="$REPO_ROOT/scripts/ci/ci-gate-fetch.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export CI_GATE_BACKOFF_BASE=0 CI_GATE_BACKOFF_MAX=0

pass=0; fail=0
ok()  { echo "  ok   $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL $1"; fail=$((fail + 1)); }

# Fake gh: fails (HTTP $FAKE_ERR) for the first $FAKE_FAILS calls, then prints
# $FAKE_BODY. Call count lives in $FAKE_COUNT_FILE.
cat >"$tmp/gh" <<'GH'
#!/usr/bin/env bash
n=$(cat "$FAKE_COUNT_FILE" 2>/dev/null || echo 0)
n=$((n + 1)); echo "$n" >"$FAKE_COUNT_FILE"
if [ "$n" -le "${FAKE_FAILS:-0}" ]; then
  echo "gh: ${FAKE_ERR:-Server Error (HTTP 502)}" >&2
  echo '{"message":"Server Error"}'
  exit 1
fi
printf '%s' "$FAKE_BODY"
GH
chmod +x "$tmp/gh"
export GH_BIN="$tmp/gh" FAKE_COUNT_FILE="$tmp/count"
page1='[{"name":"a","status":"completed","conclusion":"success"}]'
page2='[{"name":"b","status":"completed","conclusion":"success"}]'

run() { rm -f "$FAKE_COUNT_FILE"; "$FETCH" "repos/x/y/commits/z/check-runs" '[.]' 2>"$tmp/err"; }

echo "Test 1: 502 then success -> passes, merged array, 2 calls"
out=$(FAKE_FAILS=1 FAKE_BODY="${page1}${page2}" run) || true
if [ "$(jq 'length' <<<"$out")" = "2" ]; then ok "merged 2 items"; else bad "bad output: $out"; fi
if [ "$(cat "$FAKE_COUNT_FILE")" = "2" ]; then ok "retried exactly once"; else bad "calls=$(cat "$FAKE_COUNT_FILE")"; fi

echo "Test 2: persistent 502 -> fails loudly after 5 attempts"
rc=0; FAKE_FAILS=99 FAKE_BODY="$page1" run >"$tmp/out" || rc=$?
if [ "$rc" -ne 0 ]; then ok "non-zero exit"; else bad "exited 0"; fi
if grep -q "GitHub API unavailable after 5 attempts" "$tmp/err"; then ok "loud message"; else bad "no message: $(cat "$tmp/err")"; fi
if [ "$(cat "$FAKE_COUNT_FILE")" = "5" ]; then ok "5 attempts"; else bad "calls=$(cat "$FAKE_COUNT_FILE")"; fi
if [ ! -s "$tmp/out" ]; then ok "no stdout emitted"; else bad "stdout leaked"; fi

echo "Test 3: 4xx is not retried"
rc=0; FAKE_FAILS=99 FAKE_ERR="Not Found (HTTP 404)" FAKE_BODY="$page1" run >/dev/null || rc=$?
if [ "$rc" -ne 0 ] && [ "$(cat "$FAKE_COUNT_FILE")" = "1" ]; then ok "failed fast, 1 call"; else bad "rc=$rc calls=$(cat "$FAKE_COUNT_FILE")"; fi

echo "Test 4: exit 0 but non-array body -> retried then fails loudly"
rc=0; FAKE_FAILS=0 FAKE_BODY='{"message":"oops"}' run >/dev/null || rc=$?
if [ "$rc" -ne 0 ] && grep -q "unavailable after 5 attempts" "$tmp/err"; then ok "non-array rejected"; else bad "rc=$rc"; fi

echo "Test 5: empty result -> []"
out=$(FAKE_FAILS=0 FAKE_BODY='' run) || true
if [ "$out" = "[]" ]; then ok "empty -> []"; else bad "got: $out"; fi

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
