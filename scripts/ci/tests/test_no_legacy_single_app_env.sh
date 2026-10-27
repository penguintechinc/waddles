#!/usr/bin/env bash
# Regression test for fix/legacy-path-mutually-exclusive.
#
# `check-alpha-render-bundle-executor.py` (fix/alpha-deploy-executor-and-failfast)
# already asserts no legacy single-app env var renders against values-alpha.yaml,
# but hard-requires a bundle-executor Deployment to be present -- beta/gamma/
# production all disable rustDataPlane.bundleExecutor{,Action} and would never
# reach that assertion. This test runs the env-agnostic
# check-no-legacy-single-app-env.py against a BARE render (no env-specific
# values file -- the base values.yaml defaults every operator inherits before
# choosing an environment) and every values-{alpha,beta,gamma,production}.yaml
# topology, proving the chart never renders PROCESS_APP_ID/PROCESS_INGEST_*/
# PROCESS_BUNDLE_*/ACTION_APP_ID/ACTION_BUNDLE_* unless an operator explicitly
# sets the driving value.
#
# regression: legacy ping consumer competed in the same consumer group as the
# multi-tenant one; ping intermittently UnknownBundle (alpha 2026-10-03)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
CHART="$REPO_ROOT/k8s/helm/waddlebot"
CHECKER="$REPO_ROOT/scripts/ci/check-no-legacy-single-app-env.py"
FAKE_SHA="ciprobe01"

echo "Test: bare render (no env-specific values file) -- no legacy single-app env"
helm template waddlebot "$CHART" \
    --kube-version 1.30.0 \
    --set global.deploymentTier=alpha \
    --set-string "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER"

echo "Test: alpha render -- no legacy single-app env"
helm template waddlebot "$CHART" \
    --values "$CHART/values-alpha.yaml" \
    --kube-version 1.30.0 \
    --set-string "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER"

# values-beta.yaml/values-gamma.yaml/values-production.yaml pin
# global.deploymentTier=production, which requires every generated secret to be
# supplied explicitly (or already present on a live cluster via `lookup`) --
# neither is available to an offline `helm template` in CI. Overriding to the
# alpha secret-generation tier here (same convention as pr-validation.yml's
# "beta topology" steps) renders the SAME topology purely so this env check can
# run against it; it is not a substitute for a real deploy.
echo "Test: beta topology render -- no legacy single-app env"
helm template waddlebot "$CHART" \
    --values "$CHART/values-beta.yaml" \
    --kube-version 1.30.0 \
    --set global.deploymentTier=alpha \
    --set-string "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER"

echo "Test: gamma topology render -- no legacy single-app env"
helm template waddlebot "$CHART" \
    --values "$CHART/values-gamma.yaml" \
    --kube-version 1.30.0 \
    --set global.deploymentTier=alpha \
    --set-string "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER"

echo "Test: production topology render -- no legacy single-app env"
helm template waddlebot "$CHART" \
    --values "$CHART/values-production.yaml" \
    --kube-version 1.30.0 \
    --set global.deploymentTier=alpha \
    --set-string "global.imageTag=${FAKE_SHA}" \
    | python3 "$CHECKER"
