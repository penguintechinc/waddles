#!/usr/bin/env bash
# Runs the real Playwright E2E suite (tests/e2e/, ~35 workflow specs) against
# the running docker-compose stack's hub-webui frontend (localhost:3000 ->
# hub-api, per tests/e2e/README.md).
#
# This previously duplicated tests/k8s/alpha/run-all-alpha.sh (a K8s
# build+deploy+... chain) whose first step ran `docker build -t
# $PROJECT_NAME:alpha .` against a repo-root Dockerfile that doesn't exist in
# this multi-service monorepo (every service has its own Dockerfile under
# its own directory) -- it errored before ever exercising a single test. E2E
# tests target the already-running local stack; they don't build or deploy
# one from scratch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'
log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $*"; }

BASE_URL="${BASE_URL:-http://localhost:3000}"
# First-run images build from Dockerfile (no cache) -- default generously;
# override for a faster fail in CI once images are warm.
E2E_STACK_TIMEOUT="${E2E_STACK_TIMEOUT:-600}"

log_info "Running E2E tests (Playwright) against $BASE_URL"

is_healthy() {
    docker compose ps "$1" 2>/dev/null | grep -q "(healthy)"
}

# hub-webui depends_on hub-api (which depends_on postgres/redis/seaweedfs/
# db-migrations), so bringing up hub-webui alone is enough to start the
# whole dependency chain needed for the frontend to serve.
if ! is_healthy hub-webui || ! is_healthy hub-api; then
    log_info "hub-webui/hub-api not healthy -- starting via docker compose"
    docker compose up -d hub-webui
    elapsed=0
    until is_healthy hub-webui && is_healthy hub-api; do
        if [ "$elapsed" -ge "$E2E_STACK_TIMEOUT" ]; then
            log_fail "hub-webui/hub-api did not become healthy within ${E2E_STACK_TIMEOUT}s"
            docker compose ps hub-webui hub-api 2>&1 || true
            exit 1
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
fi
log_pass "Stack healthy (hub-webui + hub-api)"

cd "$REPO_ROOT/tests/e2e"

log_info "Installing E2E test dependencies"
npm ci
npx playwright install chromium

log_info "Running Playwright suite"
set +e
BASE_URL="$BASE_URL" npx playwright test
status=$?
set -e

if [ "$status" -ne 0 ]; then
    log_fail "E2E suite failed (exit $status)"
    exit "$status"
fi

log_pass "E2E suite completed"
