#!/bin/bash
################################################################################
# Branding Smoke Test for Waddles
#
# Verifies the Waddles rebrand is correct:
# 1. Frontend branding shows "Waddles" (if services running)
# 2. Infrastructure identifiers remain "waddlebot"
# 3. Domain integrity (waddlebot.io, not waddles.io)
# 4. Display names use "Waddles" in user-facing text
#
# Usage: ./tests/smoke/smoke-branding.sh
#
# Exit codes:
#   0 - All smoke tests passed
#   1 - One or more smoke tests failed
################################################################################

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TESTS_PASSED=0
TESTS_FAILED=0

################################################################################
# Helper functions
################################################################################
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

record_pass() {
    TESTS_PASSED=$((TESTS_PASSED + 1))
    print_success "$1"
}

record_fail() {
    TESTS_FAILED=$((TESTS_FAILED + 1))
    print_error "$1"
}

################################################################################
# Test 1: Frontend branding (if services running)
################################################################################
test_frontend_branding() {
    print_section "Test 1: Frontend Branding"

    local hub_url="http://localhost:3000"

    if curl -s --connect-timeout 3 "$hub_url" > /dev/null 2>&1; then
        print_info "Hub frontend is reachable at $hub_url"

        local page_content
        page_content=$(curl -s --connect-timeout 5 "$hub_url")

        # Title should contain "Waddles"
        if echo "$page_content" | grep -qi '<title>.*[Ww]addles.*</title>'; then
            record_pass "Frontend title contains 'Waddles'"
        else
            record_fail "Frontend title does not contain 'Waddles'"
        fi

        # Visible text should NOT contain "WaddleBot" (the old brand)
        if echo "$page_content" | grep -v '<meta\|<link\|<script' | grep -qi 'WaddleBot'; then
            record_fail "Frontend visible text still contains 'WaddleBot' (should be 'Waddles')"
        else
            record_pass "Frontend visible text does not contain 'WaddleBot'"
        fi
    else
        print_warn "Hub frontend not reachable at $hub_url - skipping live branding checks"
        print_info "Start services to enable frontend branding tests"
    fi
}

################################################################################
# Test 2: Infrastructure identifiers (file-based, always runs)
################################################################################
test_infrastructure_identifiers() {
    print_section "Test 2: Infrastructure Identifiers"

    # docker-compose.yml service names should use "waddlebot" (not "waddles")
    local dc_file="${PROJECT_ROOT}/docker-compose.yml"
    if [ -f "$dc_file" ]; then
        if grep -q 'waddlebot' "$dc_file"; then
            record_pass "docker-compose.yml contains 'waddlebot' service references"
        else
            record_fail "docker-compose.yml missing 'waddlebot' service references"
        fi
    else
        record_fail "docker-compose.yml not found"
    fi

    # k8s/helm/waddlebot/values.yaml namespace should be "waddlebot"
    local helm_values="${PROJECT_ROOT}/k8s/helm/waddlebot/values.yaml"
    if [ -f "$helm_values" ]; then
        if grep -q 'waddlebot' "$helm_values"; then
            record_pass "Helm values.yaml contains 'waddlebot' references"
        else
            record_fail "Helm values.yaml missing 'waddlebot' references"
        fi
    else
        record_fail "Helm values.yaml not found at $helm_values"
    fi

    # DB name/user in values.yaml should be "waddlebot"
    if [ -f "$helm_values" ]; then
        if grep -qE '(database|dbname|db_name|user).*waddlebot' "$helm_values"; then
            record_pass "Helm values.yaml DB name/user contains 'waddlebot'"
        else
            print_warn "Could not confirm DB name/user contains 'waddlebot' in values.yaml"
        fi
    fi
}

################################################################################
# Test 3: Domain integrity
################################################################################
test_domain_integrity() {
    print_section "Test 3: Domain Integrity"

    # "waddles.io" should NOT appear in config/ or docker-compose files
    local bad_domain_count=0
    if [ -d "${PROJECT_ROOT}/config" ]; then
        bad_domain_count=$(grep -r 'waddles\.io' "${PROJECT_ROOT}/config/" "${PROJECT_ROOT}/docker-compose"* 2>/dev/null | wc -l)
    else
        bad_domain_count=$(grep -r 'waddles\.io' "${PROJECT_ROOT}/docker-compose"* 2>/dev/null | wc -l)
    fi

    if [ "$bad_domain_count" -eq 0 ]; then
        record_pass "No 'waddles.io' references found in config/docker-compose files"
    else
        record_fail "Found $bad_domain_count references to 'waddles.io' (should be 'waddlebot.io')"
        grep -r 'waddles\.io' "${PROJECT_ROOT}/config/" "${PROJECT_ROOT}/docker-compose"* 2>/dev/null | head -5 || true
    fi

    # "waddlebot.io" should exist somewhere in the project
    local good_domain_count
    good_domain_count=$(grep -r 'waddlebot\.io' "${PROJECT_ROOT}/config/" "${PROJECT_ROOT}/docker-compose"* 2>/dev/null | wc -l)

    if [ "$good_domain_count" -gt 0 ]; then
        record_pass "Found $good_domain_count references to 'waddlebot.io' in config/docker-compose files"
    else
        print_warn "No 'waddlebot.io' references found in config/docker-compose files"
    fi
}

################################################################################
# Test 4: Display name checks
################################################################################
test_display_names() {
    print_section "Test 4: Display Name Checks"

    # Hub frontend index.html title should contain "Waddles"
    local index_html="${PROJECT_ROOT}/admin/hub_module/frontend/index.html"
    if [ -f "$index_html" ]; then
        if grep -qi '<title>.*[Ww]addles.*</title>' "$index_html"; then
            record_pass "Hub frontend index.html title contains 'Waddles'"
        else
            record_fail "Hub frontend index.html title does not contain 'Waddles'"
        fi
    else
        record_fail "Hub frontend index.html not found"
    fi

    # AdminLayout.jsx heading should contain "Waddles"
    local admin_layout="${PROJECT_ROOT}/admin/hub_module/frontend/src/layouts/AdminLayout.jsx"
    if [ -f "$admin_layout" ]; then
        if grep -q 'Waddles' "$admin_layout"; then
            record_pass "AdminLayout.jsx contains 'Waddles' branding"
        else
            record_fail "AdminLayout.jsx does not contain 'Waddles' branding"
        fi
    else
        record_fail "AdminLayout.jsx not found"
    fi
}

################################################################################
# Main execution
################################################################################
main() {
    print_section "Branding Smoke Test Suite"
    echo "Project Root: $PROJECT_ROOT"
    echo ""

    # Run all tests (continue even if some fail)
    test_frontend_branding || true
    test_infrastructure_identifiers || true
    test_domain_integrity || true
    test_display_names || true

    # Display summary
    print_section "Smoke Test Summary"
    echo -e "Passed: ${GREEN}${TESTS_PASSED}${NC}"
    echo -e "Failed: ${RED}${TESTS_FAILED}${NC}"
    echo ""

    if [ "$TESTS_FAILED" -gt 0 ]; then
        print_error "BRANDING SMOKE TESTS FAILED"
        echo "Fix failures before proceeding"
        exit 1
    fi

    print_success "ALL BRANDING SMOKE TESTS PASSED"
    echo "Branding is consistent and correct!"
    exit 0
}

# Run main function
main "$@"
