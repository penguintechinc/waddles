#!/usr/bin/env bash
# =============================================================================
# Waddles Alpha Deployment Script
# Local MicroK8s Deployment via Helm
#
# Helm is the only supported deployment path (alpha through production) --
# Kustomize and Docker Compose are deprecated. This script builds each
# pipeline service's image, pushes it to the local MicroK8s registry, and
# deploys the whole stack in one `helm upgrade --install` using
# k8s/helm/waddlebot/values-alpha.yaml. That values file renders a complete
# working stack on its own; the only thing this script supplies on top is
# secrets (see Environment below), which are never committed.
#
# Usage:
#   ./scripts/deploy-alpha.sh [OPTIONS]
#
# Options:
#   --build               Build Docker images and push to the local registry (default)
#   --skip-build          Skip Docker build/push, deploy with images already in the registry
#   --tag TAG             Image tag to use (default: alpha)
#   --service SERVICE     Build/push a single service only (the full chart is still deployed)
#   --dry-run             helm upgrade --install --dry-run — render and validate, apply nothing
#   --rollback            helm rollback to the previous release revision
#   --help                Show this help message
#
# Environment:
#   KUBE_CONTEXT    Kubernetes context (default: local-alpha)
#   NAMESPACE       Target namespace (default: waddlebot)
#   APP_HOST        Application hostname (default: waddlebot.localhost.local)
#   HELM_CHART      Path to the Helm chart (default: k8s/helm/waddlebot)
#
#   Required secrets — never defaulted, never committed, never passed as CLI
#   args (see docs/SECRETS_SETUP.md and ~/.claude/rules/critical-rules.md
#   Token & Secret Hygiene). The script fails loudly if any is unset:
#     WADDLEBOT_ALPHA_JWT_SECRET
#     WADDLEBOT_ALPHA_MODULE_SECRET_KEY
#     WADDLEBOT_ALPHA_SERVICE_API_KEY
#     WADDLEBOT_ALPHA_ADMIN_PASSWORD
#     WADDLEBOT_ALPHA_MINIO_ROOT_USER
#     WADDLEBOT_ALPHA_MINIO_ROOT_PASSWORD
#
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

readonly APP_NAME="${APP_NAME:-waddlebot}"
readonly KUBE_CONTEXT="${KUBE_CONTEXT:-local-alpha}"
readonly NAMESPACE="${NAMESPACE:-waddlebot}"
readonly APP_HOST="${APP_HOST:-waddlebot.localhost.local}"
readonly HELM_CHART="${HELM_CHART:-k8s/helm/waddlebot}"

# Local MicroK8s registry root. MUST match global.imageRegistry in
# k8s/helm/waddlebot/values-alpha.yaml -- this script pushes here, the chart
# pulls from here. imagePullPolicy stays IfNotPresent (values-alpha.yaml) --
# never Never -- images are pushed to this registry, not side-loaded via
# `ctr images import`.
readonly REGISTRY="localhost:32000/waddlebot"

# Services to build/push, in build order. Keys are the bare repository name
# appended to REGISTRY (e.g. localhost:32000/waddlebot/hub-api) -- the exact
# convention values-alpha.yaml and templates/_helpers.tpl's
# waddlebot.legacyModuleImage/waddlebot.dbMigrateInitContainer render. Only
# services actually enabled in values-alpha.yaml are listed here; svc-core
# and svc-rtc have no Dockerfile yet and are left on the chart's shared
# base-image skeleton (disabled in alpha).
readonly SERVICE_ORDER=(
    "hub-api"
    "hub-webui"
    "svc-ingest"
    "svc-process"
    "svc-action"
    "svc-presentation"
    "svc-streaming"
    "reputation-module"
    "waddlebot-migrations"
)

