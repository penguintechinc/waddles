#!/usr/bin/env bash
# Proves scripts/check-doc-refs.sh can FAIL: plants a dead backticked path in a
# scratch doc, asserts the gate goes red and reports it, removes it, then
# asserts the tree is green again. (critical-rules.md Verification Integrity.)
set -euo pipefail
cd "$(dirname "$0")/../../.."

plant="docs/zz_planted_dead_ref_test.md"
trap 'rm -f "$plant"' EXIT

bash scripts/check-doc-refs.sh >/dev/null || { echo "FAIL: baseline gate not green" >&2; exit 1; }

printf 'Planted: `services/does-not-exist-planted`\n' > "$plant"
out=$(bash scripts/check-doc-refs.sh 2>&1) && { echo "FAIL: gate stayed green on a planted dead link" >&2; exit 1; }
printf '%s\n' "$out" | grep -q 'dead: services/does-not-exist-planted' \
  || { echo "FAIL: gate failed but did not name the planted ref" >&2; exit 1; }

rm -f "$plant"
bash scripts/check-doc-refs.sh >/dev/null || { echo "FAIL: gate not green after removing plant" >&2; exit 1; }
echo "PASS: planted dead link failed the gate (examined baseline + 2 re-runs)"
