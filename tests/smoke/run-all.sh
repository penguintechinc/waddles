#!/bin/bash
# Waddles Master Smoke Test Runner
# Runs all smoke tests in sequence
#
# Usage: ./tests/smoke/run-all.sh [environment]
#   environment: local (default) | beta
#
# Exit codes:
#   0 - All tests passed
#   1 - One or more tests failed

set -e

ENV="${1:-local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "========================================"
echo "Waddles Master Smoke Test Suite"
echo "========================================"
echo ""
echo "Environment: $ENV"
echo "Project Root: $PROJECT_ROOT"
echo ""

TESTS_PASSED=0
TESTS_FAILED=0

# Helper function to run a test
run_test() {
    local test_name="$1"
    local test_command="$2"

    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}Running: $test_name${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    if eval "$test_command"; then
        echo ""
        echo -e "${GREEN}✓ $test_name PASSED${NC}"
        ((TESTS_PASSED++))
        return 0
    else
        echo ""
        echo -e "${RED}✗ $test_name FAILED${NC}"
        ((TESTS_FAILED++))
        return 1
    fi
}

# === Phase 1: File-based validation tests (no services required) ===

# Branding consistency (always runs first - catches fundamental issues)
run_test "Branding Smoke Test" \
    "cd '$SCRIPT_DIR' && bash smoke-branding.sh"

# === Phase 2: Service-dependent tests ===

# Run tests based on environment
if [ "$ENV" = "local" ]; then
    # Local environment tests
    run_test "Alpha Smoke Test (Container Health)" \
        "cd '$PROJECT_ROOT' && bash tests/alpha-smoke-test.sh"

    run_test "API Smoke Test (Comprehensive)" \
        "cd '$SCRIPT_DIR' && bash smoke-api-comprehensive.sh http://localhost:8060"

    # Install playwright if needed
    if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
        echo "Installing Playwright..."
        cd "$SCRIPT_DIR" && npm install --silent
    fi

    run_test "Page Load Smoke Test" \
        "cd '$SCRIPT_DIR' && BASE_URL=http://localhost:3000 node smoke-pages.js"

elif [ "$ENV" = "beta" ]; then
    # Beta environment tests
    run_test "Beta API Smoke Test (Basic)" \
        "cd '$PROJECT_ROOT' && bash tests/beta-smoke-test.sh"

    run_test "Beta API Smoke Test (Comprehensive)" \
        "cd '$SCRIPT_DIR' && bash smoke-api-comprehensive.sh https://waddlebot.penguintech.io"

    # Install playwright if needed
    if [ ! -d "$SCRIPT_DIR/node_modules" ]; then
        echo "Installing Playwright..."
        cd "$SCRIPT_DIR" && npm install --silent
    fi

    run_test "Beta Page Load Smoke Test" \
        "cd '$SCRIPT_DIR' && BASE_URL=https://waddlebot.penguintech.io node smoke-pages.js"

else
    echo -e "${RED}Unknown environment: $ENV${NC}"
    echo "Usage: $0 [local|beta]"
    exit 1
fi

# Summary
echo ""
echo "========================================"
echo "Master Smoke Test Summary"
echo "========================================"
echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
echo ""

if [ "$TESTS_FAILED" -gt 0 ]; then
    echo -e "${RED}SMOKE TESTS FAILED${NC}"
    echo "Fix failures before proceeding"
    exit 1
fi

echo -e "${GREEN}ALL SMOKE TESTS PASSED${NC}"
echo "System is healthy and ready!"
exit 0