# Build context, relative to PROJECT_ROOT. Every service except svc-streaming
# builds from the repo root because its Dockerfile COPYs shared libs/*
# (flask_core, waddle_transports, moderation_module). svc-streaming is a
# self-contained Rust crate (core/svc_streaming/Cargo.toml) with no shared
# libs to pull in -- see core/svc_streaming/Dockerfile.rust's own header.
declare -A SERVICE_CONTEXT=(
    ["hub-api"]="."
    ["hub-webui"]="."
    ["svc-ingest"]="."
    ["svc-process"]="."
    ["svc-action"]="."
    ["svc-presentation"]="."
    ["svc-streaming"]="core/svc_streaming"
    ["reputation-module"]="."
    ["waddlebot-migrations"]="."
)

# Dockerfile path, relative to PROJECT_ROOT (or relative to the context above
# for svc-streaming, which docker build handles identically either way here
# since -f accepts a path relative to CWD, not the context).
declare -A SERVICE_DOCKERFILE=(
    ["hub-api"]="hub_api/Dockerfile"
    ["hub-webui"]="admin/hub_module/Dockerfile.webui"
    ["svc-ingest"]="core/svc_ingest/Dockerfile"
    ["svc-process"]="core/svc_process/Dockerfile"
    ["svc-action"]="core/svc_action/Dockerfile"
    ["svc-presentation"]="core/svc_presentation/Dockerfile"
    ["svc-streaming"]="core/svc_streaming/Dockerfile.rust"
    ["reputation-module"]="core/reputation_module/Dockerfile"
    ["waddlebot-migrations"]="migrations/Dockerfile"
)

# Required secret env vars. Mirrors the five ad-hoc --set overrides the live
# alpha release was actually deployed with; none of these may ever be
# defaulted or committed (critical-rules.md Token & Secret Hygiene).
readonly REQUIRED_SECRET_VARS=(
    "WADDLEBOT_ALPHA_JWT_SECRET"
    "WADDLEBOT_ALPHA_MODULE_SECRET_KEY"
    "WADDLEBOT_ALPHA_SERVICE_API_KEY"
    "WADDLEBOT_ALPHA_ADMIN_PASSWORD"
    "WADDLEBOT_ALPHA_MINIO_ROOT_USER"
    "WADDLEBOT_ALPHA_MINIO_ROOT_PASSWORD"
)

# Defaults
declare TAG="alpha"
declare SERVICE_FILTER=""
declare SKIP_BUILD=false
declare DRY_RUN=false
declare DO_ROLLBACK=false
declare SECRETS_VALUES_FILE=""

# =============================================================================
# Color output helpers
# =============================================================================

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'

print_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# =============================================================================
# kubectl wrapper (always uses --context)
# =============================================================================

kctl() {
    kubectl --context "${KUBE_CONTEXT}" "$@"
}

# =============================================================================
# Secrets file cleanup (always runs -- success, failure, or Ctrl-C)
# =============================================================================

cleanup_secrets_file() {
    if [[ -n "${SECRETS_VALUES_FILE}" && -f "${SECRETS_VALUES_FILE}" ]]; then
        rm -f "${SECRETS_VALUES_FILE}"
    fi
}
trap cleanup_secrets_file EXIT

# =============================================================================
# Prerequisite checks
# =============================================================================

check_prerequisites() {
    print_info "Checking prerequisites..."
    local missing=()

    for cmd in kubectl docker helm; do
        if ! command -v "${cmd}" &>/dev/null; then
            missing+=("${cmd}")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        print_error "Missing required tools: ${missing[*]}"
        exit 1
    fi

    # Verify context exists
    if ! kubectl config get-contexts "${KUBE_CONTEXT}" &>/dev/null; then
        print_error "Kubernetes context '${KUBE_CONTEXT}' not found"
        echo "Available contexts:"
        kubectl config get-contexts --output=name
        exit 1
    fi

    # Verify cluster reachable
    if ! kctl cluster-info &>/dev/null; then
        print_error "Cannot reach cluster via context '${KUBE_CONTEXT}'"
        print_error "Is MicroK8s running? Try: microk8s status"
        exit 1
    fi

    # Verify the Helm chart and its alpha values file exist
    if [[ ! -d "${PROJECT_ROOT}/${HELM_CHART}" ]]; then
        print_error "Helm chart not found: ${HELM_CHART}"
        exit 1
    fi
    if [[ ! -f "${PROJECT_ROOT}/${HELM_CHART}/values-alpha.yaml" ]]; then
        print_error "Missing ${HELM_CHART}/values-alpha.yaml"
        exit 1
    fi

    print_success "All prerequisites satisfied"
}

