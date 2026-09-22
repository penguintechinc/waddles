#!/bin/bash
# WaddleBot Beta Cluster Deployment Script
# Standardized deployment with CLI argument parsing
# Builds images, pushes to registry, and deploys to beta K8s cluster

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
REGISTRY="ghcr.io/penguintechinc/waddlebot"
NAMESPACE="waddlebot"
HELM_CHART="${PROJECT_ROOT}/k8s/helm/waddlebot"
KUBE_CONTEXT="dal2-beta"

# Services to build and push — v2.2.x consolidated architecture (22 containers)
declare -A SERVICES=(
    # Standalone hub services (keep as-is)
    [hub-api]="${PROJECT_ROOT}/admin/hub_module/Dockerfile"
    [hub-webui]="${PROJECT_ROOT}/admin/hub_module/Dockerfile.webui"
    # Router & AI researcher (processing/core)
    [router]="${PROJECT_ROOT}/processing/router_module/Dockerfile"
    [ai-researcher]="${PROJECT_ROOT}/core/ai_researcher_module/Dockerfile"
    # New consolidated core services (services/ directory)
    [core-data]="${PROJECT_ROOT}/services/core-data/Dockerfile"
    [core-identity]="${PROJECT_ROOT}/services/core-identity/Dockerfile"
    [core-community]="${PROJECT_ROOT}/services/core-community/Dockerfile"
    # Trigger services (separate + consolidated)
    [trigger-discord]="${PROJECT_ROOT}/trigger/receiver/discord_module/Dockerfile"
    [trigger-streaming]="${PROJECT_ROOT}/services/trigger-streaming/Dockerfile"
    [trigger-webhooks]="${PROJECT_ROOT}/services/trigger-webhooks/Dockerfile"
    # Action services (separate + consolidated)
    [action-discord]="${PROJECT_ROOT}/action/pushing/discord_action_module/Dockerfile"
    [action-platforms]="${PROJECT_ROOT}/services/action-platforms/Dockerfile"
    [action-serverless]="${PROJECT_ROOT}/services/action-serverless/Dockerfile"
    # Interactive services (separate + consolidated)
    [interactive-ai]="${PROJECT_ROOT}/action/interactive/ai_interaction_module/Dockerfile"
    [interactive-loyalty]="${PROJECT_ROOT}/action/interactive/loyalty_interaction_module/Dockerfile"
    [interactive-social]="${PROJECT_ROOT}/services/interactive-social/Dockerfile"
    [interactive-gaming]="${PROJECT_ROOT}/services/interactive-gaming/Dockerfile"
    [interactive-media]="${PROJECT_ROOT}/services/interactive-media/Dockerfile"
    [interactive-productivity]="${PROJECT_ROOT}/services/interactive-productivity/Dockerfile"
    # Admin & migrations
    [marketplace]="${PROJECT_ROOT}/admin/marketplace_module/Dockerfile"
    [migrations]="${PROJECT_ROOT}/migrations/Dockerfile"
)

# Deployment tool by environment:
#   Beta  (dal2-beta)        → Helm (this script)
#   Alpha (local MicroK8s)   → Helm (scripts/deploy-alpha.sh) -- Kustomize is deprecated,
#                              see k8s/kustomize/ for the (unused) legacy overlays.

# Default values — unique epoch tag per skill (never reuse beta-latest)
TAG="${TAG:-beta-$(date +%s)}"
DEPLOY_METHOD="helm"
SERVICES_TO_BUILD=()
SKIP_BUILD=false
BUILD_PARALLEL=6
DRY_RUN=false
DO_ROLLBACK=false
SHOW_HELP=false

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

