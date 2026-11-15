#!/bin/bash
#
# WaddleBot MicroK8s Installation Script
#
# This script automates the deployment of WaddleBot to a MicroK8s cluster.
# It handles cluster setup, image building, registry configuration, and deployment.
#
# Usage:
#   ./install-microk8s.sh [OPTIONS]
#
# Options:
#   --helm              Use Helm chart for deployment (default)
#   --manifests         Use raw Kubernetes manifests for deployment
#   --build-images      Build and push Docker images to cluster registry
#   --skip-build        Skip image building (use existing images)
#   --skip-setup        Skip MicroK8s setup and addon enabling
#   --namespace NAME    Use custom namespace (default: waddlebot)
#   --uninstall         Uninstall WaddleBot from the cluster
#   --help              Show this help message
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default configuration
DEPLOYMENT_METHOD="helm"
BUILD_IMAGES="false"
SKIP_SETUP="false"
NAMESPACE="waddlebot"
UNINSTALL="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# MicroK8s configuration
MICROK8S_REGISTRY="localhost:32000"
IMAGE_PREFIX="waddlebot"

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

show_help() {
    grep '^#' "$0" | grep -v '#!/bin/bash' | sed 's/^# //' | sed 's/^#//'
    exit 0
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check if running on Linux
    if [[ "$OSTYPE" != "linux-gnu"* ]]; then
        log_error "This script is designed for Linux systems only"
        exit 1
    fi

    # Check if snap is installed
    if ! command -v snap &> /dev/null; then
        log_error "snap is required but not installed. Please install snapd first."
        exit 1
    fi

    # Check if MicroK8s is installed
    if ! command -v microk8s &> /dev/null; then
        log_warning "MicroK8s is not installed. Installing now..."
        sudo snap install microk8s --classic --channel=1.28/stable
        sudo usermod -a -G microk8s $USER
        sudo chown -f -R $USER ~/.kube

        log_warning "You may need to log out and back in for group changes to take effect."
        log_warning "Alternatively, run: newgrp microk8s"
    fi

    # Check if Helm is installed (if using Helm deployment)
    if [[ "$DEPLOYMENT_METHOD" == "helm" ]]; then
        if ! command -v helm &> /dev/null; then
            log_error "Helm is required but not installed. Please install Helm 3.x first."
            log_info "Install with: snap install helm --classic"
            exit 1
        fi

        # Verify Helm version 3.x
        HELM_VERSION=$(helm version --short | grep -oP 'v\K[0-9]+')
        if [[ "$HELM_VERSION" -lt 3 ]]; then
            log_error "Helm 3.x or higher is required. Found version: $(helm version --short)"
            exit 1
        fi
    fi

    # Check if Docker is installed (if building images)
    if [[ "$BUILD_IMAGES" == "true" ]]; then
        if ! command -v docker &> /dev/null; then
            log_error "Docker is required for building images but not installed."
            exit 1
        fi
    fi

    log_success "Prerequisites check passed"
}

setup_microk8s() {
    if [[ "$SKIP_SETUP" == "true" ]]; then
        log_info "Skipping MicroK8s setup (--skip-setup enabled)"
        return
    fi

    log_info "Setting up MicroK8s cluster..."

    # Wait for MicroK8s to be ready
    log_info "Waiting for MicroK8s to be ready..."
    microk8s status --wait-ready

    # Enable required addons
    log_info "Enabling MicroK8s addons..."
    microk8s enable dns storage registry ingress

    # Wait for addons to be ready
    log_info "Waiting for addons to be ready..."
    sleep 10
    microk8s kubectl wait --for=condition=ready pod -l k8s-app=kube-dns -n kube-system --timeout=300s

    # Create kubectl alias for convenience
    if ! grep -q "alias kubectl='microk8s kubectl'" ~/.bashrc; then
        echo "alias kubectl='microk8s kubectl'" >> ~/.bashrc
        log_info "Added kubectl alias to ~/.bashrc"
    fi

    log_success "MicroK8s setup complete"
}

build_and_push_images() {
    if [[ "$BUILD_IMAGES" != "true" ]]; then
        log_info "Skipping image build (use --build-images to enable)"
        return
    fi

    log_info "Building and pushing Docker images to MicroK8s registry..."

    # Legacy per-module Python images were retired; live images are built by CI / scripts/alpha-deploy.sh
    declare -a MODULES=(
    )

    for module in ${MODULES[@]+"${MODULES[@]}"}; do
        module_name=$(basename "$module")
        module_path="${PROJECT_ROOT}/${module}"

        if [[ ! -f "${module_path}/Dockerfile" ]]; then
            log_warning "No Dockerfile found for ${module_name}, skipping..."
            continue
        fi

        log_info "Building ${module_name}..."

        # Build image
        docker build -t "${module_name}:latest" "${module_path}"

        # Tag for MicroK8s registry
        docker tag "${module_name}:latest" "${MICROK8S_REGISTRY}/${IMAGE_PREFIX}/${module_name}:latest"

        # Push to MicroK8s registry
        log_info "Pushing ${module_name} to MicroK8s registry..."
        docker push "${MICROK8S_REGISTRY}/${IMAGE_PREFIX}/${module_name}:latest"
    done

    log_success "All images built and pushed successfully"
}

