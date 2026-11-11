#!/bin/bash
# =============================================================================
# WaddleBot Comprehensive Test Suite
# Runs build tests, API tests, and WebUI load tests in parallel
# Outputs logs to /tmp for analysis
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="/tmp/waddlebot-tests-${TIMESTAMP}"
PARALLEL_JOBS="${PARALLEL_JOBS:-8}"
TIMEOUT_SECONDS="${TIMEOUT:-300}"

# Option flags
RUN_BUILD_TESTS=true
RUN_API_TESTS=true
RUN_WEBUI_TESTS=true
SKIP_SERVICE_CHECK=false

# Create log directory
mkdir -p "$LOG_DIR"
mkdir -p "$LOG_DIR/builds"
mkdir -p "$LOG_DIR/api-tests"
mkdir -p "$LOG_DIR/webui-tests"

# Counters (using files for parallel-safe counters)
COUNTER_DIR="$LOG_DIR/counters"
mkdir -p "$COUNTER_DIR"
echo "0" > "$COUNTER_DIR/total"
echo "0" > "$COUNTER_DIR/passed"
echo "0" > "$COUNTER_DIR/failed"
echo "0" > "$COUNTER_DIR/skipped"
echo "0" > "$COUNTER_DIR/connection_errors"

# Failure tracking
FAILURE_DIR="$LOG_DIR/failures"
mkdir -p "$FAILURE_DIR"

# Functions for atomic counter operations
increment_counter() {
    local counter_file="$COUNTER_DIR/$1"
    flock "$counter_file" bash -c "echo \$(( \$(cat $counter_file) + 1 )) > $counter_file"
}

get_counter() {
    cat "$COUNTER_DIR/$1"
}

show_help() {
    cat << EOF
WaddleBot Comprehensive Test Suite

USAGE:
    ./test-all.sh [OPTIONS]

OPTIONS:
    --build-only            Run only build/syntax tests
    --api-only              Run only API tests
    --webui-only            Run only WebUI tests
    --skip-service-check    Skip pre-flight service availability checks
    -h, --help              Show this help message

ENVIRONMENT VARIABLES:
    PARALLEL_JOBS           Number of parallel jobs (default: 8)
    TIMEOUT                 Timeout per test in seconds (default: 300)

EXAMPLES:
    ./test-all.sh                           # Run all tests
    ./test-all.sh --build-only              # Run only syntax/build tests
    ./test-all.sh --api-only                # Run only API tests
    ./test-all.sh --skip-service-check      # Skip pre-flight checks
    PARALLEL_JOBS=16 ./test-all.sh          # Run with 16 parallel jobs

NOTES:
    - All tests run in parallel for speed
    - Logs are saved to /tmp/waddlebot-tests-<timestamp>/
    - Pre-flight checks verify docker-compose and service availability
    - Exit code 0 = all tests passed, non-zero = failures detected

EOF
    exit 0
}

print_banner() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════════════════════════════════╗"
    echo "║              WaddleBot Comprehensive Test Suite                      ║"
    echo "║              All tests run in parallel with logging                  ║"
    echo "╚══════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

