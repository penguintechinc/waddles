#!/bin/bash
#
# WaddleBot Hub WebUI Load Test
# Tests that all frontend pages load without errors
#

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
BASE_URL="${HUB_URL:-http://localhost:8080}"
TIMEOUT=10

# Counters
PASSED=0
FAILED=0

echo ""
echo "WaddleBot Hub WebUI Load Test"
echo "=============================="
echo "Base URL: $BASE_URL"
echo ""

# Function to test a page
test_page() {
    local name="$1"
    local path="$2"
    local expected="${3:-200}"

    printf "  %-40s" "$name..."

    response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout $TIMEOUT "${BASE_URL}${path}" 2>/dev/null || echo "000")

    if [ "$response" = "$expected" ]; then
        echo -e "${GREEN}OK${NC} ($response)"
        ((PASSED++))
    elif [ "$response" = "000" ]; then
        echo -e "${YELLOW}UNREACHABLE${NC}"
        ((FAILED++))
    else
        echo -e "${RED}FAIL${NC} (got $response, expected $expected)"
        ((FAILED++))
    fi
}

# Test public pages (no auth required - SPA routes return 200 with index.html)
echo "Frontend Pages (SPA):"
test_page "Home / Login" "/"
test_page "Login Page" "/login"
test_page "Register Page" "/register"
test_page "Cookie Policy" "/cookie-policy"
test_page "Dashboard" "/dashboard"
test_page "Communities" "/communities"

echo ""
echo "Static Assets:"
test_page "Favicon" "/waddle.svg"

echo ""
echo "API Endpoints:"
test_page "Auth Status" "/api/v1/auth/me"
test_page "Communities (auth required)" "/api/v1/communities" 401

# Summary
echo ""
echo "=============================="
TOTAL=$((PASSED + FAILED))
echo "Results: $PASSED/$TOTAL passed"

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All pages loaded successfully!${NC}"
    exit 0
else
    echo -e "${RED}$FAILED page(s) failed to load${NC}"
    exit 1
fi
