#!/usr/bin/env bash
# Marketing screenshot run: preflight, pinned Playwright install, capture,
# artifact cleanup (pass or fail). Invoked by `make screenshots`.
#
# Env: BASE_URL (default http://localhost:8060), SCREENSHOT_EMAIL /
#      SCREENSHOT_PASSWORD (or ADMIN_EMAIL / ADMIN_PASSWORD), COMMUNITY_ID,
#      TENANT_SLUG, OUT_DIR, STRICT_EMPTY=1.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_DIR="${ROOT}/tests/screenshots"
export BASE_URL="${BASE_URL:-http://localhost:8060}"
export PLAYWRIGHT_OUTPUT_DIR="/tmp/playwright-waddles"

# Per-repo Playwright scratch dir; removed on exit whether the run passed or failed.
cleanup() { rm -rf "${PLAYWRIGHT_OUTPUT_DIR}"; }
trap cleanup EXIT

if [[ -z "${SCREENSHOT_EMAIL:-${ADMIN_EMAIL:-}}" || -z "${SCREENSHOT_PASSWORD:-${ADMIN_PASSWORD:-}}" ]]; then
    echo "ERROR: set SCREENSHOT_EMAIL/SCREENSHOT_PASSWORD (or ADMIN_EMAIL/ADMIN_PASSWORD)" >&2
    exit 2
fi

echo "Preflight: ${BASE_URL}"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${BASE_URL}/login" || true)"
if [[ "${code}" != 2* && "${code}" != 3* ]]; then
    echo "ERROR: hub-webui not reachable at ${BASE_URL} (HTTP ${code:-none}); start it and run 'make seed-mock-data' first" >&2
    exit 3
fi

mkdir -p "${PLAYWRIGHT_OUTPUT_DIR}"
cd "${TOOL_DIR}"
npm ci --ignore-scripts --no-audit --no-fund
npx --no-install playwright install chromium
node capture.cjs