# =============================================================================
# PRE-FLIGHT SERVICE CHECKS
# =============================================================================
check_service_availability() {
    print_section "Pre-Flight Service Availability Check"

    local all_services_up=true
    local warnings=()

    # Check if docker-compose is available
    if ! command -v docker-compose &> /dev/null && ! command -v docker &> /dev/null; then
        echo -e "${RED}✗${NC} Docker/docker-compose not found in PATH"
        warnings+=("Docker is not available - API tests will likely fail")
        all_services_up=false
    else
        echo -e "${GREEN}✓${NC} Docker/docker-compose found"

        # Check if any containers are running
        local running_containers=0
        if command -v docker-compose &> /dev/null; then
            running_containers=$(docker-compose ps -q 2>/dev/null | wc -l)
        elif command -v docker &> /dev/null; then
            running_containers=$(docker ps -q 2>/dev/null | wc -l)
        fi

        if [ "$running_containers" -eq 0 ]; then
            echo -e "${YELLOW}⚠${NC} No Docker containers appear to be running"
            warnings+=("No containers running - consider running: docker-compose up -d")
            all_services_up=false
        else
            echo -e "${GREEN}✓${NC} Docker containers are running ($running_containers found)"
        fi
    fi

    # Check critical service ports
    local services=(
        "8060:Hub Module (WebUI)"
        "8000:Router Module"
        "5432:PostgreSQL Database"
    )

    echo ""
    echo "Checking critical service ports:"
    for service_spec in "${services[@]}"; do
        IFS=':' read -r port service_name <<< "$service_spec"

        if timeout 2 bash -c "echo > /dev/tcp/localhost/$port" 2>/dev/null; then
            echo -e "  ${GREEN}✓${NC} Port $port ($service_name) is accessible"
        else
            echo -e "  ${YELLOW}⚠${NC} Port $port ($service_name) is not accessible"
            warnings+=("Port $port ($service_name) not responding")
            all_services_up=false
        fi
    done

    echo ""

    if [ "$all_services_up" = true ]; then
        echo -e "${GREEN}✓ All critical services appear to be available${NC}"
        echo ""
        return 0
    else
        echo -e "${YELLOW}⚠ Some services may not be available${NC}"
        echo ""
        echo -e "${YELLOW}Warnings:${NC}"
        for warning in "${warnings[@]}"; do
            echo -e "  ${YELLOW}•${NC} $warning"
        done
        echo ""
        echo -e "${CYAN}Suggestions:${NC}"
        echo -e "  ${CYAN}•${NC} Start services: ${BLUE}docker-compose up -d${NC}"
        echo -e "  ${CYAN}•${NC} Check service status: ${BLUE}docker-compose ps${NC}"
        echo -e "  ${CYAN}•${NC} View service logs: ${BLUE}docker-compose logs${NC}"
        echo -e "  ${CYAN}•${NC} Skip this check: ${BLUE}./test-all.sh --skip-service-check${NC}"
        echo ""

        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}Aborting tests${NC}"
            exit 1
        fi
    fi
}

print_section() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# Function to run a test in background with timeout
run_test_async() {
    local test_type="$1"  # build, api, webui
    local test_name="$2"
    local test_command="$3"
    local log_file="$LOG_DIR/${test_type}/${test_name//\//_}.log"

    increment_counter "total"

    (
        echo "Starting: $test_name" > "$log_file"
        echo "Command: $test_command" >> "$log_file"
        echo "Started at: $(date)" >> "$log_file"
        echo "----------------------------------------" >> "$log_file"

        if timeout "$TIMEOUT_SECONDS" bash -c "$test_command" >> "$log_file" 2>&1; then
            echo "----------------------------------------" >> "$log_file"
            echo "Completed at: $(date)" >> "$log_file"
            echo "Status: PASSED" >> "$log_file"
            increment_counter "passed"
            echo -e "${GREEN}✓${NC} $test_name"
        else
            exit_code=$?
            echo "----------------------------------------" >> "$log_file"
            echo "Completed at: $(date)" >> "$log_file"

            # Check if this is a connection error
            if grep -qi "connection refused\|failed to connect\|connection timeout\|no route to host\|network unreachable" "$log_file"; then
                echo "Failure Type: CONNECTION_ERROR" >> "$log_file"
                increment_counter "connection_errors"
                echo "$test_name" >> "$FAILURE_DIR/connection_errors.txt"
            fi

            if [ $exit_code -eq 124 ]; then
                echo "Status: TIMEOUT (${TIMEOUT_SECONDS}s)" >> "$log_file"
                increment_counter "failed"
                echo "$test_name" >> "$FAILURE_DIR/timeouts.txt"
                echo -e "${RED}✗${NC} $test_name (TIMEOUT)"
            else
                echo "Status: FAILED (exit code: $exit_code)" >> "$log_file"
                increment_counter "failed"
                echo "$test_name" >> "$FAILURE_DIR/test_failures.txt"
                echo -e "${RED}✗${NC} $test_name"
            fi
        fi
    ) &
}

