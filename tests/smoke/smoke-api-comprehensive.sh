#!/bin/bash
# Waddles Comprehensive API Smoke Tests
# Tests all major API endpoints across all route files
#
# Usage: ./tests/smoke/smoke-api-comprehensive.sh [BASE_URL] [ALB_HOST]
#   BASE_URL defaults to http://localhost:8060 for local testing
#   For beta: ./tests/smoke/smoke-api-comprehensive.sh https://waddlebot.penguintech.io dal2.penguintech.io
#   ALB_HOST (optional) - ALB DNS name to bypass Cloudflare
#
# Environment variables:
#   ALB_HOST - ALB DNS name (overrides parameter)
#   VHOST - Virtual host header (extracted from BASE_URL if not set)
#
# Exit codes:
#   0 - All endpoints responding correctly (PASS)
#   1 - One or more endpoints failing (FAIL)

set -e

# Configuration
BASE_URL="${1:-http://localhost:8060}"
ALB_HOST="${ALB_HOST:-${2}}"
TIMEOUT=5
PASSED=0
FAILED=0
WARNINGS=0

# Extract virtual host from BASE_URL for Host header
if [ -z "$VHOST" ]; then
    VHOST=$(echo "$BASE_URL" | sed -E 's#https?://([^/]+).*#\1#')
fi

# Determine if we're using ALB bypass
SSL_FLAG=""
if [ -n "$ALB_HOST" ]; then
    echo "Using ALB bypass: $ALB_HOST with Host header: $VHOST"
    SSL_FLAG="-k"
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "========================================"
echo "Waddles API Smoke Tests (Comprehensive)"
echo "========================================"
echo ""
echo "Target: $BASE_URL"
echo "Timeout: ${TIMEOUT}s per request"
echo ""

# Test function - expects specific status
test_endpoint() {
    local name="$1"
    local method="$2"
    local endpoint="$3"
    local expected_status="$4"
    local data="$5"
    local token="$6"

    local url
    local actual_status
    local headers=(-H "Content-Type: application/json")

    if [ -n "$ALB_HOST" ]; then
        url="https://$ALB_HOST${endpoint}"
        headers+=(-H "Host: $VHOST")
    else
        url="${BASE_URL}${endpoint}"
    fi

    if [ -n "$token" ]; then
        headers+=(-H "Authorization: Bearer $token")
    fi

    if [ "$method" = "GET" ]; then
        actual_status=$(curl $SSL_FLAG -s -o /dev/null -w "%{http_code}" --connect-timeout "$TIMEOUT" "${headers[@]}" "$url" 2>/dev/null || echo "000")
    elif [ "$method" = "POST" ]; then
        actual_status=$(curl $SSL_FLAG -s -o /dev/null -w "%{http_code}" --connect-timeout "$TIMEOUT" \
            -X POST "${headers[@]}" -d "$data" "$url" 2>/dev/null || echo "000")
    fi

    if [ "$actual_status" = "$expected_status" ]; then
        echo -e "  ${GREEN}[PASS]${NC} $name ($method $endpoint) -> $actual_status"
        ((PASSED++))
        return 0
    elif [ "$actual_status" = "000" ]; then
        echo -e "  ${RED}[FAIL]${NC} $name ($method $endpoint) -> Connection failed"
        ((FAILED++))
        return 1
    else
        echo -e "  ${YELLOW}[WARN]${NC} $name ($method $endpoint) -> Expected $expected_status, got $actual_status"
        ((WARNINGS++))
        return 0
    fi
}

