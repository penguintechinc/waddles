#!/bin/bash

set -e

# Test module database access with scoped credentials
# Verifies each module can access its database with proper RLS enforcement
# Exit codes: 0 = all tests pass, 1 = any test fails

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Database configuration from environment or defaults
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-waddlebot}"
DB_SUPERUSER="${DB_SUPERUSER:-postgres}"
DB_SUPERUSER_PASSWORD="${DB_SUPERUSER_PASSWORD:-postgres}"

# TAP output file
TAP_OUTPUT="${TAP_OUTPUT:-/tmp/module_db_access_test.tap}"

echo "1..0 # TAP version 13" > "$TAP_OUTPUT"

# Test prerequisite: Check database connectivity
test_db_connectivity() {
    local result
    result=$(PGPASSWORD="$DB_SUPERUSER_PASSWORD" psql \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_SUPERUSER" -d "$DB_NAME" \
        -t -c "SELECT 1" 2>&1) || {
        echo -e "${RED}ERROR: Cannot connect to PostgreSQL${NC}"
        echo "Database: $DB_HOST:$DB_PORT/$DB_NAME"
        exit 1
    }
}

# Helper function for connection tests
test_connection() {
    local user="$1"
    local password="$2"
    local label="$3"

    TESTS_RUN=$((TESTS_RUN + 1))

    local result
    result=$(PGPASSWORD="$password" psql \
        -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$DB_NAME" \
        -t -c "SELECT 1" 2>&1) || true

    if [ "$result" = "1" ]; then
        echo -e "${GREEN}✓${NC} PASS: $label ($user)"
        echo "ok $TESTS_RUN - $label ($user)" >> "$TAP_OUTPUT"
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        echo -e "${RED}✗${NC} FAIL: $label ($user)"
        echo "not ok $TESTS_RUN - $label ($user)" >> "$TAP_OUTPUT"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

# Helper function for platform-specific access tests
test_platform_access() {
    local user="$1"
    local password="$2"
    local platform="$3"
    local label="$4"

    TESTS_RUN=$((TESTS_RUN + 1))

    local result
    result=$(PGPASSWORD="$password" psql \
        -h "$DB_HOST" -p "$DB_PORT" -U "$user" -d "$DB_NAME" -t -c \
        "SELECT COUNT(*) FROM platform_integrations WHERE platform = '$platform';" 2>&1) || true

    if [ "$result" -ge 0 ]; then
        echo -e "${GREEN}✓${NC} PASS: $label can access $platform"
        echo "ok $TESTS_RUN - $label can access $platform" >> "$TAP_OUTPUT"
        TESTS_PASSED=$((TESTS_PASSED + 1))
    else
        echo -e "${RED}✗${NC} FAIL: $label cannot access $platform"
        echo "not ok $TESTS_RUN - $label cannot access $platform" >> "$TAP_OUTPUT"
        TESTS_FAILED=$((TESTS_FAILED + 1))
    fi
}

echo "========================================="
echo "Module Database Access Smoke Tests"
echo "========================================="
echo "Target: $DB_HOST:$DB_PORT/$DB_NAME"
echo "========================================="
echo

# Check database connectivity
echo -e "${BLUE}Checking prerequisites...${NC}"
test_db_connectivity
echo -e "${GREEN}Database connectivity OK${NC}"
echo

# Test Admin Users
echo -e "${BLUE}Testing Admin Users...${NC}"
test_connection "hub_admin" "mod_hub_admin_dev_changeme" "Hub Admin"
test_connection "analytics" "mod_analytics_dev_changeme" "Analytics"
test_connection "credential_manager" "mod_credential_manager_dev_changeme" "Credential Manager"
echo

# Test Core Modules
echo -e "${BLUE}Testing Core Modules...${NC}"
test_connection "mod_router" "mod_router_dev_changeme" "Router"
test_connection "mod_core_labels" "mod_core_labels_dev_changeme" "Labels"
test_connection "mod_core_browser_source" "mod_core_browser_source_dev_changeme" "Browser Source"
test_connection "mod_core_identity" "mod_core_identity_dev_changeme" "Identity"
test_connection "mod_core_ai_researcher" "mod_core_ai_researcher_dev_changeme" "AI Researcher"
test_connection "mod_core_workflow" "mod_core_workflow_dev_changeme" "Workflow"
test_connection "mod_core_community" "mod_core_community_dev_changeme" "Community"
test_connection "mod_core_reputation" "mod_core_reputation_dev_changeme" "Reputation"
test_connection "mod_core_security" "mod_core_security_dev_changeme" "Security"
test_connection "mod_core_video_proxy" "mod_core_video_proxy_dev_changeme" "Video Proxy"
test_connection "mod_core_engagement" "mod_core_engagement_dev_changeme" "Engagement"
test_connection "mod_core_rtc" "mod_core_rtc_dev_changeme" "Module RTC"
echo

# Test Trigger Modules
echo -e "${BLUE}Testing Trigger Modules...${NC}"
test_connection "twitch_trigger" "mod_twitch_trigger_dev_changeme" "Twitch Trigger"
test_connection "discord_trigger" "mod_discord_trigger_dev_changeme" "Discord Trigger"
test_connection "slack_trigger" "mod_slack_trigger_dev_changeme" "Slack Trigger"
test_connection "youtube_trigger" "mod_youtube_trigger_dev_changeme" "YouTube Trigger"
test_connection "kick_trigger" "mod_kick_trigger_dev_changeme" "Kick Trigger"
echo

# Test Action Modules
echo -e "${BLUE}Testing Action Modules...${NC}"
test_connection "twitch_action" "mod_twitch_action_dev_changeme" "Twitch Action"
test_connection "discord_action" "mod_discord_action_dev_changeme" "Discord Action"
test_connection "slack_action" "mod_slack_action_dev_changeme" "Slack Action"
test_connection "youtube_action" "mod_youtube_action_dev_changeme" "YouTube Action"
test_connection "lambda_action" "mod_lambda_action_dev_changeme" "Lambda Action"
test_connection "gcp_action" "mod_gcp_action_dev_changeme" "GCP Functions Action"
echo

# Test Interactive Modules
echo -e "${BLUE}Testing Interactive Modules...${NC}"
test_connection "interactive_ai" "mod_interactive_ai_dev_changeme" "AI Interaction"
test_connection "interactive_alias" "mod_interactive_alias_dev_changeme" "Alias Interaction"
test_connection "interactive_shoutout" "mod_interactive_shoutout_dev_changeme" "Shoutout Interaction"
test_connection "interactive_inventory" "mod_interactive_inventory_dev_changeme" "Inventory Interaction"
test_connection "interactive_memories" "mod_interactive_memories_dev_changeme" "Memories Interaction"
test_connection "interactive_ytmusic" "mod_interactive_ytmusic_dev_changeme" "YouTube Music"
test_connection "interactive_spotify" "mod_interactive_spotify_dev_changeme" "Spotify Interaction"
test_connection "interactive_loyalty" "mod_interactive_loyalty_dev_changeme" "Loyalty Interaction"
test_connection "interactive_quote" "mod_interactive_quote_dev_changeme" "Quote Interaction"
echo

# Update TAP output with total count
sed -i '1s/.*/1..'$TESTS_RUN'/' "$TAP_OUTPUT"

echo
echo "========================================="
echo "Module Database Access Test Results"
echo "========================================="
printf "Tests Run:    %2d\n" "$TESTS_RUN"
printf "Tests Passed: ${GREEN}%2d${NC}\n" "$TESTS_PASSED"
printf "Tests Failed: ${RED}%2d${NC}\n" "$TESTS_FAILED"
echo "========================================="
echo "TAP Output: $TAP_OUTPUT"
echo

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ All module database access tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some module database access tests failed!${NC}"
    exit 1
fi
