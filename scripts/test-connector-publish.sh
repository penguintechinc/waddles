#!/usr/bin/env bash
# Real-path test: publish a connector to a pinned SeaweedFS with a throwaway test key, then the
# bundle-executor's own pin loader verifies + loads it; tampering must be refused.
# Needs docker + cargo (Rust 1.97.x) + hub_api python deps. A missing prerequisite FAILS.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "test-connector-publish: FAIL -- docker required" >&2; exit 1; }
command -v cargo >/dev/null || { echo "test-connector-publish: FAIL -- cargo required" >&2; exit 1; }
cd hub_api
WADDLES_E2E_CONNECTOR_PUBLISH=1 "${PYTHON:-python3}" -m pytest tests/test_connector_publish_e2e.py -v -p no:cacheprovider --no-cov
