#!/usr/bin/env bash
# Repeatable alpha smoke test: proves the deployed Waddles on dal2alpha is functional.
#
# Checks (K8s layer here, HTTP layer in scripts/smoke/alpha_checks.py run inside a pod):
#   k8s: all Deployments ready (count examined)        -- check_deployments
#   k8s: Helm release status == deployed               -- check_helm
#   k8s: seeder: helm deployed AND job absent/Succeeded AND no Failed pods -- check_seeder
#   http: hub-api GET /health, /ready          -> 200  (hub_api/app.py:281 /ready)
#   http: hub-webui GET / -> 200 with <div id="root">  (admin/hub_module/frontend/index.html:14)
#   http: unauth GET /api/v1/superadmin/communities -> 401/403 (blueprints/v1/superadmin.py:446)
#   http: POST /api/v1/auth/login (admin from Secret <release>-hub-admin-bootstrap) -> JWT (auth.py:507)
#   http: GET /api/v1/auth/me (isSuperAdmin)            (auth.py:694)
#   http: GET /api/v1/marketplace/catalog total >= bundle-versions total (marketplace_catalog.py:66)
#   http: GET /api/v1/marketplace/vendors shape, sum==catalog total, first-party filter (ASSUMED shape)
#   http: GET /api/v1/superadmin/communities            (superadmin.py:446)
#   http: GET /api/v1/admin/bundle-versions             (bundle_admin.py:80)
#   SKIP: bundle activation round-trip (only mutating paths exist)
#
# Env: KUBE_CONTEXT (default dal2alpha), NAMESPACE (waddlebot), RELEASE (waddlebot),
#      SMOKE_IMAGE (default: digest-pinned image from chart values waitForHubApi.image),
#      SMOKE_FIRST_PARTY_VENDOR (penguintechinc). Exit 0 only if checks>0 and failed==0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CTX="${KUBE_CONTEXT:-dal2alpha}"
NS="${NAMESPACE:-waddlebot}"
RELEASE="${RELEASE:-waddlebot}"
VENDOR="${SMOKE_FIRST_PARTY_VENDOR:-penguintechinc}"
POD="alpha-smoke-$(date +%s)-$$"
SECRET="${RELEASE}-hub-admin-bootstrap"
RESULTS=()
POD_CREATED=0

k() { kubectl --context "$CTX" -n "$NS" "$@"; }

# redact strips JWT-shaped strings and bearer tokens from any text we print.
redact() { sed -E 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/<jwt-redacted>/g; s/(Bearer )[^ ]+/\1<redacted>/g'; }

# record appends "name|STATUS|detail" to the results list.
record() { RESULTS+=("$1|$2|$3"); printf '[%s] %s %s\n' "$2" "$1" "$3" | redact; }

# resolve_image reads the digest-pinned python image already used by the chart.
resolve_image() {
    if [ -n "${SMOKE_IMAGE:-}" ]; then echo "$SMOKE_IMAGE"; return; fi
    sed -nE 's/^[[:space:]]*image: "(python:3\.13-slim-bookworm@sha256:[0-9a-f]{64})".*/\1/p' \
        "$REPO_ROOT/k8s/helm/waddlebot/values.yaml" | head -1
}

# cleanup deletes the smoke pod; best-effort, never a gate.
cleanup() {
    if [ "$POD_CREATED" = 1 ]; then
        k delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
}

# summarize prints the table and counts; returns non-zero on any failure or zero checks.
summarize() {
    local checks=0 passed=0 failed=0 skipped=0 row name status detail
    echo; printf '%-34s %-6s %s\n' CHECK STATUS DETAIL
    for row in "${RESULTS[@]+"${RESULTS[@]}"}"; do
        IFS='|' read -r name status detail <<<"$row"
        printf '%-34s %-6s %s\n' "$name" "$status" "$detail" | redact
        checks=$((checks + 1))
        case "$status" in
            PASS) passed=$((passed + 1));;
            SKIP) skipped=$((skipped + 1));;
            *) failed=$((failed + 1));;
        esac
    done
    echo "checks=$checks passed=$passed failed=$failed skipped=$skipped"
    if [ "$checks" -eq 0 ]; then echo "FAIL: zero checks executed"; return 1; fi
    [ "$failed" -eq 0 ]
}

