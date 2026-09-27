#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
export PROJECT_NAME="$(basename "$REPO_ROOT")"
export NAMESPACE="${PROJECT_NAME}-alpha"
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $*"; }
log_info "Running unit tests"
cd "$REPO_ROOT"

# Tests import repository packages directly. hub_api, libs/flask_core, and
# every core/svc_* container each carry their own hash-pinned
# requirements.txt and are NOT satisfied by the legacy identity_core_module
# requirements alone -- scripts/ci/install-unit-test-deps.sh installs every
# one of them, sequentially, before pytest ever runs (see that script for
# why a single combined install call doesn't work). Skip with
# SKIP_UNIT_TEST_DEPS_INSTALL=1 if the caller has already provisioned the
# environment (e.g. a container image built with these deps baked in).
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/core:$REPO_ROOT/libs/flask_core${PYTHONPATH:+:$PYTHONPATH}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Required Python executable not found: $PYTHON_BIN" >&2
    exit 127
}

if [ "${SKIP_UNIT_TEST_DEPS_INSTALL:-0}" != "1" ]; then
    log_info "Installing unit test dependencies (per-package, see scripts/ci/install-unit-test-deps.sh)"
    PYTHON_BIN="$PYTHON_BIN" "$REPO_ROOT/scripts/ci/install-unit-test-deps.sh"
fi

TOTAL_PASSED=0
run_suite() {
    local label="$1"
    shift
    log_info "Suite: $label"
    local output status
    set +e
    output="$("$@" 2>&1)"
    status=$?
    set -e
    echo "$output"
    if [ "$status" -ne 0 ]; then
        log_fail "Suite failed: $label"
        exit "$status"
    fi
    # Extract "<N> passed" from pytest's summary line; a suite that collects
    # zero tests (moved path, empty testpaths, etc.) must not report as green --
    # `grep -q . ` on an empty count would silently pass, so require a match.
    local passed
    passed="$(printf '%s\n' "$output" | grep -oE '[0-9]+ passed' | tail -n1 | grep -oE '[0-9]+' || true)"
    if [ -z "$passed" ]; then
        log_fail "Suite reported no parseable 'N passed' summary: $label"
        exit 1
    fi
    TOTAL_PASSED=$((TOTAL_PASSED + passed))
}

# --- Legacy suite (pre-v3 flask_core module tests + identity gRPC handler) ---
run_suite "legacy tests/unit + identity_core_module" \
    "$PYTHON_BIN" -m pytest tests/unit core/identity_core_module/services/test_grpc_handler.py

# --- workflow_core_module (own tests/ dir, own conftest.py) ---
run_suite "core/workflow_core_module" \
    env -C "$REPO_ROOT/core/workflow_core_module" "$PYTHON_BIN" -m pytest

# --- hub_api (v3 control-plane REST API) ---
run_suite "hub_api" \
    env -C "$REPO_ROOT/hub_api" "$PYTHON_BIN" -m pytest

# --- libs/* (flask_core spine + SCCEMBS module libraries) ---
for lib_dir in "$REPO_ROOT"/libs/*/; do
    lib_name="$(basename "$lib_dir")"
    [ -d "${lib_dir}tests" ] || continue
    run_suite "libs/${lib_name}" \
        env -C "$lib_dir" "$PYTHON_BIN" -m pytest
done

# --- core/svc_* (async stage-runner containers: ingest/process/action/presentation/streaming) ---
for svc_dir in "$REPO_ROOT"/core/svc_*/; do
    svc_name="$(basename "$svc_dir")"
    [ -d "${svc_dir}tests" ] || continue
    run_suite "core/${svc_name}" \
        env -C "$svc_dir" "$PYTHON_BIN" -m pytest
done

log_pass "Unit tests step completed -- ${TOTAL_PASSED} passed across all suites"
