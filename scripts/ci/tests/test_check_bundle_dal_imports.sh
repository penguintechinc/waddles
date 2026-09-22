#!/usr/bin/env bash
# Self-test for check_bundle_dal_imports.sh gate script
# Tests three scenarios: clean tree, missing directory, and seeded violation
set -euo pipefail

# Get the repo root (where check_bundle_dal_imports.sh is located)
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
GATE_SCRIPT="$REPO_ROOT/scripts/ci/check_bundle_dal_imports.sh"

# Track test results
cases_passed=0
cases_failed=0
temp_dirs=()

# Setup: create a cleanup trap
cleanup_temp_dirs() {
    [ ${#temp_dirs[@]} -eq 0 ] || for tmpdir in "${temp_dirs[@]}"; do
        if [ -d "$tmpdir" ]; then
            rm -rf "$tmpdir"
        fi
    done
}
trap cleanup_temp_dirs EXIT

echo "Running test_check_bundle_dal_imports.sh..."

# Test Case 1: Clean tree (exit 0, files_scanned > 0)
echo ""
echo "Test 1: Clean tree should exit 0 with files_scanned > 0"

sandbox1=$(mktemp -d)
temp_dirs+=("$sandbox1")

# Copy real bundle directories to sandbox
mkdir -p "$sandbox1/core/svc_process/bundles"
mkdir -p "$sandbox1/core/svc_action/bundles"
mkdir -p "$sandbox1/core/svc_ingest/bundles"

cp -r "$REPO_ROOT/core/svc_process/bundles/"*.py "$sandbox1/core/svc_process/bundles/"
cp -r "$REPO_ROOT/core/svc_action/bundles/"*.py "$sandbox1/core/svc_action/bundles/"
cp -r "$REPO_ROOT/core/svc_ingest/bundles/"*.py "$sandbox1/core/svc_ingest/bundles/"

# Verify files were copied
files_copied=$(find "$sandbox1/core" -maxdepth 3 -name '*.py' | wc -l)
if [ "$files_copied" -eq 0 ]; then
    echo "  ✗ No files copied to sandbox (copy failed)"
    cases_failed=$((cases_failed + 1))
    exit 1
fi

# Run gate from sandbox
cd "$sandbox1"
output1=$(bash "$GATE_SCRIPT" 2>&1) && exit_code1=0 || exit_code1=$?
cd "$REPO_ROOT"

# Check exit code
if [ "$exit_code1" -eq 0 ]; then
    echo "  ✓ Exit code is 0"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Exit code is $exit_code1 (expected 0)"
    cases_failed=$((cases_failed + 1))
    echo "    Output: $output1"
fi

# Check that files_scanned is in output and > 0
if echo "$output1" | grep -q "files_scanned="; then
    files_scanned=$(echo "$output1" | grep -oP 'files_scanned=\K[0-9]+' | head -1)
    if [ "$files_scanned" -gt 0 ]; then
        echo "  ✓ files_scanned=$files_scanned (> 0)"
        cases_passed=$((cases_passed + 1))
    else
        echo "  ✗ files_scanned=$files_scanned is not > 0"
        cases_failed=$((cases_failed + 1))
    fi
else
    echo "  ✗ Output does not contain files_scanned="
    cases_failed=$((cases_failed + 1))
    echo "    Output: $output1"
fi

# Test Case 2: Missing directory should exit 1 with error message
echo ""
echo "Test 2: Missing directory should exit 1 with error in stderr"

sandbox2=$(mktemp -d)
temp_dirs+=("$sandbox2")

# Create incomplete directory structure (missing svc_process)
mkdir -p "$sandbox2/core/svc_action/bundles"
mkdir -p "$sandbox2/core/svc_ingest/bundles"
touch "$sandbox2/core/svc_action/bundles/dummy.py"
touch "$sandbox2/core/svc_ingest/bundles/dummy.py"

# Run gate from sandbox
cd "$sandbox2"
output2=$(bash "$GATE_SCRIPT" 2>&1) && exit_code2=0 || exit_code2=$?
cd "$REPO_ROOT"

# Check exit code
if [ "$exit_code2" -eq 1 ]; then
    echo "  ✓ Exit code is 1"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Exit code is $exit_code2 (expected 1)"
    cases_failed=$((cases_failed + 1))
fi

# Check that error message names the missing directory
if echo "$output2" | grep -q "expected directory missing: core/svc_process/bundles"; then
    echo "  ✓ Error message names missing directory"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Error message does not name the missing directory"
    cases_failed=$((cases_failed + 1))
    echo "    Output: $output2"
fi

# Test Case 3: Seeded violation should exit 1 with path:line in output
echo ""
echo "Test 3: Seeded violation should exit 1 with path:line in stderr"

sandbox3=$(mktemp -d)
temp_dirs+=("$sandbox3")

# Copy real bundle directories to sandbox
mkdir -p "$sandbox3/core/svc_process/bundles"
mkdir -p "$sandbox3/core/svc_action/bundles"
mkdir -p "$sandbox3/core/svc_ingest/bundles"

cp -r "$REPO_ROOT/core/svc_process/bundles/"*.py "$sandbox3/core/svc_process/bundles/"
cp -r "$REPO_ROOT/core/svc_action/bundles/"*.py "$sandbox3/core/svc_action/bundles/"
cp -r "$REPO_ROOT/core/svc_ingest/bundles/"*.py "$sandbox3/core/svc_ingest/bundles/"

# Verify files were copied
files_copied=$(find "$sandbox3/core" -maxdepth 3 -name '*.py' | wc -l)
if [ "$files_copied" -eq 0 ]; then
    echo "  ✗ No files copied to sandbox (copy failed)"
    cases_failed=$((cases_failed + 1))
    exit 1
fi

# Inject a legacy import into one bundle
echo "" >> "$sandbox3/core/svc_action/bundles/social_quote_action.py"
echo "# Test injection for violation" >> "$sandbox3/core/svc_action/bundles/social_quote_action.py"
echo "from flask_core.database import AsyncDAL  # This should trigger the gate" >> "$sandbox3/core/svc_action/bundles/social_quote_action.py"

# Run gate from sandbox
cd "$sandbox3"
output3=$(bash "$GATE_SCRIPT" 2>&1) && exit_code3=0 || exit_code3=$?
cd "$REPO_ROOT"

# Check exit code
if [ "$exit_code3" -eq 1 ]; then
    echo "  ✓ Exit code is 1"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Exit code is $exit_code3 (expected 1)"
    cases_failed=$((cases_failed + 1))
fi

# Check that output contains the file:line reference
if echo "$output3" | grep -qE "core/svc_action/bundles/social_quote_action\.py:[0-9]+:"; then
    echo "  ✓ Output contains path:line format"
    cases_passed=$((cases_passed + 1))
else
    echo "  ✗ Output does not contain path:line format"
    cases_failed=$((cases_failed + 1))
    echo "    Output: $output3"
fi

# Final Report
echo ""
total_cases=$((cases_passed + cases_failed))
if [ "$cases_failed" -eq 0 ]; then
    echo "✓ Test PASSED: $cases_passed/$total_cases cases passed"
    exit 0
else
    echo "✗ Test FAILED: $cases_passed/$total_cases cases passed, $cases_failed failed"
    exit 1
fi