# =============================================================================
# BUILD TESTS
# =============================================================================
run_build_tests() {
    print_section "Build Tests (Syntax & Docker)"

    echo "Launching Python syntax checks in parallel..."

    # Core module syntax tests
    for module in libs/flask_core/flask_core/*.py; do
        if [ -f "$module" ]; then
            module_name=$(basename "$module" .py)
            run_test_async "builds" "syntax_flask_core_${module_name}" "python3 -m py_compile '$module'"
        fi
    done

    echo "Build tests launched (running in background)..."
}

# =============================================================================
# API TESTS
# =============================================================================
run_api_tests() {
    print_section "API Tests (REST & gRPC)"

    echo "Launching API tests in parallel..."

    # Core modules
    test_scripts=(
    )

    # Processing modules
    test_scripts+=(
    )

    # Action modules (interactive)
    test_scripts+=(
    )

    # Action modules (pushing)
    test_scripts+=(
    )

    # Trigger modules
    test_scripts+=(
    )

    # Admin modules
    test_scripts+=(
    )

    # Run all API tests
    for test_spec in "${test_scripts[@]}"; do
        IFS=':' read -r script_path test_name <<< "$test_spec"
        if [ -f "$SCRIPT_DIR/$script_path" ]; then
            run_test_async "api-tests" "$test_name" "cd '$SCRIPT_DIR' && bash '$script_path'"
        else
            echo -e "${YELLOW}⊘${NC} $test_name (script not found: $script_path)"
            increment_counter "total"
            increment_counter "skipped"
        fi
    done

    echo "API tests launched (running in background)..."
}

# =============================================================================
# WEBUI TESTS
# =============================================================================
run_webui_tests() {
    print_section "WebUI Load Tests"

    echo "Launching WebUI tests..."

    # Hub module WebUI test
    if [ -f "$SCRIPT_DIR/admin/hub_module/test-webui.sh" ]; then
        run_test_async "webui-tests" "hub_webui" "cd '$SCRIPT_DIR' && bash 'admin/hub_module/test-webui.sh'"
    fi

    # Additional page load tests for specific routes
    webui_pages=(
        "/:home_page"
        "/login:login_page"
        "/register:register_page"
        "/dashboard:dashboard_page"
        "/communities:communities_page"
        "/cookie-policy:cookie_policy_page"
    )

    for page_spec in "${webui_pages[@]}"; do
        IFS=':' read -r page_path page_name <<< "$page_spec"
        run_test_async "webui-tests" "hub_${page_name}" "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 'http://localhost:8060${page_path}' | grep -q '200'"
    done

    echo "WebUI tests launched (running in background)..."
}

# =============================================================================
# WAIT FOR COMPLETION
# =============================================================================
wait_for_tests() {
    print_section "Waiting for all tests to complete..."

    local max_wait=$((TIMEOUT_SECONDS + 60))
    local elapsed=0
    local spin='-\|/'
    local i=0

    echo -n "Waiting for background jobs "

    while [ $(jobs -r | wc -l) -gt 0 ]; do
        i=$(( (i+1) %4 ))
        printf "\r${CYAN}Waiting for background jobs ${spin:$i:1}${NC} (${elapsed}s elapsed, $(jobs -r | wc -l) remaining)"
        sleep 1
        elapsed=$((elapsed + 1))

        if [ $elapsed -gt $max_wait ]; then
            echo -e "\n${RED}Timeout waiting for tests to complete!${NC}"
            kill $(jobs -p) 2>/dev/null || true
            break
        fi
    done

    printf "\r${GREEN}All background jobs completed!${NC} (${elapsed}s total)                    \n"
}

# =============================================================================
# GENERATE SUMMARY REPORT
# =============================================================================
generate_report() {
    print_section "Test Results Summary"

    local total=$(get_counter "total")
    local passed=$(get_counter "passed")
    local failed=$(get_counter "failed")
    local skipped=$(get_counter "skipped")
    local connection_errors=$(get_counter "connection_errors")

    local report_file="$LOG_DIR/SUMMARY.txt"

    {
        echo "WaddleBot Comprehensive Test Suite Results"
        echo "==========================================="
        echo "Timestamp: $(date)"
        echo "Log Directory: $LOG_DIR"
        echo ""
        echo "Test Results:"
        echo "  Total:              $total"
        echo "  Passed:             $passed"
        echo "  Failed:             $failed"
        echo "  Skipped:            $skipped"
        echo "  Connection Errors:  $connection_errors"
        echo ""

        if [ $failed -gt 0 ]; then
            echo "Failure Breakdown by Category:"
            echo "-------------------------------"

            # Connection errors
            if [ -f "$FAILURE_DIR/connection_errors.txt" ]; then
                local conn_err_count=$(wc -l < "$FAILURE_DIR/connection_errors.txt" 2>/dev/null || echo 0)
                if [ "$conn_err_count" -gt 0 ]; then
                    echo ""
                    echo "Connection Errors ($conn_err_count):"
                    cat "$FAILURE_DIR/connection_errors.txt" | while read -r test; do
                        echo "  - $test"
                    done
                    echo ""
                    echo "  Suggestion: Run 'docker-compose up -d' to start services"
                fi
            fi

            # Timeouts
            if [ -f "$FAILURE_DIR/timeouts.txt" ]; then
                local timeout_count=$(wc -l < "$FAILURE_DIR/timeouts.txt" 2>/dev/null || echo 0)
                if [ "$timeout_count" -gt 0 ]; then
                    echo ""
                    echo "Timeouts ($timeout_count):"
                    cat "$FAILURE_DIR/timeouts.txt" | while read -r test; do
                        echo "  - $test"
                    done
                    echo ""
                    echo "  Suggestion: Increase timeout with TIMEOUT=600 ./test-all.sh"
                fi
            fi

            # Test failures
            if [ -f "$FAILURE_DIR/test_failures.txt" ]; then
                local test_fail_count=$(wc -l < "$FAILURE_DIR/test_failures.txt" 2>/dev/null || echo 0)
                if [ "$test_fail_count" -gt 0 ]; then
                    echo ""
                    echo "Test Failures ($test_fail_count):"
                    cat "$FAILURE_DIR/test_failures.txt" | while read -r test; do
                        echo "  - $test"
                    done
                fi
            fi

            echo ""
            echo "Detailed Failed Test Logs:"
            echo "--------------------------"
            grep -l "Status: FAILED\|Status: TIMEOUT" "$LOG_DIR"/*/*.log 2>/dev/null | while read -r log_file; do
                echo "  Log: $log_file"
            done
            echo ""
        fi

        echo "Detailed logs available in: $LOG_DIR"
        echo ""
        echo "Quick log access:"
        echo "  Build logs:  $LOG_DIR/builds/"
        echo "  API logs:    $LOG_DIR/api-tests/"
        echo "  WebUI logs:  $LOG_DIR/webui-tests/"
    } | tee "$report_file"

    # Console summary with colors
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║          Test Summary                  ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════╝${NC}"
    echo -e "Total:              $total"
    echo -e "${GREEN}Passed:             $passed${NC}"
    echo -e "${RED}Failed:             $failed${NC}"
    echo -e "${YELLOW}Skipped:            $skipped${NC}"
    if [ "$connection_errors" -gt 0 ]; then
        echo -e "${MAGENTA}Connection Errors:  $connection_errors${NC}"
    fi
    echo ""

    if [ $total -gt 0 ]; then
        local pass_rate=$((passed * 100 / total))
        echo -e "Pass Rate: ${pass_rate}%"
    fi

    echo ""
    echo -e "${BLUE}Full report: $report_file${NC}"
    echo -e "${BLUE}Log directory: $LOG_DIR${NC}"
    echo ""

    # Show helpful suggestions for connection errors
    if [ "$connection_errors" -gt 0 ]; then
        echo -e "${YELLOW}╔════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${YELLOW}║  Connection errors detected - services may be down         ║${NC}"
        echo -e "${YELLOW}╚════════════════════════════════════════════════════════════╝${NC}"
        echo -e "${CYAN}Suggestions:${NC}"
        echo -e "  ${CYAN}•${NC} Start services: ${BLUE}docker-compose up -d${NC}"
        echo -e "  ${CYAN}•${NC} Check status:   ${BLUE}docker-compose ps${NC}"
        echo -e "  ${CYAN}•${NC} View logs:      ${BLUE}docker-compose logs${NC}"
        echo ""
    fi

    # Return exit code based on results
    if [ $failed -eq 0 ]; then
        echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║   ✓ All tests passed!                  ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        return 0
    else
        echo -e "${RED}╔════════════════════════════════════════╗${NC}"
        echo -e "${RED}║   ✗ Some tests failed!                 ║${NC}"
        echo -e "${RED}╚════════════════════════════════════════╝${NC}"
        return 1
    fi
}

