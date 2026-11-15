#!/bin/bash
# Waddles MicroK8s Testing Script
# Alternative to docker-compose for local Kubernetes testing

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
K8S_DIR="$PROJECT_ROOT/k8s"
NAMESPACE="waddlebot"
REGISTRY="localhost:32000"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Wrapper for microk8s commands
mk() {
    if groups | grep -q microk8s; then
        microk8s "$@"
    else
        sg microk8s -c "microk8s $*"
    fi
}

kubectl_cmd() {
    mk kubectl "$@"
}

usage() {
    echo "Waddles MicroK8s Testing Script"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  setup       - Enable required microk8s addons"
    echo "  build       - Build and push Docker images to local registry"
    echo "  deploy      - Deploy Waddles stack to microk8s"
    echo "  status      - Show deployment status"
    echo "  logs <pod>  - Show logs for a pod (e.g., logs hub)"
    echo "  shell <pod> - Open shell in a pod"
    echo "  teardown    - Remove all Waddles resources"
    echo "  restart     - Teardown and redeploy"
    echo "  all         - Run setup, build, and deploy"
    echo ""
    echo "Examples:"
    echo "  $0 all              # Full setup and deploy"
    echo "  $0 logs hub         # View hub logs"
    echo "  $0 status           # Check pod status"
}

install_microk8s() {
    log_info "Checking if MicroK8s is installed..."

    if command -v microk8s &>/dev/null; then
        log_success "MicroK8s is already installed"
        return 0
    fi

    log_info "Installing MicroK8s via snap..."
    if ! command -v snap &>/dev/null; then
        log_error "Snap is not installed. Please install snapd first:"
        echo "  sudo apt update && sudo apt install -y snapd"
        exit 1
    fi

    sudo snap install microk8s --classic --channel=1.28/stable

    log_info "Adding current user to microk8s group..."
    sudo usermod -a -G microk8s "$USER"
    sudo chown -f -R "$USER" ~/.kube 2>/dev/null || true

    log_warn "You may need to log out and back in for group changes to take effect"
    log_warn "Or run: newgrp microk8s"

    log_info "Waiting for MicroK8s to be ready..."
    sudo microk8s status --wait-ready

    log_success "MicroK8s installed successfully"
}

check_microk8s() {
    log_info "Checking microk8s status..."

    # Check if microk8s is installed
    if ! command -v microk8s &>/dev/null; then
        log_warn "MicroK8s is not installed"
        read -p "Would you like to install it now? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            install_microk8s
        else
            log_error "MicroK8s is required. Install with: sudo snap install microk8s --classic"
            exit 1
        fi
    fi

    if ! mk status &>/dev/null; then
        log_warn "MicroK8s is not running. Starting..."
        mk start
        sleep 5
    fi
    log_success "MicroK8s is running"
}

setup_addons() {
    log_info "Enabling required microk8s addons..."

    # Enable addons
    mk enable dns hostpath-storage registry ingress

    # Wait for registry to be ready
    log_info "Waiting for registry to be ready..."
    local retries=30
    while [ $retries -gt 0 ]; do
        if curl -s http://localhost:32000/v2/ &>/dev/null; then
            log_success "Registry is ready at localhost:32000"
            return 0
        fi
        retries=$((retries - 1))
        sleep 2
    done

    log_warn "Registry may not be fully ready, but continuing..."
}

build_images() {
    log_info "Building Docker images..."
    cd "$PROJECT_ROOT"

    # Build images
    local images=(
        "hub-api:hub_api/Dockerfile:."
        "hub-webui:admin/hub_module/Dockerfile.webui:."
    )

    for img_spec in "${images[@]}"; do
        IFS=':' read -r name dockerfile context <<< "$img_spec"
        log_info "Building waddlebot/$name..."
        docker build -f "$dockerfile" -t "waddlebot/$name:latest" "$context"
        docker tag "waddlebot/$name:latest" "$REGISTRY/waddlebot/$name:latest"
    done

    log_success "All images built"
}

push_images() {
    log_info "Pushing images to local registry..."

    local images=(
        "waddlebot/hub-api"
        "waddlebot/hub-webui"
    )

    for img in "${images[@]}"; do
        log_info "Pushing $img..."
        docker push "$REGISTRY/$img:latest"
    done

    # Also push OpenWhisk standalone
    log_info "Pushing openwhisk/standalone..."
    docker pull openwhisk/standalone:nightly 2>/dev/null || true
    docker tag openwhisk/standalone:nightly "$REGISTRY/openwhisk/standalone:nightly"
    docker push "$REGISTRY/openwhisk/standalone:nightly"

    log_success "All images pushed to registry"
}

