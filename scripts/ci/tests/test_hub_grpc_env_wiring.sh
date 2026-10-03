#!/usr/bin/env bash
# Regression test for feature/hub-grpc-env-wiring.
#
# Runs the REAL helm (not stubbed) against the real chart + values-alpha.yaml
# with the Rust data plane enabled and pipes the render through
# scripts/ci/check-hub-grpc-env-wiring.py to assert svc-process-rust and
# svc-action-rust both render a non-empty HUB_API_GRPC_ENDPOINT/
# SERVICE_JWT_TOKEN_ENDPOINT/SERVICE_JWT_SA_TOKEN_PATH -- without these,
# PR #561/#569's PII tokenization (default ON) fails loud at startup and
# crash-loops both Deployments.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/check-hub-grpc-env-wiring.py"

echo "Test: alpha render -- svc-process-rust/svc-action-rust HUB_API_GRPC_ENDPOINT/SERVICE_JWT_* wiring"
helm template waddlebot "$REPO_ROOT/k8s/helm/waddlebot" \
    --values "$REPO_ROOT/k8s/helm/waddlebot/values-alpha.yaml" \
    --kube-version 1.30.0 \
    --set pipeline.rustDataPlane.enabled=true \
    | python3 "$CHECKER"