# =============================================================================
# Required secrets -- env vars only, never CLI args, never a committed
# default (critical-rules.md Token & Secret Hygiene). Fails loudly and lists
# every missing var before doing anything else.
# =============================================================================

check_required_secrets() {
    local missing=()
    local var

    for var in "${REQUIRED_SECRET_VARS[@]}"; do
        if [[ -z "${!var:-}" ]]; then
            missing+=("${var}")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        print_error "Missing required secret environment variable(s):"
        for var in "${missing[@]}"; do
            echo "    ${var}" >&2
        done
        cat >&2 <<'EOF'

These are never defaulted and never committed. Set them before deploying, e.g.:

    export WADDLEBOT_ALPHA_JWT_SECRET="$(openssl rand -hex 32)"
    export WADDLEBOT_ALPHA_MODULE_SECRET_KEY="$(openssl rand -hex 32)"
    export WADDLEBOT_ALPHA_SERVICE_API_KEY="$(openssl rand -hex 32)"
    export WADDLEBOT_ALPHA_ADMIN_PASSWORD="$(openssl rand -base64 24)"
    export WADDLEBOT_ALPHA_MINIO_ROOT_USER="waddlebot-alpha"
    export WADDLEBOT_ALPHA_MINIO_ROOT_PASSWORD="$(openssl rand -hex 16)"

See docs/SECRETS_SETUP.md.
EOF
        exit 1
    fi

    # Write an ephemeral, mode-600 values file so secrets never appear as a
    # CLI arg (ps/shell history) or in Helm's own --set logging. Removed by
    # the trap above on every exit path.
    umask 077
    SECRETS_VALUES_FILE="$(mktemp "${TMPDIR:-/tmp}/waddlebot-alpha-secrets-XXXXXX.yaml")"
    cat > "${SECRETS_VALUES_FILE}" <<EOF
global:
  jwtSecret: "${WADDLEBOT_ALPHA_JWT_SECRET}"
  moduleSecretKey: "${WADDLEBOT_ALPHA_MODULE_SECRET_KEY}"
  serviceApiKey: "${WADDLEBOT_ALPHA_SERVICE_API_KEY}"
  initialAdmin:
    password: "${WADDLEBOT_ALPHA_ADMIN_PASSWORD}"
infrastructure:
  minio:
    rootUser: "${WADDLEBOT_ALPHA_MINIO_ROOT_USER}"
    rootPassword: "${WADDLEBOT_ALPHA_MINIO_ROOT_PASSWORD}"
EOF

    print_success "Required secrets present"
}

# =============================================================================
# Local registry reachability
# =============================================================================

check_registry_reachable() {
    local registry_host="${REGISTRY%%/*}"
    if command -v curl &>/dev/null; then
        if ! curl -sf --max-time 3 "http://${registry_host}/v2/" &>/dev/null; then
            print_error "Local registry not reachable at http://${registry_host}/v2/"
            print_error "Enable it first, e.g.: microk8s enable registry"
            exit 1
        fi
    fi
}

# =============================================================================
# Docker build and push to the local registry
# =============================================================================

build_and_push() {
    local service="$1"
    local tag="$2"
    local context="${PROJECT_ROOT}/${SERVICE_CONTEXT[${service}]}"
    local dockerfile="${PROJECT_ROOT}/${SERVICE_DOCKERFILE[${service}]}"

    if [[ ! -f "${dockerfile}" ]]; then
        print_warning "No Dockerfile found for ${service} (${SERVICE_DOCKERFILE[${service}]}) — skipping"
        return 0
    fi

    local image="${REGISTRY}/${service}:${tag}"

    print_info "Building ${image} (dockerfile=${SERVICE_DOCKERFILE[${service}]})"
    if ! docker build \
        --file "${dockerfile}" \
        --tag "${image}" \
        --label "environment=alpha" \
        --label "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "${context}"; then
        print_error "Failed to build ${service}"
        return 1
    fi

    print_info "Pushing ${image}..."
    if ! docker push "${image}"; then
        print_error "Failed to push ${image}"
        return 1
    fi

    print_success "Built and pushed: ${image}"
}

