#!/usr/bin/env bash
# Pre-push wrapper for k8s/helm/waddlebot/tests/test_provision_secrets_novalue_render.py.
#
# Regression gate (alpha deploy blocker 2026-11-14): Helm blanks the kubectl
# "<no value>" sentinel in provision-secrets.sh, turning the deployed strip into
# a no-op. The bug only exists post-render, so the test renders the chart with
# helm. The test itself `pytest.skip`s when helm is absent -- a skip exits 0 and
# would let the push through unchecked (critical-rules.md Verification Integrity:
# a silently-skipped gate is the same defect as `|| true`). This wrapper fails
# LOUD when helm is missing instead, so the gate can never silently disappear.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if ! command -v helm3 >/dev/null 2>&1 && ! command -v helm >/dev/null 2>&1; then
  echo "FAIL: helm (or helm3) not found -- required for the provision-secrets no-value render gate." >&2
  echo "      Install helm or run this gate in an environment that has it; it must not be skipped." >&2
  exit 1
fi

# -p no:cacheprovider keeps the worktree clean; the helm presence check above
# guarantees the test runs for real (no skip) in this environment.
exec python3 -m pytest -q -p no:cacheprovider \
  k8s/helm/waddlebot/tests/test_provision_secrets_novalue_render.py
