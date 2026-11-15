#!/usr/bin/env bash
# Helm matrix gate for k8s/helm/waddlebot: every environment x every available helm major
# (v3 and v4; the chart must work with both), then the chart tests and the CI render scripts.
#
# What it runs, per helm binary found:
#   1. helm lint + helm template for alpha dal2alpha beta gamma production local ci
#      (beta/gamma/production/ci get --set global.deploymentTier=alpha, exactly like
#      pr-validation.yml: those tiers need real secrets an offline render does not have)
#   2. kubeconform -strict on each render when kubeconform is installed (CRD kinds are skipped)
#   3. the render scripts under scripts/ci/ (hook dependency ordering, placeholder credentials,
#      rendered-manifest invariants, seeder Valkey TLS wiring) on every render, plus
#      scripts/check_helm_hook_phases.py, scripts/check_wait_for_hub_api.py and
#      scripts/ci/tests/test_alpha_render_bundle_executor.sh
#   4. pytest k8s/helm/waddlebot/tests (skip with HELM_MATRIX_QUICK=1 or --quick)
#
# Verification integrity (critical-rules.md): nothing here can pass vacuously. Zero helm binaries,
# zero rendered documents, zero hooks examined, or zero tests passed is a FAILURE, and every
# count is printed. Exit 0 only when every check ran and passed.
#
# Usage: scripts/ci/helm_matrix.sh [--quick]     (make test-helm-matrix / test-helm-matrix-quick)
# Env:   HELM3=/path/to/helm3  HELM4=/path/to/helm4  (default: helm3, helm4, helm on PATH, by version)
#        HELM_MATRIX_OUT=dir   keep renders/logs there (default: a temp dir, removed on success)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHART="$REPO_ROOT/k8s/helm/waddlebot"
ENVS="alpha dal2alpha beta gamma production local ci"
QUICK="${HELM_MATRIX_QUICK:-0}"
[ "${1:-}" = "--quick" ] && QUICK=1

OUT="${HELM_MATRIX_OUT:-$(mktemp -d)}"
mkdir -p "$OUT"
KEEP_OUT=0
[ -n "${HELM_MATRIX_OUT:-}" ] && KEEP_OUT=1
# Host helm plugins can break `helm version` on one major (incompatible plugin.yaml); the matrix
# needs none, so give helm an empty plugin dir.
export HELM_PLUGINS="$OUT/no-plugins"
mkdir -p "$HELM_PLUGINS"

FAILURES=0
SUMMARY=""
fail() { FAILURES=$((FAILURES + 1)); echo "FAIL: $*" >&2; }
note() { SUMMARY="$SUMMARY$*"$'\n'; }

# ---- locate helm binaries by major version -------------------------------------------------
helm_major() { "$1" version --template '{{.Version}}' 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
find_helm() { # $1 = major, $2... = candidates
  local want="$1" c path m; shift
  for c in "$@"; do
    [ -n "$c" ] || continue
    path="$(command -v "$c" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    m="$(helm_major "$path" || true)"
    if [ "$m" = "$want" ]; then echo "$path"; return 0; fi
  done
  return 1
}
HELM_BINS=""
h3="$(find_helm 3 "${HELM3:-}" helm3 helm || true)"
h4="$(find_helm 4 "${HELM4:-}" helm4 helm || true)"
[ -n "$h3" ] && HELM_BINS="$HELM_BINS 3:$h3"
[ -n "$h4" ] && HELM_BINS="$HELM_BINS 4:$h4"
[ -n "$HELM_BINS" ] || { echo "FAIL: no helm v3 or v4 binary found (set HELM3/HELM4)" >&2; exit 1; }
[ -n "$h3" ] || echo "WARN: helm v3 not found -- v3 coverage SKIPPED" >&2
[ -n "$h4" ] || echo "WARN: helm v4 not found -- v4 coverage SKIPPED" >&2

KUBECONFORM="$(command -v kubeconform 2>/dev/null || true)"
[ -n "$KUBECONFORM" ] || echo "WARN: kubeconform not installed -- schema validation SKIPPED" >&2

extra_set() { case "$1" in beta|gamma|production|ci) echo "--set global.deploymentTier=alpha" ;; esac; }

