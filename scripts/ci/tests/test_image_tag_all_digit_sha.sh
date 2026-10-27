#!/usr/bin/env bash
# regression: all-digit short SHA rendered as %!s(int64=...) image tag (alpha 2026-10-03)
#
# Runs the REAL helm (not stubbed) against the real chart + values-alpha.yaml
# with an all-digit short SHA (e.g. "35604387") as global.imageTag, pipes the
# render through scripts/ci/check-image-tag-render.py, and asserts:
#   1. with plain `--set` (the old, broken call style) the tag still renders
#      correctly -- the chart-side `| toString` fix in _helpers.tpl and every
#      inline `repo:tag` template must hold even if a caller forgets
#      --set-string;
#   2. with `--set-string` (the fixed call style every deploy script now
#      uses) the tag renders correctly;
#   3. both renders contain no `%!` anywhere (the literal signature of Go's
#      printf choking on an int64 where it expected a string) and resolve at
#      least one image to the exact digit-string tag.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../" && pwd)"
CHECKER="$REPO_ROOT/scripts/ci/check-image-tag-render.py"
ALL_DIGIT_SHA="35604387"

echo "Test: alpha render -- all-digit short SHA image tag (--set, int64-inference path)"
render_set="$(helm template waddlebot "$REPO_ROOT/k8s/helm/waddlebot" \
    --values "$REPO_ROOT/k8s/helm/waddlebot/values-alpha.yaml" \
    --kube-version 1.30.0 \
    --set "global.imageTag=${ALL_DIGIT_SHA}")"

if printf '%s' "${render_set}" | grep -q '%!'; then
    echo "FAIL: --set render contains a corrupted %! Go format verb:" >&2
    printf '%s\n' "${render_set}" | grep '%!' >&2
    exit 1
fi
printf '%s' "${render_set}" | python3 "$CHECKER" "${ALL_DIGIT_SHA}"

echo "Test: alpha render -- all-digit short SHA image tag (--set-string, fixed path)"
render_set_string="$(helm template waddlebot "$REPO_ROOT/k8s/helm/waddlebot" \
    --values "$REPO_ROOT/k8s/helm/waddlebot/values-alpha.yaml" \
    --kube-version 1.30.0 \
    --set-string "global.imageTag=${ALL_DIGIT_SHA}")"

if printf '%s' "${render_set_string}" | grep -q '%!'; then
    echo "FAIL: --set-string render contains a corrupted %! Go format verb:" >&2
    printf '%s\n' "${render_set_string}" | grep '%!' >&2
    exit 1
fi
printf '%s' "${render_set_string}" | python3 "$CHECKER" "${ALL_DIGIT_SHA}"

echo "PASS: all-digit short SHA renders correctly via both --set and --set-string."