create_namespace() {
    log_info "Creating namespace $NAMESPACE..."
    kubectl_cmd create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl_cmd apply -f -
}

deploy_infrastructure() {
    log_info "Deploying infrastructure (PostgreSQL, Redis)..."
    kubectl_cmd apply -f "$K8S_DIR/infrastructure.yaml" -n "$NAMESPACE"

    log_info "Waiting for infrastructure to be ready..."
    kubectl_cmd wait --for=condition=ready pod -l app=postgres -n "$NAMESPACE" --timeout=120s
    kubectl_cmd wait --for=condition=ready pod -l app=redis -n "$NAMESPACE" --timeout=60s
    log_success "Infrastructure is ready"
}

deploy_openwhisk() {
    log_info "Deploying OpenWhisk standalone..."
    kubectl_cmd apply -f "$K8S_DIR/openwhisk.yaml" -n "$NAMESPACE"

    log_info "Waiting for OpenWhisk to be ready (this may take a minute)..."
    kubectl_cmd wait --for=condition=ready pod -l app=openwhisk -n "$NAMESPACE" --timeout=180s || \
        log_warn "OpenWhisk may still be starting up"
    log_success "OpenWhisk deployed"
}

deploy_modules() {
    log_info "Deploying Waddles modules..."
    kubectl_cmd apply -f "$K8S_DIR/modules.yaml" -n "$NAMESPACE"

    log_info "Waiting for modules to be ready..."
    kubectl_cmd wait --for=condition=ready pod -l tier=action -n "$NAMESPACE" --timeout=120s || true
    kubectl_cmd wait --for=condition=ready pod -l app=hub -n "$NAMESPACE" --timeout=120s || true
    log_success "Modules deployed"
}

show_status() {
    echo ""
    log_info "=== Waddles Deployment Status ==="
    echo ""

    log_info "Pods:"
    kubectl_cmd get pods -n "$NAMESPACE" -o wide
    echo ""

    log_info "Services:"
    kubectl_cmd get svc -n "$NAMESPACE"
    echo ""

    log_info "Access URLs:"
    local hub_port=$(kubectl_cmd get svc hub -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "N/A")
    local openwhisk_port=$(kubectl_cmd get svc openwhisk -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || echo "N/A")

    echo "  Hub:       http://localhost:$hub_port"
    echo "  OpenWhisk: http://localhost:$openwhisk_port"
}

show_logs() {
    local pod_name="$1"
    if [ -z "$pod_name" ]; then
        log_error "Please specify a pod name"
        kubectl_cmd get pods -n "$NAMESPACE"
        exit 1
    fi

    kubectl_cmd logs -f -l "app=$pod_name" -n "$NAMESPACE" --all-containers
}

open_shell() {
    local pod_name="$1"
    if [ -z "$pod_name" ]; then
        log_error "Please specify a pod name"
        kubectl_cmd get pods -n "$NAMESPACE"
        exit 1
    fi

    local pod=$(kubectl_cmd get pod -l "app=$pod_name" -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}')
    kubectl_cmd exec -it "$pod" -n "$NAMESPACE" -- /bin/sh
}

teardown() {
    log_warn "Removing all Waddles resources..."
    kubectl_cmd delete namespace "$NAMESPACE" --ignore-not-found
    log_success "Teardown complete"
}

# Main command handler
case "${1:-}" in
    setup)
        check_microk8s
        setup_addons
        ;;
    build)
        build_images
        push_images
        ;;
    deploy)
        check_microk8s
        create_namespace
        deploy_infrastructure
        deploy_openwhisk
        deploy_modules
        show_status
        ;;
    status)
        check_microk8s
        show_status
        ;;
    logs)
        show_logs "$2"
        ;;
    shell)
        open_shell "$2"
        ;;
    teardown)
        check_microk8s
        teardown
        ;;
    restart)
        check_microk8s
        teardown
        sleep 5
        create_namespace
        deploy_infrastructure
        deploy_openwhisk
        deploy_modules
        show_status
        ;;
    all)
        check_microk8s
        setup_addons
        build_images
        push_images
        create_namespace
        deploy_infrastructure
        deploy_openwhisk
        deploy_modules
        show_status
        ;;
    *)
        usage
        ;;
esac
