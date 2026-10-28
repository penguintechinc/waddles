#!/usr/bin/env bash
# Gated runners for the marketplace-sources suites (plan Task 11). Usage:
#   scripts/marketplace_test_gate.sh security   # cluster-less regression suite (normal CI)
#   scripts/marketplace_test_gate.sh e2e        # flag-ON cluster E2E (needs a deployment)
# A gate that cannot fail is not a gate: missing prerequisites exit non-zero, and the JUnit result
# is verified independently of pytest's exit code -- ZERO tests executed, ANY skip, any failure or
# error is a FAIL. Counts are always printed. No `|| true` anywhere.
set -euo pipefail

mode="${1:-}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
py="${HUB_PYTHON:-python3}"
junit="$(mktemp -t marketplace-junit.XXXXXX.xml)"
trap 'rm -f "$junit"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

verify_junit() {  # $1 = minimum executed tests
  "$py" -I - "$junit" "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET  # nosec B405 - our own pytest output

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
n = lambda k: sum(int(s.get(k, 0)) for s in suites)  # noqa: E731
tests, fails, errs, skips = n("tests"), n("failures"), n("errors"), n("skipped")
print(f"junit: tests={tests} failures={fails} errors={errs} skipped={skips}")
need = int(sys.argv[2])
if tests < need:
    sys.exit(f"FAIL: only {tests} tests executed, expected >= {need} (zero/short denominator)")
if fails or errs or skips:
    sys.exit("FAIL: failures/errors/skips present (a skip is a FAIL for this gate)")
PY
}

case "$mode" in
  security)
    command -v git >/dev/null || fail "git is required"
    "$py" -c "import flask_core, quart, cryptography, yaml" 2>/dev/null \
      || fail "HUB_PYTHON must have hub_api deps (flask_core, quart, cryptography, PyYAML)"
    cd hub_api
    "$py" -m pytest \
      tests/test_marketplace_sources_security_regression.py \
      tests/test_marketplace_fixture_git_server.py \
      tests/test_marketplace_source_ingest.py tests/test_marketplace_source_service.py \
      tests/test_source_namespace_gate.py tests/test_marketplace_source_scanner.py \
      tests/test_marketplace_publisher_attestation.py \
      tests/test_v1_marketplace_sources_blueprint.py \
      --no-cov -q -p no:cacheprovider -W ignore --junitxml "$junit"
    verify_junit 300
    ;;
  e2e)
    [ "${MARKETPLACE_E2E_ENABLED:-}" = "1" ] \
      || fail "MARKETPLACE_E2E_ENABLED=1 is required (this target needs a live deployment)"
    for v in MARKETPLACE_E2E_BASE_URL MARKETPLACE_E2E_ADMIN_TOKEN MARKETPLACE_E2E_FIXTURE_URL \
             FIXTURE_ADMIN_TOKEN MARKETPLACE_E2E_KUBE_CONTEXT MARKETPLACE_E2E_NAMESPACE; do
      [ -n "${!v:-}" ] || fail "$v is not set"
    done
    command -v kubectl >/dev/null || fail "kubectl is required"
    "$py" -c "import httpx, pytest" 2>/dev/null || fail "HUB_PYTHON needs httpx + pytest"
    # The fixture server must already be reachable (admin API answers) -- started separately.
    "$py" -I - "$MARKETPLACE_E2E_FIXTURE_URL" <<'PY' || fail "fixture git server not reachable"
import os, sys, urllib.request  # noqa: E401
req = urllib.request.Request(sys.argv[1] + "/_admin/repos")
req.add_header("Authorization", "Bearer " + os.environ["FIXTURE_ADMIN_TOKEN"])
with urllib.request.urlopen(req, timeout=15) as r:  # nosec B310 - operator-supplied https URL
    sys.exit(0 if r.status == 200 else 1)
PY
    k_args=()
    [ -z "${PYTEST_K:-}" ] || k_args=(-k "$PYTEST_K")
    # PYTEST_K narrows the run (mutation check per gate); the denominator floor drops with it.
    min=20
    [ -z "${PYTEST_K:-}" ] || min=1
    "$py" -m pytest tests/api/test_marketplace_sources_e2e.py "${k_args[@]}" -o addopts= \
      -o "markers=e2e: cluster e2e" -o "markers=requires_cluster: needs a live cluster" \
      -p no:cacheprovider -W ignore -v --junitxml "$junit"
    verify_junit "$min"
    ;;
  *) fail "usage: $0 security|e2e" ;;
esac
echo "marketplace $mode gate: PASS"