log_section() {
    echo ""
    echo -e "${CYAN}===================================================${NC}"
    echo -e "${CYAN}$*${NC}"
    echo -e "${CYAN}===================================================${NC}"
    echo ""
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Print usage
print_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

OPTIONS:
  --tag TAG              Image tag to build and deploy (default: beta-<epoch>)
  --method METHOD        Deployment method: helm or kustomize (default: helm)
  --service SERVICE      Build and deploy specific service (can be used multiple times)
  --parallel N           Max parallel builds (default: 6, set to 1 for sequential)
  --skip-build           Skip building images, only deploy (requires pre-built images)
  --dry-run              Show what would be done without making changes
  --rollback             Rollback to previous deployment
  --help                 Show this help message

SERVICES (v2.2.x — 22 total):
  hub-api, hub-webui
  router, ai-researcher
  core-data, core-identity, core-community
  trigger-discord, trigger-streaming, trigger-webhooks
  action-discord, action-platforms, action-serverless
  interactive-ai, interactive-loyalty, interactive-social
  interactive-gaming, interactive-media, interactive-productivity
  marketplace, waddlebot-migrations

EXAMPLES:
  # Deploy all services with a specific tag
  $0 --tag v1.2.3

  # Deploy only hub-api
  $0 --service hub-api

  # Deploy multiple services
  $0 --service hub-api --service core-engagement

  # Skip build and deploy pre-built images
  $0 --skip-build --tag my-custom-tag

  # Rollback to previous version
  $0 --rollback

  # Dry run to see what would be deployed
  $0 --dry-run

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)
            TAG="$2"
            shift 2
            ;;
        --method)
            DEPLOY_METHOD="$2"
            if [[ "$DEPLOY_METHOD" != "helm" && "$DEPLOY_METHOD" != "kustomize" ]]; then
                log_error "Invalid deployment method: $DEPLOY_METHOD (must be 'helm' or 'kustomize')"
                exit 1
            fi
            shift 2
            ;;
        --service)
            SERVICES_TO_BUILD+=("$2")
            shift 2
            ;;
        --parallel)
            BUILD_PARALLEL="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
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
            print_usage
            ;;
        *)
            log_error "Unknown option: $1"
            print_usage
            ;;
    esac
done