# Test function - any success status is OK (not 404/500)
test_exists() {
    local name="$1"
    local method="$2"
    local endpoint="$3"
    local data="$4"
    local token="$5"

    local url
    local actual_status
    local headers=(-H "Content-Type: application/json")

    if [ -n "$ALB_HOST" ]; then
        url="https://$ALB_HOST${endpoint}"
        headers+=(-H "Host: $VHOST")
    else
        url="${BASE_URL}${endpoint}"
    fi

    if [ -n "$token" ]; then
        headers+=(-H "Authorization: Bearer $token")
    fi

    if [ "$method" = "GET" ]; then
        actual_status=$(curl $SSL_FLAG -s -o /dev/null -w "%{http_code}" --connect-timeout "$TIMEOUT" "${headers[@]}" "$url" 2>/dev/null || echo "000")
    elif [ "$method" = "POST" ]; then
        actual_status=$(curl $SSL_FLAG -s -o /dev/null -w "%{http_code}" --connect-timeout "$TIMEOUT" \
            -X POST "${headers[@]}" -d "$data" "$url" 2>/dev/null || echo "000")
    fi

    if [ "$actual_status" = "404" ]; then
        echo -e "  ${RED}[FAIL]${NC} $name ($method $endpoint) -> 404 NOT FOUND"
        ((FAILED++))
        return 1
    elif [ "$actual_status" = "500" ] || [ "$actual_status" = "502" ] || [ "$actual_status" = "503" ]; then
        echo -e "  ${RED}[FAIL]${NC} $name ($method $endpoint) -> Server error $actual_status"
        ((FAILED++))
        return 1
    elif [ "$actual_status" = "000" ]; then
        echo -e "  ${RED}[FAIL]${NC} $name ($method $endpoint) -> Connection failed"
        ((FAILED++))
        return 1
    else
        echo -e "  ${GREEN}[PASS]${NC} $name ($method $endpoint) -> $actual_status"
        ((PASSED++))
        return 0
    fi
}

echo "----------------------------------------"
echo "1. Public API Endpoints (public.js)"
echo "----------------------------------------"
test_endpoint "Health check" "GET" "/health" "200"
test_exists "Signup settings" "GET" "/api/v1/signup-settings"
test_exists "Public stats" "GET" "/api/v1/stats"
test_exists "Public communities" "GET" "/api/v1/communities"

echo ""
echo "----------------------------------------"
echo "2. Auth Endpoints (auth.js)"
echo "----------------------------------------"
test_exists "Login endpoint" "POST" "/api/v1/auth/login" '{"email":"test@test.com","password":"test"}'
test_exists "Register endpoint" "POST" "/api/v1/auth/register" '{"email":"test@test.com","password":"Test123!","username":"test"}'
test_exists "Current user (no auth)" "GET" "/api/v1/auth/me"
test_exists "Logout endpoint" "POST" "/api/v1/auth/logout" '{}'

echo ""
echo "----------------------------------------"
echo "3. Cookie Consent Endpoints (cookieConsent.js)"
echo "----------------------------------------"
test_exists "Cookie policy" "GET" "/api/v1/cookie/policy"
test_exists "Get cookie consent" "GET" "/api/v1/cookie"
test_exists "Update cookie consent" "POST" "/api/v1/cookie" '{"preferences":{"necessary":true,"functional":false,"analytics":false,"marketing":false}}'

echo ""
echo "----------------------------------------"
echo "4. Community Endpoints (community.js)"
echo "----------------------------------------"
test_exists "List communities" "GET" "/api/v1/communities"
test_exists "Create community (no auth)" "POST" "/api/v1/communities" '{"name":"test","display_name":"Test"}'
# Community ids are public UUIDs on the API (never the numeric id): look one up from the list.
if [ -n "$ALB_HOST" ]; then
    COMMUNITY_LIST_URL="https://$ALB_HOST/api/v1/communities"
else
    COMMUNITY_LIST_URL="${BASE_URL}/api/v1/communities"
fi
COMMUNITY_UUID=$(curl $SSL_FLAG -s --connect-timeout "$TIMEOUT" ${ALB_HOST:+-H "Host: $VHOST"} \
    "$COMMUNITY_LIST_URL" 2>/dev/null \
    | grep -oE '"id": ?"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"' \
    | head -1 | grep -oE '[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}' || true)
if [ -n "$COMMUNITY_UUID" ]; then
    test_exists "Community by ID" "GET" "/api/v1/communities/${COMMUNITY_UUID}"
else
    echo -e "  ${RED}[FAIL]${NC} Community by ID -> no community UUID found in GET /api/v1/communities (0 examined)"
    ((FAILED++))
fi

echo ""
echo "----------------------------------------"
echo "5. User Endpoints (user.js)"
echo "----------------------------------------"
test_exists "User profile (no auth)" "GET" "/api/v1/users/1"
test_exists "User communities (no auth)" "GET" "/api/v1/users/1/communities"

