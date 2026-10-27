#!/usr/bin/env bash
# Self-test for ci-gate-merge-pages.sh.
# regression: ci-gate --paginate multi-page arrays (#515/#517)
#
# `gh api ... --paginate --jq '[...]'` runs the --jq filter once per page,
# so a commit with >100 check-runs/statuses prints several JSON arrays back
# to back on stdout ("[..][..]"), which `jq -n --argjson` then rejects as
# invalid JSON. ci-gate-merge-pages.sh slurps that stream and flattens it
# into one array; these cases simulate the 2-page concatenated output and
# the empty-result case.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
MERGE_SCRIPT="$REPO_ROOT/scripts/ci/ci-gate-merge-pages.sh"

cases_passed=0
cases_failed=0

echo "Running test_ci_gate_merge_pages.sh..."

# Test Case 1: two concatenated page arrays (100 + 37 items) merge into one
# 137-element array -- this is the exact multi-page shape gh emits for a
# commit with more than 100 check-runs.
echo ""
echo "Test 1: two-page concatenated arrays merge into a single flat array"

page1=$(jq -nc '[range(100) | {name: ("check-" + (. | tostring)), status: "completed", conclusion: "success"}]')
page2=$(jq -nc '[range(37) | {name: ("check-" + ((100 + .) | tostring)), status: "completed", conclusion: "success"}]')

merged=$(printf '%s%s' "$page1" "$page2" | bash "$MERGE_SCRIPT")

if echo "$merged" | jq -e 'type == "array"' >/dev/null 2>&1; then
    echo "  ✓ Output is valid JSON array"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Output is not valid JSON: $merged"
    cases_failed=$((cases_failed + 1))
fi

merged_len=$(echo "$merged" | jq 'length' 2>/dev/null || echo "-1")
if [ "$merged_len" -eq 137 ]; then
    echo "  ✓ Merged length is 137 (100 + 37)"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Merged length is $merged_len (expected 137)"
    cases_failed=$((cases_failed + 1))
fi

# Test Case 2: single-page input (no pagination triggered) still merges fine
echo ""
echo "Test 2: single-page input merges to itself"

single=$(jq -nc '[range(5) | {name: ("check-" + (. | tostring)), status: "completed", conclusion: "success"}]')
merged_single=$(printf '%s' "$single" | bash "$MERGE_SCRIPT")
single_len=$(echo "$merged_single" | jq 'length' 2>/dev/null || echo "-1")

if [ "$single_len" -eq 5 ]; then
    echo "  ✓ Single-page length is 5"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Single-page length is $single_len (expected 5)"
    cases_failed=$((cases_failed + 1))
fi

# Test Case 3: empty result (no pages at all) must still produce []
echo ""
echo "Test 3: empty input produces [] (not null, not an error)"

empty_result=$(printf '' | bash "$MERGE_SCRIPT")
if [ "$empty_result" = "[]" ]; then
    echo "  ✓ Empty input produces []"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Empty input produced '$empty_result' (expected [])"
    cases_failed=$((cases_failed + 1))
fi

# Test Case 4: a single empty-array page (zero check-runs on the commit)
# must also produce [], matching the "zero items examined" contract that
# the ci-gate workflow's own empty-set handling depends on.
echo ""
echo "Test 4: single empty-array page produces []"

empty_page_result=$(printf '[]' | bash "$MERGE_SCRIPT")
if [ "$empty_page_result" = "[]" ]; then
    echo "  ✓ Single empty-array page produces []"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Single empty-array page produced '$empty_page_result' (expected [])"
    cases_failed=$((cases_failed + 1))
fi

echo ""
total_cases=$((cases_passed + cases_failed))
if [ "$cases_failed" -eq 0 ]; then
    echo "✓ Test PASSED: $cases_passed/$total_cases cases passed"
    exit 0
else
    echo "✗ Test FAILED: $cases_passed/$total_cases cases passed, $cases_failed failed"
    exit 1
fi