# If no specific services selected, build all
if [ ${#SERVICES_TO_BUILD[@]} -eq 0 ] && [ "$SKIP_BUILD" = false ]; then
    SERVICES_TO_BUILD=("${!SERVICES[@]}")
elif [ ${#SERVICES_TO_BUILD[@]} -gt 0 ] && [ "$SKIP_BUILD" = false ]; then
    # Partial builds: always include waddlebot-migrations since it's an init container
    # shared by all pods. Without it, pods with the new global.imageTag can't start.
    if [[ ! " ${SERVICES_TO_BUILD[*]} " =~ " waddlebot-migrations " ]]; then
        SERVICES_TO_BUILD+=("waddlebot-migrations")
    fi
fi

# Check prerequisites
check_prerequisites() {
    log_section "Checking Prerequisites"

    if ! command_exists docker; then
        log_error "Docker is not installed or not in PATH"
        exit 1
    fi
    log_success "Docker found: $(docker --version)"

    if ! command_exists kubectl; then
        log_error "kubectl is not installed or not in PATH"
        exit 1
    fi
    log_success "kubectl found: $(kubectl version --client --short 2>/dev/null || echo 'installed')"

    if [ "$DEPLOY_METHOD" = "helm" ]; then
        if ! command_exists helm; then
            log_error "Helm is not installed or not in PATH (required for --method helm)"
            exit 1
        fi
        log_success "Helm found: $(helm version --short 2>/dev/null || echo 'installed')"
    fi

    if [ "$DEPLOY_METHOD" = "kustomize" ]; then
        if ! command_exists kustomize; then
            log_error "kustomize is not installed or not in PATH (required for --method kustomize)"
            exit 1
        fi
        log_success "kustomize found: $(kustomize version 2>/dev/null || echo 'installed')"
    fi

    # Check if we're in the right directory
    if [ ! -f "${PROJECT_ROOT}/docker-compose.yml" ] || [ ! -d "${PROJECT_ROOT}/admin/hub_module" ]; then
        log_error "This script must be run from the WaddleBot root directory"
        exit 1
    fi
    log_success "WaddleBot project directory verified"

    # Verify kubectl context
    current_context=$(kubectl config current-context 2>/dev/null || echo "")
    if [ -z "$current_context" ]; then
        log_warning "No Kubernetes context set, using default"
    else
        log_success "Kubernetes context: $current_context"
    fi

    # Log in to ghcr.io for image push
    if [ "$SKIP_BUILD" = false ]; then
        if [ -f "$HOME/code/.gh-token" ]; then
            read -r _GHCR_TOKEN < <(sed -n '4p' "$HOME/code/.gh-token")
            echo "$_GHCR_TOKEN" | docker login ghcr.io -u penguintechinc --password-stdin
            unset _GHCR_TOKEN
            log_success "Logged in to ghcr.io"
        else
            log_error "~/code/.gh-token not found — cannot authenticate with ghcr.io"
            exit 1
        fi
    fi
}

# Build and push a single image — runs in a subshell for parallel execution.
# Logs to a per-service temp file; prints atomically when done.
_build_and_push_one() {
    local service="$1"
    local dockerfile="$2"
    local log_file="$3"

    {
        echo "[START] ${service}"

        # Build locally (never use buildx --push against beta registry)
        if ! docker build \
            -f "${dockerfile}" \
            -t "waddlebot-${service}:latest" \
            "${PROJECT_ROOT}" 2>&1; then
            echo "[FAIL] ${service}: docker build failed"
            exit 1
        fi
        echo "[BUILT] ${service}"

        # Tag for registry — both versioned and latest
        docker tag "waddlebot-${service}:latest" "${REGISTRY}/${service}:${TAG}" 2>&1
        docker tag "waddlebot-${service}:latest" "${REGISTRY}/${service}:latest" 2>&1
        echo "[TAGGED] ${service} → ${REGISTRY}/${service}:${TAG} + :latest"

        # Push both tags (separate step — never use buildx --push)
        if ! docker push "${REGISTRY}/${service}:${TAG}" 2>&1; then
            echo "[FAIL] ${service}: docker push failed"
            exit 1
        fi
        if ! docker push "${REGISTRY}/${service}:latest" 2>&1; then
            echo "[FAIL] ${service}: docker push :latest failed"
            exit 1
        fi
        echo "[DONE] ${service}"
    } > "${log_file}" 2>&1
}

# Build and push images — parallel with MAX_PARALLEL concurrency limit
build_and_push_images() {
    if [ "$SKIP_BUILD" = true ]; then
        log_section "Skipping Image Build (--skip-build flag set)"
        return 0
    fi

    log_section "Building and Pushing Docker Images"

    # Validate all requested services first
    for service in "${SERVICES_TO_BUILD[@]}"; do
        if [ ! -v "SERVICES[$service]" ]; then
            log_error "Unknown service: $service"
            exit 1
        fi
    done

    if [ "$DRY_RUN" = true ]; then
        for service in "${SERVICES_TO_BUILD[@]}"; do
            log_info "[DRY-RUN] build+push ${service} → ${REGISTRY}/${service}:${TAG}"
        done
        return 0
    fi

    local MAX_PARALLEL="${BUILD_PARALLEL:-6}"
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    local -a pids=()
    local -a pid_services=()
    local failed_services=()
    local active=0

    log_info "Building ${#SERVICES_TO_BUILD[@]} services (up to ${MAX_PARALLEL} in parallel)..."
    echo ""

    # Process queue — launch up to MAX_PARALLEL jobs at once
    local i=0
    local total="${#SERVICES_TO_BUILD[@]}"

    while [ $i -lt $total ] || [ ${#pids[@]} -gt 0 ]; do
        # Launch new jobs while under the concurrency limit and queue remains
        while [ $i -lt $total ] && [ ${#pids[@]} -lt $MAX_PARALLEL ]; do
            local service="${SERVICES_TO_BUILD[$i]}"
            local dockerfile="${SERVICES[$service]}"
            local log_file="${tmp_dir}/${service}.log"

            log_info "Queuing ${service}..."
            _build_and_push_one "${service}" "${dockerfile}" "${log_file}" &
            pids+=("$!")
            pid_services+=("${service}")
            (( i++ )) || true
        done

        # Wait for any one job to finish
        if [ ${#pids[@]} -gt 0 ]; then
            local finished_pid finished_idx finished_service
            # Poll for completed jobs
            finished_idx=-1
            for idx in "${!pids[@]}"; do
                if ! kill -0 "${pids[$idx]}" 2>/dev/null; then
                    finished_idx=$idx
                    break
                fi
            done

            if [ $finished_idx -ge 0 ]; then
                finished_pid="${pids[$finished_idx]}"
                finished_service="${pid_services[$finished_idx]}"
                local log_file="${tmp_dir}/${finished_service}.log"

                # Collect exit code
                wait "${finished_pid}" 2>/dev/null
                local exit_code=$?

                # Print the buffered log atomically
                echo "--- ${finished_service} ---"
                cat "${log_file}" 2>/dev/null || true
                echo ""

                if [ $exit_code -ne 0 ]; then
                    log_error "${finished_service} FAILED (exit ${exit_code})"
                    failed_services+=("${finished_service}")
                else
                    log_success "${finished_service} pushed successfully"
                fi

                # Remove from active arrays
                unset 'pids[$finished_idx]'
                unset 'pid_services[$finished_idx]'
                pids=("${pids[@]}")
                pid_services=("${pid_services[@]}")
            else
                # No job finished yet — short sleep to avoid busy-wait
                sleep 1
            fi
        fi
    done

    rm -rf "${tmp_dir}"

    if [ ${#failed_services[@]} -gt 0 ]; then
        log_error "The following services failed to build/push:"
        for svc in "${failed_services[@]}"; do
            log_error "  - ${svc}"
        done
        exit 1
    fi

    log_success "All ${total} services built and pushed successfully"
}

# Deploy via Helm
do_deploy() {
    log_section "Deploying to Beta Cluster with Helm"

    # Create namespace if it doesn't exist
    log_info "Checking if namespace ${NAMESPACE} exists..."
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] kubectl --context ${KUBE_CONTEXT} get namespace ${NAMESPACE}"
    else
        if ! kubectl --context "${KUBE_CONTEXT}" get namespace "${NAMESPACE}" &>/dev/null; then
            log_warning "Namespace ${NAMESPACE} does not exist, creating..."
            kubectl --context "${KUBE_CONTEXT}" create namespace "${NAMESPACE}" || true
            log_success "Namespace ${NAMESPACE} created"
        else
            log_success "Namespace ${NAMESPACE} already exists"
        fi
    fi

    # Recover stuck Helm release (pending-install/pending-upgrade/pending-rollback)
    if [ "$DRY_RUN" = false ]; then
        RELEASE_STATUS=$(helm --kube-context "${KUBE_CONTEXT}" status waddlebot -n "${NAMESPACE}" -o json 2>/dev/null | grep -o '"status":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")
        if [[ "$RELEASE_STATUS" == pending-* ]]; then
            log_warning "Helm release stuck in '${RELEASE_STATUS}' — rolling back to clear lock..."
            LAST_GOOD=$(helm --kube-context "${KUBE_CONTEXT}" history waddlebot -n "${NAMESPACE}" -o json 2>/dev/null \
                | python3 -c "import sys,json; revs=[r for r in json.load(sys.stdin) if r['status'] in ('deployed','failed')]; print(revs[-1]['revision'] if revs else '')" 2>/dev/null || echo "")
            if [ -n "$LAST_GOOD" ]; then
                helm --kube-context "${KUBE_CONTEXT}" rollback waddlebot "$LAST_GOOD" -n "${NAMESPACE}" --timeout 2m 2>/dev/null || true
                log_success "Rolled back to revision ${LAST_GOOD}, lock cleared"
            else
                log_warning "No previous revision found — uninstalling stuck release..."
                helm --kube-context "${KUBE_CONTEXT}" uninstall waddlebot -n "${NAMESPACE}" --no-hooks 2>/dev/null || true
                log_success "Uninstalled stuck release, will do fresh install"
            fi
        fi
    fi

    # Deploy/Upgrade Helm chart
    log_info "Deploying WaddleBot to beta cluster..."

    helm_cmd="helm upgrade waddlebot ${HELM_CHART} \
        --install \
        --kube-context ${KUBE_CONTEXT} \
        --namespace ${NAMESPACE} \
        -f ${HELM_CHART}/values.yaml \
        -f ${HELM_CHART}/values-beta.yaml \
        --set global.imageTag=${TAG} \
        --force-conflicts \
        --timeout 10m \
        --wait"

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] ${helm_cmd}"
    else
        if ! eval "${helm_cmd}"; then
            log_error "Helm deployment failed"
            exit 1
        fi
        log_success "Helm deployment successful"
    fi
}

# Restart deployments to pick up new images
verify_deployment() {
    log_section "Verifying Deployment"

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would restart deployments and verify rollout"
        return 0
    fi

    # Force restart hub services to pull new images
    log_info "Restarting hub-api deployment..."
    kubectl --context "${KUBE_CONTEXT}" rollout restart deployment waddlebot-hub-api -n "${NAMESPACE}" 2>/dev/null || log_warning "hub-api deployment not found, skipping restart"

    log_info "Restarting hub-webui deployment..."
    kubectl --context "${KUBE_CONTEXT}" rollout restart deployment waddlebot-hub-webui -n "${NAMESPACE}" 2>/dev/null || log_warning "hub-webui deployment not found, skipping restart"

    # Wait for rollout to complete
    log_info "Waiting for hub-api rollout to complete..."
    kubectl --context "${KUBE_CONTEXT}" rollout status deployment waddlebot-hub-api -n "${NAMESPACE}" --timeout=5m 2>/dev/null || log_warning "hub-api rollout status unavailable"

    log_info "Waiting for hub-webui rollout to complete..."
    kubectl --context "${KUBE_CONTEXT}" rollout status deployment waddlebot-hub-webui -n "${NAMESPACE}" --timeout=5m 2>/dev/null || log_warning "hub-webui rollout status unavailable"

    # Verify pods picked up the new image
    log_info "Verifying pods are running the expected image tag (${TAG})..."
    echo ""
    VERIFY_FAILED=false
    for svc in hub-api hub-webui; do
        POD_IMAGE=$(kubectl --context "${KUBE_CONTEXT}" get pods -n "${NAMESPACE}" \
            -l "app.kubernetes.io/name=waddlebot-${svc}" \
            -o jsonpath='{.items[0].spec.containers[0].image}' 2>/dev/null || echo "")
        if [ -z "${POD_IMAGE}" ]; then
            log_warning "${svc}: no pods found"
            VERIFY_FAILED=true
        elif echo "${POD_IMAGE}" | grep -q "${TAG}"; then
            log_success "${svc} running: ${POD_IMAGE}"
        else
            log_warning "${svc} image mismatch! Expected tag ${TAG}, got: ${POD_IMAGE}"
            VERIFY_FAILED=true
        fi
    done
    if [ "$VERIFY_FAILED" = true ]; then
        log_warning "Some pods may not have picked up the new image. Check manually:"
        log_warning "  kubectl --context ${KUBE_CONTEXT} get pods -n ${NAMESPACE} -o wide"
    fi
    echo ""

    # Display deployment status
    log_info "Deployment Status:"
    echo ""
    kubectl --context "${KUBE_CONTEXT}" get pods -n "${NAMESPACE}" | grep -E "hub-api|hub-webui" || echo "No hub pods found"
    echo ""

    # Display ingress information
    log_info "Ingress Information:"
    echo ""
    kubectl --context "${KUBE_CONTEXT}" get ingress -n "${NAMESPACE}" || echo "No ingress found"
    echo ""
}

# Deploy via Kustomize
do_deploy_kustomize() {
    log_section "Deploying to Beta Cluster with Kustomize"

    local OVERLAY_DIR="${PROJECT_ROOT}/k8s/kustomize/overlays/beta"

    # Create namespace if it doesn't exist
    log_info "Checking if namespace ${NAMESPACE} exists..."
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] kubectl --context ${KUBE_CONTEXT} get namespace ${NAMESPACE}"
    else
        if ! kubectl --context "${KUBE_CONTEXT}" get namespace "${NAMESPACE}" &>/dev/null; then
            log_warning "Namespace ${NAMESPACE} does not exist, creating..."
            kubectl --context "${KUBE_CONTEXT}" create namespace "${NAMESPACE}" || true
            log_success "Namespace ${NAMESPACE} created"
        else
            log_success "Namespace ${NAMESPACE} already exists"
        fi
    fi

    # Set image tags in overlay using kustomize edit
    log_info "Setting image tags to ${TAG} in kustomize overlay..."
    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would set all image tags to ${TAG} in ${OVERLAY_DIR}"
    else
        pushd "${OVERLAY_DIR}" > /dev/null
        for svc in "${!SERVICES[@]}"; do
            kustomize edit set image "${svc}=${REGISTRY}/${svc}:${TAG}"
        done
        popd > /dev/null
        log_success "Image tags set to ${TAG}"
    fi

    # Apply kustomize overlay
    log_info "Applying kustomize overlay..."
    kustomize_cmd="kustomize build ${OVERLAY_DIR} | kubectl --context ${KUBE_CONTEXT} apply -n ${NAMESPACE} -f -"

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] ${kustomize_cmd}"
        log_info "[DRY-RUN] kustomize build output:"
        kustomize build "${OVERLAY_DIR}" | head -50
    else
        if ! eval "${kustomize_cmd}"; then
            log_error "Kustomize deployment failed"
            exit 1
        fi
        log_success "Kustomize deployment successful"
    fi
}

# Rollback to previous deployment (Helm)
do_rollback() {
    log_section "Rolling Back Deployment (Helm)"

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would rollback to previous release"
        return 0
    fi

    log_info "Rolling back Helm release..."
    if helm --kube-context "${KUBE_CONTEXT}" rollback waddlebot -n "${NAMESPACE}"; then
        log_success "Rollback successful"
    else
        log_error "Rollback failed"
        exit 1
    fi

    verify_deployment
}

# Rollback to previous deployment (Kustomize)
do_rollback_kustomize() {
    log_section "Rolling Back Deployment (Kustomize)"

    if [ "$DRY_RUN" = true ]; then
        log_info "[DRY-RUN] Would rollback all deployments to previous revision"
        return 0
    fi

    log_info "Rolling back all deployments..."
    local deployments
    deployments=$(kubectl --context "${KUBE_CONTEXT}" get deployments -n "${NAMESPACE}" -o name 2>/dev/null)

    if [ -z "$deployments" ]; then
        log_error "No deployments found in namespace ${NAMESPACE}"
        exit 1
    fi

    for deploy in $deployments; do
        log_info "Rolling back ${deploy}..."
        if kubectl --context "${KUBE_CONTEXT}" rollout undo "${deploy}" -n "${NAMESPACE}"; then
            log_success "${deploy} rolled back"
        else
            log_warning "Failed to rollback ${deploy}"
        fi
    done

    verify_deployment
}

# Main execution
main() {
    log_section "WaddleBot Beta Deployment Script"
    log_info "Tag: ${TAG}"
    log_info "Method: ${DEPLOY_METHOD}"
    log_info "Namespace: ${NAMESPACE}"
    log_info "Kube Context: ${KUBE_CONTEXT}"
    if [ "$DRY_RUN" = true ]; then
        log_warning "Running in DRY-RUN mode - no changes will be made"
    fi

    check_prerequisites

    if [ "$DO_ROLLBACK" = true ]; then
        if [ "$DEPLOY_METHOD" = "kustomize" ]; then
            do_rollback_kustomize
        else
            do_rollback
        fi
    else
        build_and_push_images
        if [ "$DEPLOY_METHOD" = "kustomize" ]; then
            do_deploy_kustomize
        else
            do_deploy
        fi
        verify_deployment
    fi

    log_section "Deployment Complete"
    log_success "==================================="
    log_success "Beta deployment completed!"
    log_success "==================================="
    log_info "WebUI: https://waddlebot.penguintech.cloud/"
    log_info "API: https://waddlebot.penguintech.cloud/api"
    echo ""
    log_info "To view logs:"
    log_info "  kubectl --context ${KUBE_CONTEXT} logs -f deployment/waddlebot-hub-api -n ${NAMESPACE}"
    log_info "  kubectl --context ${KUBE_CONTEXT} logs -f deployment/waddlebot-hub-webui -n ${NAMESPACE}"
    echo ""
    log_info "To check pod status:"
    log_info "  kubectl --context ${KUBE_CONTEXT} get pods -n ${NAMESPACE} -l app.kubernetes.io/component=hub"
    log_info "  kubectl --context ${KUBE_CONTEXT} get pods -n ${NAMESPACE} -l app.kubernetes.io/component=hub-webui"
    echo ""
}

# Run main function
main
