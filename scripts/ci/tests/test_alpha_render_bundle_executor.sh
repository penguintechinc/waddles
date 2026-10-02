#!/usr/bin/env bash
# Regression test for fix/alpha-deploy-executor-and-failfast.
#
# Runs the REAL helm (not stubbed) against the real chart + values-alpha.yaml
# and pipes the render through scripts/ci/check-alpha-render-bundle-executor.py
# to assert, against the actual templates/values (not alpha-deploy.sh's build
# logic -- see test_alpha_deploy_service_build_list.sh for that):
#   1. both bundle-executor Deployments carry the OOM-fix resources
#      (1Gi/500m request, 2560Mi/2000m limit);
#   2. both bundle-executor Deployments' startupProbe gives a cold Cranelift
#      compile a generous budget (failureThreshold >= 30 at periodSeconds=5);
#   3. the bundle-executor image resolves to the SHA tag passed via
#      global.imageTag, never a static "alpha" tag;
#   4. no legacy single-bundle env var (PROCESS_APP_ID, PROCESS_INGEST_*,
#      PROCESS_BUNDLE_*, ACTION_APP_ID, ACTION_BUNDLE_*) appears anywhere in
#      the alpha render.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/check-alpha-render-bundle-executor.py"
FAKE_SHA="ciprobe01"

echo "Test: alpha render -- bundle-executor resources/probes/image-tag/legacy-env"
helm template waddlebot "$REPO_ROOT/k8s/helm/waddlebot" \
    --values "$REPO_ROOT/k8s/helm/waddlebot/values-alpha.yaml" \
    --kube-version 1.30.0 \
    --set "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER" "$FAKE_SHA"