deploy_with_helm() {
    log_info "Deploying WaddleBot with Helm..."

    HELM_CHART="${SCRIPT_DIR}/helm/waddlebot"
    VALUES_FILE="${HELM_CHART}/values-local.yaml"

    if [[ ! -d "$HELM_CHART" ]]; then
        log_error "Helm chart not found at: $HELM_CHART"
        exit 1
    fi

    # Check if already installed
    if microk8s helm list -n "$NAMESPACE" | grep -q "waddlebot"; then
        log_info "WaddleBot is already installed. Upgrading..."
        microk8s helm upgrade waddlebot "$HELM_CHART" \
            -f "$VALUES_FILE" \
            --namespace "$NAMESPACE"
    else
        log_info "Installing WaddleBot..."
        microk8s helm install waddlebot "$HELM_CHART" \
            -f "$VALUES_FILE" \
            --namespace "$NAMESPACE" \
            --create-namespace
    fi

    log_success "Helm deployment initiated"
}

deploy_with_manifests() {
    log_info "Deploying WaddleBot with raw Kubernetes manifests..."

    MANIFESTS_DIR="${SCRIPT_DIR}/manifests"

    if [[ ! -d "$MANIFESTS_DIR" ]]; then
        log_error "Manifests directory not found at: $MANIFESTS_DIR"
        exit 1
    fi

    # Apply manifests using kustomize
    microk8s kubectl apply -k "$MANIFESTS_DIR"

    log_success "Manifest deployment initiated"
}

wait_for_deployment() {
    log_info "Waiting for pods to be ready..."

    # Wait for infrastructure pods
    log_info "Waiting for infrastructure pods..."
    microk8s kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=infrastructure \
        -n "$NAMESPACE" \
        --timeout=600s || log_warning "Some infrastructure pods may not be ready yet"

    # Wait for core pods
    log_info "Waiting for core pods..."
    microk8s kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=core \
        -n "$NAMESPACE" \
        --timeout=300s || log_warning "Some core pods may not be ready yet"

    # Show deployment status
    log_info "Deployment status:"
    microk8s kubectl get pods -n "$NAMESPACE"
}

configure_access() {
    log_info "Configuring access to WaddleBot..."

    # Add hosts entry if not exists
    if ! grep -q "waddlebot.local" /etc/hosts; then
        log_info "Adding waddlebot.local to /etc/hosts..."
        echo "127.0.0.1 waddlebot.local" | sudo tee -a /etc/hosts
    fi

    log_success "Access configuration complete"
    echo ""
    log_info "WaddleBot can be accessed at:"
    echo "  - http://waddlebot.local (via Ingress)"
    echo "  - http://localhost:30080 (via NodePort)"
    echo ""
    log_info "To view logs:"
    echo "  microk8s kubectl logs -n $NAMESPACE deployment/hub --tail=50"
    echo ""
    log_info "To check status:"
    echo "  microk8s kubectl get pods -n $NAMESPACE"
}

uninstall() {
    log_info "Uninstalling WaddleBot from MicroK8s..."

    if [[ "$DEPLOYMENT_METHOD" == "helm" ]]; then
        log_info "Uninstalling Helm release..."
        microk8s helm uninstall waddlebot -n "$NAMESPACE" || log_warning "Helm release not found"
    else
        log_info "Deleting Kubernetes resources..."
        microk8s kubectl delete -k "${SCRIPT_DIR}/manifests" || log_warning "Resources may not exist"
    fi

    # Delete namespace
    log_info "Deleting namespace..."
    microk8s kubectl delete namespace "$NAMESPACE" --ignore-not-found

    log_success "Uninstall complete"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --helm)
            DEPLOYMENT_METHOD="helm"
            shift
            ;;
        --manifests)
            DEPLOYMENT_METHOD="manifests"
            shift
            ;;
        --build-images)
            BUILD_IMAGES="true"
            shift
            ;;
        --skip-build)
            BUILD_IMAGES="false"
            shift
            ;;
        --skip-setup)
            SKIP_SETUP="true"
            shift
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL="true"
            shift
            ;;
        --help)
            show_help
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            ;;
    esac
done

# Main execution
main() {
    echo ""
    log_info "=== WaddleBot MicroK8s Installation Script ==="
    echo ""

    if [[ "$UNINSTALL" == "true" ]]; then
        uninstall
        exit 0
    fi

    check_prerequisites
    setup_microk8s
    build_and_push_images

    if [[ "$DEPLOYMENT_METHOD" == "helm" ]]; then
        deploy_with_helm
    else
        deploy_with_manifests
    fi

    wait_for_deployment
    configure_access

    echo ""
    log_success "=== WaddleBot installation complete! ==="
    echo ""
}

main
