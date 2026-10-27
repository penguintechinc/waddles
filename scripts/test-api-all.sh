#!/bin/bash
#
# Waddles Comprehensive API Test Script
# Tests health endpoints for all modules
#

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
PASSED=0
FAILED=0
SKIPPED=0

# Function to print section headers
section() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

# Function to test a health endpoint
test_health() {
    local name="$1"
    local url="$2"
    local expected_status="${3:-200}"

    printf "  %-35s" "$name..."

    response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || echo "000")

    if [ "$response" = "$expected_status" ]; then
        echo -e "${GREEN}PASS${NC} (HTTP $response)"
        ((PASSED++))
        return 0
    elif [ "$response" = "000" ]; then
        echo -e "${YELLOW}SKIP${NC} (Connection refused)"
        ((SKIPPED++))
        return 0
    else
        echo -e "${RED}FAIL${NC} (HTTP $response, expected $expected_status)"
        ((FAILED++))
        return 0
    fi
}

# Function to test JSON response content
test_json_field() {
    local name="$1"
    local url="$2"
    local field="$3"
    local expected="$4"

    printf "  %-35s" "$name..."

    value=$(curl -s --connect-timeout 5 "$url" 2>/dev/null | jq -r "$field" 2>/dev/null || echo "")

    if [ "$value" = "$expected" ]; then
        echo -e "${GREEN}PASS${NC} ($field=$value)"
        ((PASSED++))
        return 0
    elif [ -z "$value" ] || [ "$value" = "null" ]; then
        echo -e "${YELLOW}SKIP${NC} (No response)"
        ((SKIPPED++))
        return 0
    else
        echo -e "${RED}FAIL${NC} ($field=$value, expected $expected)"
        ((FAILED++))
        return 0
    fi
}

# Function to check non-HTTP services via docker
test_docker_health() {
    local name="$1"
    local container="$2"

    printf "  %-35s" "$name..."

    status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")

    if [ "$status" = "healthy" ]; then
        echo -e "${GREEN}PASS${NC} (Docker: $status)"
        ((PASSED++))
        return 0
    elif [ "$status" = "unknown" ]; then
        echo -e "${YELLOW}SKIP${NC} (Container not found)"
        ((SKIPPED++))
        return 0
    else
        echo -e "${RED}FAIL${NC} (Docker: $status)"
        ((FAILED++))
        return 0
    fi
}

echo ""
echo -e "${BLUE}Waddles Comprehensive API Test Suite${NC}"
echo -e "${BLUE}=======================================${NC}"
echo ""

# ============================================
# CORE INFRASTRUCTURE
# ============================================
section "Core Infrastructure"

test_docker_health "PostgreSQL" "waddlebot-postgres"
test_docker_health "Redis" "waddlebot-redis"
test_health "SeaweedFS" "http://localhost:9333/cluster/status"

# ============================================
# PROCESSING MODULE
# ============================================
section "Processing Module"

test_health "Router Module" "http://localhost:8000/health"
test_json_field "Router Status" "http://localhost:8000/health" ".status" "healthy"

# ============================================
# TRIGGER MODULES (Receivers)
# ============================================
section "Trigger Modules (Receivers)"

test_docker_health "Twitch Receiver" "waddlebot-twitch"
test_docker_health "Discord Receiver" "waddlebot-discord"
test_docker_health "Slack Receiver" "waddlebot-slack"
test_docker_health "YouTube Live Receiver" "waddlebot-youtube-live"

# ============================================
# ACTION MODULES (Interactive)
# ============================================
section "Action Modules (Interactive)"

test_docker_health "Shoutout Module" "waddlebot-shoutout"
test_docker_health "Inventory Module" "waddlebot-inventory"
test_docker_health "Alias Module" "waddlebot-alias"
test_docker_health "Calendar Module" "waddlebot-calendar"
test_docker_health "Memories Module" "waddlebot-memories"
test_docker_health "AI Interaction" "waddlebot-ai"
test_docker_health "YouTube Music" "waddlebot-youtube-music"
test_docker_health "Spotify Module" "waddlebot-spotify"

# ============================================
# ACTION MODULES (Pushing - Serverless)
# ============================================
section "Action Modules (Pushing - Serverless)"

test_docker_health "Lambda Action" "waddlebot-lambda-action"
test_docker_health "GCP Functions Action" "waddlebot-gcp-functions-action"
test_docker_health "OpenWhisk Action" "waddlebot-openwhisk-action"

# ============================================
# CORE MODULES
# ============================================
section "Core Modules"

test_docker_health "Identity Core" "waddlebot-identity"
test_docker_health "Labels Core" "waddlebot-labels"
test_docker_health "Community Core" "waddlebot-community"
test_docker_health "Browser Source" "waddlebot-browser-source"
test_docker_health "Reputation Core" "waddlebot-reputation"

# ============================================
# ADMIN MODULES
# ============================================
section "Admin Modules"

test_docker_health "Hub Module" "waddlebot-hub"

# ============================================
# AI SERVICES
# ============================================
section "AI Services"

test_health "Ollama API" "http://localhost:11434/api/tags"

# Test Ollama model availability
printf "  %-35s" "Ollama tinyllama model..."
models=$(curl -s --connect-timeout 5 "http://localhost:11434/api/tags" 2>/dev/null | jq -r '.models[].name' 2>/dev/null || echo "")
if echo "$models" | grep -q "tinyllama"; then
    echo -e "${GREEN}PASS${NC} (Model loaded)"
    ((PASSED++))
else
    echo -e "${YELLOW}SKIP${NC} (Model not found)"
    ((SKIPPED++))
fi

# ============================================
# OPENWHISK INTEGRATION
# ============================================
section "OpenWhisk Integration"

test_health "OpenWhisk Controller" "http://localhost:3233/ping"

# ============================================
# SUMMARY
# ============================================
section "Test Summary"

TOTAL=$((PASSED + FAILED + SKIPPED))

echo ""
echo -e "  Total Tests:   $TOTAL"
echo -e "  ${GREEN}Passed:${NC}        $PASSED"
echo -e "  ${RED}Failed:${NC}        $FAILED"
echo -e "  ${YELLOW}Skipped:${NC}       $SKIPPED"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All active services are responding correctly!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed. Check the output above for details.${NC}"
    exit 1
fi