# check_deployments asserts every Deployment has readyReplicas == replicas (>=1).
check_deployments() {
    local name want ready total=0 bad=0 listing
    listing="$(k get deploy -o jsonpath='{range .items[*]}{.metadata.name} {.spec.replicas} {.status.readyReplicas}{"\n"}{end}')"
    while read -r name want ready; do
        [ -n "$name" ] || continue
        total=$((total + 1))
        if [ "${want:-0}" -lt 1 ] || [ "${ready:-0}" -ne "$want" ]; then
            bad=$((bad + 1)); record "deploy/$name" FAIL "ready=${ready:-0}/$want"
        fi
    done <<<"$listing"
    if [ "$total" -eq 0 ]; then record deployments FAIL "0 deployments examined"
    elif [ "$bad" -eq 0 ]; then record deployments PASS "$total examined, all ready"
    else record deployments FAIL "$bad of $total not ready"; fi
}

# check_helm asserts the Helm release status is "deployed".
check_helm() {
    local st
    st="$(helm --kube-context "$CTX" -n "$NS" status "$RELEASE" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["info"]["status"])')"
    if [ "$st" = deployed ]; then record helm-release PASS "status=$st"; else record helm-release FAIL "status=$st"; fi
}

# check_seeder: Helm release deployed AND (seeder job absent OR Succeeded) AND no seeder
# pod in Error/Failed; reports which condition held or broke.
check_seeder() {
    local job="${RELEASE}-core-bundle-seeder" out helm_st job_state bad_pods
    helm_st="$(helm --kube-context "$CTX" -n "$NS" status "$RELEASE" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["info"]["status"])')"
    if out="$(k get job "$job" -o jsonpath='{.status.succeeded}/{.status.failed}' 2>/dev/null)"; then
        case "$out" in 1/*) job_state="succeeded";; *) job_state="not-succeeded($out)";; esac
    else
        job_state="absent"
    fi
    bad_pods="$(k get pods -l "job-name=$job" -o jsonpath='{range .items[*]}{.metadata.name}={.status.phase}{"\n"}{end}' | grep -cE '=(Failed|Error)$' || true)"
    if [ "$helm_st" = deployed ] && { [ "$job_state" = absent ] || [ "$job_state" = succeeded ]; } && [ "$bad_pods" -eq 0 ]; then
        record seeder-job PASS "helm=$helm_st job=$job_state bad_pods=0"
    else
        record seeder-job FAIL "helm=$helm_st job=$job_state bad_pods=$bad_pods"
    fi
}

# run_http_checks starts a PSA-restricted pod and executes alpha_checks.py inside it.
run_http_checks() {
    local image line name status detail n=0
    image="$(resolve_image)"
    if [ -z "$image" ]; then record http-image FAIL "no pinned image resolved"; return; fi
    k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  labels: {app.kubernetes.io/name: alpha-smoke}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile: {type: RuntimeDefault}
  containers:
  - name: smoke
    image: $image
    command: ["python3", "-c", "import time; time.sleep(600)"]
    env:
    - {name: SMOKE_FIRST_PARTY_VENDOR, value: "$VENDOR"}
    - name: ADMIN_EMAIL
      valueFrom: {secretKeyRef: {name: $SECRET, key: INITIAL_ADMIN_EMAIL}}
    - name: ADMIN_PASSWORD
      valueFrom: {secretKeyRef: {name: $SECRET, key: INITIAL_ADMIN_PASSWORD}}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: {drop: ["ALL"]}
    resources:
      requests: {cpu: 50m, memory: 64Mi}
      limits: {cpu: 200m, memory: 128Mi}
YAML
    POD_CREATED=1
    if ! k wait --for=condition=Ready "pod/$POD" --timeout=180s >/dev/null; then
        record smoke-pod FAIL "pod not Ready in 180s"; return
    fi
    record smoke-pod PASS "psa-restricted pod ready"
    local out rc=0
    out="$(k exec -i "$POD" -- python3 - <"$SCRIPT_DIR/alpha_checks.py" 2>&1)" || rc=$?
    while IFS= read -r line; do
        case "$line" in RESULT\|*) ;; *) continue;; esac
        IFS='|' read -r _ name status detail <<<"$line"
        record "$name" "$status" "$detail"; n=$((n + 1))
    done <<<"$out"
    if [ "$rc" -ne 0 ]; then record in-pod-checker FAIL "exec exit $rc"; fi
    if [ "$n" -eq 0 ]; then record in-pod-checker FAIL "no results returned"; fi
}

# main runs K8s-layer then HTTP-layer checks and prints the summary.
main() {
    trap cleanup EXIT
    case "${1:-}" in
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0;;
        "") ;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
    command -v kubectl >/dev/null || { echo "kubectl required" >&2; exit 2; }
    command -v helm >/dev/null || { echo "helm required" >&2; exit 2; }
    echo "alpha smoke: context=$CTX namespace=$NS release=$RELEASE"
    check_deployments
    check_helm
    check_seeder
    run_http_checks
    summarize
}

# Guard: only run when executed, so tests can source the helpers.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