# =============================================================================
# ARGUMENT PARSING
# =============================================================================
parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --build-only)
                RUN_BUILD_TESTS=true
                RUN_API_TESTS=false
                RUN_WEBUI_TESTS=false
                shift
                ;;
            --api-only)
                RUN_BUILD_TESTS=false
                RUN_API_TESTS=true
                RUN_WEBUI_TESTS=false
                shift
                ;;
            --webui-only)
                RUN_BUILD_TESTS=false
                RUN_API_TESTS=false
                RUN_WEBUI_TESTS=true
                shift
                ;;
            --skip-service-check)
                SKIP_SERVICE_CHECK=true
                shift
                ;;
            -h|--help)
                show_help
                ;;
            *)
                echo -e "${RED}Error: Unknown option: $1${NC}"
                echo "Use --help for usage information"
                exit 1
                ;;
        esac
    done
}

# =============================================================================
# MAIN
# =============================================================================
main() {
    # Parse command-line arguments first
    parse_arguments "$@"

    print_banner

    echo "Configuration:"
    echo "  Project Root:    $SCRIPT_DIR"
    echo "  Log Directory:   $LOG_DIR"
    echo "  Parallel Jobs:   $PARALLEL_JOBS"
    echo "  Timeout:         ${TIMEOUT_SECONDS}s"
    echo ""
    echo "Test Selection:"
    echo "  Build Tests:     $([ "$RUN_BUILD_TESTS" = true ] && echo "Yes" || echo "No")"
    echo "  API Tests:       $([ "$RUN_API_TESTS" = true ] && echo "Yes" || echo "No")"
    echo "  WebUI Tests:     $([ "$RUN_WEBUI_TESTS" = true ] && echo "Yes" || echo "No")"
    echo "  Service Check:   $([ "$SKIP_SERVICE_CHECK" = true ] && echo "Skipped" || echo "Enabled")"
    echo ""

    # Change to project directory
    cd "$SCRIPT_DIR"

    # Run pre-flight service checks (unless skipped or build-only)
    if [ "$SKIP_SERVICE_CHECK" = false ] && { [ "$RUN_API_TESTS" = true ] || [ "$RUN_WEBUI_TESTS" = true ]; }; then
        check_service_availability
    fi

    # Run selected test suites (they launch background jobs)
    if [ "$RUN_BUILD_TESTS" = true ]; then
        run_build_tests
    fi

    if [ "$RUN_API_TESTS" = true ]; then
        run_api_tests
    fi

    if [ "$RUN_WEBUI_TESTS" = true ]; then
        run_webui_tests
    fi

    # Wait for all background jobs
    wait_for_tests

    # Generate and display report
    generate_report
    exit_code=$?

    echo ""
    echo "To view detailed logs:"
    echo "  ls -la $LOG_DIR"
    echo "  cat $LOG_DIR/SUMMARY.txt"
    echo ""

    exit $exit_code
}

# Handle Ctrl+C gracefully
trap 'echo -e "\n${YELLOW}Tests interrupted by user${NC}"; kill $(jobs -p) 2>/dev/null; exit 130' INT

# Run main
main "$@"