echo ""
echo "----------------------------------------"
echo "6. Marketplace Endpoints (marketplace.js)"
echo "----------------------------------------"
test_exists "List modules" "GET" "/api/v1/marketplace/modules"
test_exists "Module by ID" "GET" "/api/v1/marketplace/modules/1"
test_exists "Module categories" "GET" "/api/v1/marketplace/categories"

echo ""
echo "----------------------------------------"
echo "7. Vendor Endpoints (vendor.js)"
echo "----------------------------------------"
test_exists "Vendor submissions (no auth)" "GET" "/api/v1/vendor/submissions"
test_exists "Submit vendor request" "POST" "/api/v1/vendor/submit" '{}'
test_exists "Vendor dashboard (no auth)" "GET" "/api/v1/vendor/dashboard"

echo ""
echo "----------------------------------------"
echo "8. Platform Config Endpoints (platform.js)"
echo "----------------------------------------"
test_exists "Platform configs (no auth)" "GET" "/api/v1/platform/configs"
test_exists "OAuth platforms" "GET" "/api/v1/platform/oauth/platforms"

echo ""
echo "----------------------------------------"
echo "9. Admin Endpoints (admin.js) - Require Auth"
echo "----------------------------------------"
test_exists "Admin communities" "GET" "/api/v1/admin/communities"
test_exists "Admin users" "GET" "/api/v1/admin/users"
test_exists "Admin analytics" "GET" "/api/v1/admin/analytics"

echo ""
echo "----------------------------------------"
echo "10. Super Admin Endpoints (superadmin.js)"
echo "----------------------------------------"
test_exists "Platform stats" "GET" "/api/v1/superadmin/platform/stats"
test_exists "Platform users" "GET" "/api/v1/superadmin/platform/users"

echo ""
echo "----------------------------------------"
echo "11. Polls Endpoints (polls.js)"
echo "----------------------------------------"
test_exists "List polls" "GET" "/api/v1/polls"
test_exists "Create poll (no auth)" "POST" "/api/v1/polls" '{"question":"test","options":["a","b"]}'

echo ""
echo "----------------------------------------"
echo "12. Forms Endpoints (forms.js)"
echo "----------------------------------------"
test_exists "List forms" "GET" "/api/v1/forms"
test_exists "Create form (no auth)" "POST" "/api/v1/forms" '{"title":"test","fields":[]}'

echo ""
echo "----------------------------------------"
echo "13. Streaming Endpoints (streaming.js)"
echo "----------------------------------------"
test_exists "Stream status" "GET" "/api/v1/streaming/status"
test_exists "Stream config" "GET" "/api/v1/streaming/config"

echo ""
echo "----------------------------------------"
echo "14. Music Endpoints (music.js)"
echo "----------------------------------------"
test_exists "Music status" "GET" "/api/v1/music/status"
test_exists "Music queue" "GET" "/api/v1/music/queue"

echo ""
echo "----------------------------------------"
echo "15. Workflow Endpoints (workflow.js)"
echo "----------------------------------------"
test_exists "List workflows" "GET" "/api/v1/workflows"
test_exists "Workflow by ID" "GET" "/api/v1/workflows/1"

echo ""
echo "----------------------------------------"
echo "16. Calls Endpoints (calls.js)"
echo "----------------------------------------"
test_exists "Call status" "GET" "/api/v1/calls/status"
test_exists "Active calls" "GET" "/api/v1/calls/active"

echo ""
echo "========================================"
echo "Summary"
echo "========================================"
echo -e "Passed:   ${GREEN}$PASSED${NC}"
echo -e "Failed:   ${RED}$FAILED${NC}"
echo -e "Warnings: ${YELLOW}$WARNINGS${NC}"
echo ""

if [ "$FAILED" -gt 0 ]; then
    echo -e "${RED}API SMOKE TEST FAILED${NC}"
    echo ""
    echo "Critical issues found - check application logs"
    exit 1
fi

if [ "$WARNINGS" -gt 0 ]; then
    echo -e "${YELLOW}API SMOKE TEST PASSED WITH WARNINGS${NC}"
    exit 0
fi

echo -e "${GREEN}API SMOKE TEST PASSED${NC}"
echo "All $PASSED endpoints responding correctly!"
exit 0