# =============================================================================
# Helm deployment
# =============================================================================

do_deploy() {
    print_info "Deploying to local MicroK8s cluster via Helm..."
    print_info "  Context:   ${KUBE_CONTEXT}"
    print_info "  Namespace: ${NAMESPACE}"
    print_info "  Chart:     ${HELM_CHART}"
    print_info "  Tag:       ${TAG}"

    local helm_args=(
        upgrade --install "${APP_NAME}" "${PROJECT_ROOT}/${HELM_CHART}"
        --kube-context "${KUBE_CONTEXT}"
        --namespace "${NAMESPACE}"
        --create-namespace
        --values "${PROJECT_ROOT}/${HELM_CHART}/values-alpha.yaml"
        --values "${SECRETS_VALUES_FILE}"
        --set "global.imageTag=${TAG}"
    )

    if [[ "${DRY_RUN}" == "true" ]]; then
        helm_args+=(--dry-run --debug)
    fi

    if ! helm "${helm_args[@]}"; then
        print_error "Helm deployment failed"
        return 1
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        print_success "Dry-run complete — nothing applied"
    else
        print_success "Helm release applied"
    fi
}

# =============================================================================
# Rollout verification
# =============================================================================

wait_for_rollout() {
    print_info "Waiting for deployments to roll out..."

    local deployments
    deployments=$(kctl get deployments -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

    if [[ -z "${deployments}" ]]; then
        print_warning "No deployments found in namespace ${NAMESPACE}"
        return 0
    fi

    local failed=false
    for deploy in ${deployments}; do
        print_info "Waiting for deployment/${deploy}..."
        if ! kctl rollout status "deployment/${deploy}" -n "${NAMESPACE}" --timeout=300s; then
            print_error "Deployment ${deploy} failed to roll out"
            failed=true
        fi
    done

    local statefulsets
    statefulsets=$(kctl get statefulsets -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

    for sts in ${statefulsets}; do
        print_info "Waiting for statefulset/${sts}..."
        if ! kctl rollout status "statefulset/${sts}" -n "${NAMESPACE}" --timeout=300s; then
            print_error "StatefulSet ${sts} failed to roll out"
            failed=true
        fi
    done

    if [[ "${failed}" == "true" ]]; then
        return 1
    fi

    print_success "All workloads rolled out successfully"
}

# =============================================================================
# Show status
# =============================================================================

show_status() {
    echo ""
    print_info "Pod Status:"
    kctl get pods -n "${NAMESPACE}" -o wide
    echo ""
    print_info "Services:"
    kctl get svc -n "${NAMESPACE}"
    echo ""
    print_info "Access URL: https://${APP_HOST}"
    echo ""
    print_info "Quick commands:"
    echo "  Helm status: helm status ${APP_NAME} --kube-context ${KUBE_CONTEXT} -n ${NAMESPACE}"
    echo "  View pods:   kubectl --context ${KUBE_CONTEXT} get pods -n ${NAMESPACE}"
    echo "  View logs:   kubectl --context ${KUBE_CONTEXT} logs -n ${NAMESPACE} -l app.kubernetes.io/instance=${APP_NAME} -f"
    echo "  Describe:    kubectl --context ${KUBE_CONTEXT} describe pods -n ${NAMESPACE}"
}

# =============================================================================
# Rollback
# =============================================================================

do_rollback() {
    print_warning "Rolling back Helm release '${APP_NAME}' in ${NAMESPACE}..."

    if ! helm rollback "${APP_NAME}" --kube-context "${KUBE_CONTEXT}" --namespace "${NAMESPACE}"; then
        print_error "Helm rollback failed"
        return 1
    fi

    print_success "Rollback initiated"
    wait_for_rollout
}

# =============================================================================
# Help
# =============================================================================

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Deploy ${APP_NAME} to the local MicroK8s alpha environment using Helm.

OPTIONS:
    --build               Build images and push to the local registry (default)
    --skip-build          Skip build/push, deploy with existing registry images
    --tag TAG             Image tag (default: alpha)
    --service SERVICE     Build/push a single service only
    --dry-run             helm upgrade --install --dry-run (render + validate, apply nothing)
    --rollback            helm rollback to the previous release revision
    --help                Show this help message

ENVIRONMENT:
    KUBE_CONTEXT:   ${KUBE_CONTEXT}
    NAMESPACE:      ${NAMESPACE}
    APP_HOST:       ${APP_HOST}
    HELM_CHART:     ${HELM_CHART}
    REGISTRY:       ${REGISTRY}

REQUIRED SECRETS (env vars, never defaulted/committed — see docs/SECRETS_SETUP.md):
    WADDLEBOT_ALPHA_JWT_SECRET
    WADDLEBOT_ALPHA_MODULE_SECRET_KEY
    WADDLEBOT_ALPHA_SERVICE_API_KEY
    WADDLEBOT_ALPHA_ADMIN_PASSWORD
    WADDLEBOT_ALPHA_MINIO_ROOT_USER
    WADDLEBOT_ALPHA_MINIO_ROOT_PASSWORD

SERVICES (built/pushed as ${REGISTRY}/<service>:<tag>):
    hub-api                (hub_api/Dockerfile)
    hub-webui              (admin/hub_module/Dockerfile.webui)
    svc-ingest              (core/svc_ingest/Dockerfile)
    svc-process             (core/svc_process/Dockerfile)
    svc-action              (core/svc_action/Dockerfile)
    svc-presentation        (core/svc_presentation/Dockerfile)
    svc-streaming           (core/svc_streaming/Dockerfile.rust)
    reputation-module       (core/reputation_module/Dockerfile)
    waddlebot-migrations    (migrations/Dockerfile)

EXAMPLES:
    # Full build, push, and deploy
    $(basename "$0")

    # Deploy without rebuilding images
    $(basename "$0") --skip-build

    # Build and push only one service, then deploy the full chart
    $(basename "$0") --service hub-api

    # Preview what would change
    $(basename "$0") --skip-build --dry-run

    # Roll back to the previous release revision
    $(basename "$0") --rollback
EOF
}

# =============================================================================
# Main
# =============================================================================

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --build)
                SKIP_BUILD=false
                shift
                ;;
            --skip-build)
                SKIP_BUILD=true
                shift
                ;;
            --tag)
                TAG="$2"
                shift 2
                ;;
            --service)
                SERVICE_FILTER="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --rollback)
                DO_ROLLBACK=true
                shift
                ;;
            --help)
                show_help
                exit 0
                ;;
            *)
                print_error "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done

    echo ""
    print_info "=========================================="
    print_info "  ${APP_NAME} — Alpha Deployment (Helm)"
    print_info "=========================================="
    echo ""

    check_prerequisites

    if [[ "${DO_ROLLBACK}" == "true" ]]; then
        local rollback_rc=0
        do_rollback || rollback_rc=$?
        show_status
        exit "${rollback_rc}"
    fi

    check_required_secrets

    if [[ "${SKIP_BUILD}" != "true" ]]; then
        check_registry_reachable
        print_info "Building and pushing Docker images..."
        for service in "${SERVICE_ORDER[@]}"; do
            if [[ -z "${SERVICE_FILTER}" ]] || [[ "${SERVICE_FILTER}" == "${service}" ]]; then
                build_and_push "${service}" "${TAG}" || {
                    print_error "Failed to build ${service}"
                    exit 1
                }
            fi
        done
    else
        print_info "Skipping build (--skip-build)"
    fi

    do_deploy || exit 1

    if [[ "${DRY_RUN}" != "true" ]]; then
        wait_for_rollout || print_warning "Some workloads did not roll out cleanly"
        show_status
        print_success "Alpha deployment complete!"
    fi
}

main "$@"