# ---- matrix ----------------------------------------------------------------------------------
for entry in $HELM_BINS; do
  major="${entry%%:*}"; bin="${entry#*:}"
  shim="$OUT/shim-v$major"; mkdir -p "$shim"; ln -sf "$bin" "$shim/helm"
  lint_ok=0; render_ok=0; kc_valid=0; kc_invalid=0; docs_total=0
  for env in $ENVS; do
    vals="$CHART/values-$env.yaml"
    [ -f "$vals" ] || { fail "v$major $env: values file missing ($vals)"; continue; }
    # shellcheck disable=SC2046
    if "$bin" lint "$CHART" -f "$vals" --kube-version 1.30.0 $(extra_set "$env") >"$OUT/v$major-$env.lint" 2>&1; then
      lint_ok=$((lint_ok + 1))
    else fail "v$major $env: helm lint (see $OUT/v$major-$env.lint)"; fi
    render="$OUT/v$major-$env.yaml"
    # shellcheck disable=SC2046
    if "$bin" template waddlebot "$CHART" -f "$vals" --kube-version 1.30.0 $(extra_set "$env") >"$render" 2>"$OUT/v$major-$env.err"; then
      docs="$(grep -c '^kind:' "$render" || true)"
      if [ "$docs" -ge 1 ]; then render_ok=$((render_ok + 1)); docs_total=$((docs_total + docs))
      else fail "v$major $env: render produced zero documents"; continue; fi
    else fail "v$major $env: helm template (see $OUT/v$major-$env.err)"; continue; fi
    if [ -n "$KUBECONFORM" ]; then
      if "$KUBECONFORM" -strict -ignore-missing-schemas -summary "$render" >"$OUT/v$major-$env.kc" 2>&1; then
        kc_valid=$((kc_valid + 1))
      else kc_invalid=$((kc_invalid + 1)); fail "v$major $env: kubeconform (see $OUT/v$major-$env.kc)"; fi
    fi
    # CI render scripts on this render (each fails on zero items examined by itself)
    for chk in check-helm-hook-dependency-ordering check-no-placeholder-credentials \
               check-helm-rendered-manifest-invariants check-seeder-valkey-tls-wiring; do
      if python3 "$REPO_ROOT/scripts/ci/$chk.py" <"$render" >"$OUT/v$major-$env.$chk.log" 2>&1; then
        :
      else fail "v$major $env: $chk (see $OUT/v$major-$env.$chk.log)"; fi
    done
  done
  n_envs="$(echo "$ENVS" | wc -w | tr -d ' ')"
  [ "$render_ok" -ge 1 ] || fail "v$major: zero environments rendered"
  note "helm v$major: lint $lint_ok/$n_envs, render $render_ok/$n_envs ($docs_total documents), kubeconform ${kc_valid}/${n_envs} (invalid envs: $kc_invalid), render scripts 4 x $render_ok"

  # scripts that shell out to `helm` themselves: run with this major first on PATH
  if PATH="$shim:$PATH" python3 "$REPO_ROOT/scripts/check_helm_hook_phases.py" >"$OUT/v$major.hook-phases.log" 2>&1; then
    note "helm v$major: check_helm_hook_phases.py pass"
  else fail "v$major: check_helm_hook_phases.py (see $OUT/v$major.hook-phases.log)"; fi
  if PATH="$shim:$PATH" python3 "$REPO_ROOT/scripts/check_wait_for_hub_api.py" >"$OUT/v$major.wait-hub.log" 2>&1; then
    note "helm v$major: check_wait_for_hub_api.py pass ($(grep -o 'Examined [0-9]*' "$OUT/v$major.wait-hub.log" | head -1))"
  else fail "v$major: check_wait_for_hub_api.py (see $OUT/v$major.wait-hub.log)"; fi
  if PATH="$shim:$PATH" bash "$REPO_ROOT/scripts/ci/tests/test_alpha_render_bundle_executor.sh" >"$OUT/v$major.executor-render.log" 2>&1; then
    note "helm v$major: test_alpha_render_bundle_executor.sh pass"
  else fail "v$major: test_alpha_render_bundle_executor.sh (see $OUT/v$major.executor-render.log)"; fi

  if [ "$QUICK" != "1" ]; then
    log="$OUT/v$major.pytest.log"
    rc=0
    (cd "$REPO_ROOT" && PATH="$shim:$PATH" python3 -m pytest -q -p no:cacheprovider "$CHART/tests") >"$log" 2>&1 || rc=$?
    passed="$(grep -Eo '[0-9]+ passed' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
    failed="$(grep -Eo '[0-9]+ failed' "$log" | tail -1 | grep -Eo '[0-9]+' || true)"
    passed="${passed:-0}"; failed="${failed:-0}"
    if [ "$rc" -ne 0 ] || [ "$passed" -lt 1 ]; then fail "v$major: chart tests rc=$rc passed=$passed failed=$failed (see $log)"; fi
    note "helm v$major: chart tests $passed passed, $failed failed (rc=$rc)"
  else
    note "helm v$major: chart tests SKIPPED (--quick)"
  fi
done

echo "---- helm matrix summary ----"
printf '%s' "$SUMMARY"
if [ "$FAILURES" -ne 0 ]; then
  echo "RESULT: FAIL ($FAILURES failing check(s)); logs kept in $OUT" >&2
  exit 1
fi
echo "RESULT: PASS"
[ "$KEEP_OUT" = "1" ] || rm -rf "$OUT"
