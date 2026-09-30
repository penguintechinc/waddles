#!/bin/bash
#
# WaddleBot CNCF Kubernetes Installation Script
#
# This script automates the deployment of WaddleBot to a CNCF Kubernetes cluster.
# Supports: kubeadm, kind, minikube, and other standard Kubernetes distributions.
#
# Usage:
#   ./install-k8s.sh [OPTIONS]
#
# Options:
#   --helm              Use Helm chart for deployment (default)
#   --manifests         Use raw Kubernetes manifests for deployment
#   --build-images      Build and push Docker images to registry
#   --skip-build        Skip image building (use existing images)
#   --registry URL      Container registry URL (default: localhost:5000)
#   --context NAME      Kubernetes context to use (default: current)
#   --namespace NAME    Use custom namespace (default: waddlebot)
#   --storage-class     StorageClass to use (default: standard)
#   --ingress-class     IngressClass to use (default: nginx)
#   --kind              Setup and deploy to kind cluster
#   --minikube          Setup and deploy to minikube cluster
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
REGISTRY="localhost:5000"
K8S_CONTEXT=""
NAMESPACE="waddlebot"
STORAGE_CLASS="standard"
INGRESS_CLASS="nginx"
CLUSTER_TYPE=""
UNINSTALL="false"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
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

    # Check if kubectl is installed
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is required but not installed."
        log_info "Install with: curl -LO https://dl.k8s.io/release/\$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
        exit 1
    fi

    # Check if Helm is installed (if using Helm deployment)
    if [[ "$DEPLOYMENT_METHOD" == "helm" ]]; then
        if ! command -v helm &> /dev/null; then
            log_error "Helm is required but not installed."
            log_info "Install with: curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
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

    # Check cluster-specific tools
    if [[ "$CLUSTER_TYPE" == "kind" ]] && ! command -v kind &> /dev/null; then
        log_error "kind is required but not installed."
        log_info "Install with: curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64 && chmod +x ./kind && sudo mv ./kind /usr/local/bin/"
        exit 1
    fi

    if [[ "$CLUSTER_TYPE" == "minikube" ]] && ! command -v minikube &> /dev/null; then
        log_error "minikube is required but not installed."
        log_info "Install with: curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64 && sudo install minikube-linux-amd64 /usr/local/bin/minikube"
        exit 1
    fi

    # Check Kubernetes cluster connectivity
    if [[ -z "$CLUSTER_TYPE" ]]; then
        if ! kubectl cluster-info &> /dev/null; then
            log_error "Cannot connect to Kubernetes cluster. Please check your kubeconfig."
            exit 1
        fi
    fi

    log_success "Prerequisites check passed"
}

setup_kind_cluster() {
    log_info "Setting up kind cluster..."

    # Check if cluster already exists
    if kind get clusters | grep -q "waddlebot"; then
        log_info "kind cluster 'waddlebot' already exists"
    else
        log_info "Creating kind cluster with registry..."

        # Create registry container if it doesn't exist
        if ! docker ps | grep -q "kind-registry"; then
            log_info "Creating local registry container..."
            docker run -d --restart=always -p "5000:5000" --name "kind-registry" registry:2
        fi

        # Create kind cluster with registry
        cat <<EOF | kind create cluster --name waddlebot --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry.mirrors."localhost:5000"]
    endpoint = ["http://kind-registry:5000"]
nodes:
- role: control-plane
  kubeadmConfigPatches:
  - |
    kind: InitConfiguration
    nodeRegistration:
      kubeletExtraArgs:
        node-labels: "ingress-ready=true"
  extraPortMappings:
  - containerPort: 80
    hostPort: 80
    protocol: TCP
  - containerPort: 443
    hostPort: 443
    protocol: TCP
EOF

        # Connect registry to kind network
        docker network connect "kind" "kind-registry" || true
    fi

    # Set context
    kubectl config use-context kind-waddlebot

    # Install ingress-nginx
    log_info "Installing ingress-nginx..."
    kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
    kubectl wait --namespace ingress-nginx \
        --for=condition=ready pod \
        --selector=app.kubernetes.io/component=controller \
        --timeout=300s

    REGISTRY="localhost:5000"
    STORAGE_CLASS="standard"
    INGRESS_CLASS="nginx"

    log_success "kind cluster setup complete"
}

setup_minikube_cluster() {
    log_info "Setting up minikube cluster..."

    # Check if minikube is running
    if ! minikube status &> /dev/null; then
        log_info "Starting minikube..."
        minikube start --cpus=4 --memory=8192 --disk-size=50g
    fi

    # Enable addons
    log_info "Enabling minikube addons..."
    minikube addons enable ingress
    minikube addons enable registry
    minikube addons enable storage-provisioner

    # Set up registry forwarding
    log_info "Setting up registry forwarding..."
    kubectl port-forward --namespace kube-system service/registry 5000:80 &>/dev/null &

    REGISTRY="localhost:5000"
    STORAGE_CLASS="standard"
    INGRESS_CLASS="nginx"

    log_success "minikube cluster setup complete"
}

build_and_push_images() {
    if [[ "$BUILD_IMAGES" != "true" ]]; then
        log_info "Skipping image build (use --build-images to enable)"
        return
    fi

    log_info "Building and pushing Docker images to registry: $REGISTRY"

    # List of all modules to build
    declare -a MODULES=(
        "processing/router_module"
        "admin/hub_module"
        "core/identity_core_module"
        "core/labels_core_module"
        "core/browser_source_core_module"
        "core/reputation_module"
        "core/community_module"
        "core/ai_researcher_module"
        "trigger/receiver/twitch_module"
        "trigger/receiver/discord_module"
        "trigger/receiver/slack_module"
        "trigger/receiver/youtube_live_module"
        "trigger/receiver/kick_module_flask"
        "action/interactive/ai_interaction_module"
        "action/interactive/alias_interaction_module"
        "action/interactive/shoutout_interaction_module"
        "action/interactive/inventory_interaction_module"
        "action/interactive/calendar_interaction_module"
        "action/interactive/memories_interaction_module"
        "action/interactive/youtube_music_interaction_module"
        "action/interactive/spotify_interaction_module"
        "action/interactive/loyalty_interaction_module"
    )

    for module in "${MODULES[@]}"; do
        module_name=$(basename "$module")
        module_path="${PROJECT_ROOT}/${module}"

        if [[ ! -f "${module_path}/Dockerfile" ]]; then
            log_warning "No Dockerfile found for ${module_name}, skipping..."
            continue
        fi

        log_info "Building ${module_name}..."

        # Build image
        docker build -t "${module_name}:latest" "${module_path}"

        # Tag for registry
        docker tag "${module_name}:latest" "${REGISTRY}/${IMAGE_PREFIX}/${module_name}:latest"

        # Push to registry
        log_info "Pushing ${module_name} to registry..."
        docker push "${REGISTRY}/${IMAGE_PREFIX}/${module_name}:latest"
    done

    log_success "All images built and pushed successfully"
}

create_values_override() {
    log_info "Creating values override file for deployment..."

    cat > /tmp/waddlebot-values-override.yaml <<EOF
global:
  imageRegistry: ${REGISTRY}/${IMAGE_PREFIX}
  storageClass: ${STORAGE_CLASS}

namespace:
  create: true
  name: ${NAMESPACE}

ingress:
  enabled: true
  className: ${INGRESS_CLASS}
  host: waddlebot.local

postgres:
  persistence:
    storageClass: ${STORAGE_CLASS}

redis:
  persistence:
    storageClass: ${STORAGE_CLASS}

seaweedfs:
  persistence:
    storageClass: ${STORAGE_CLASS}

qdrant:
  persistence:
    storageClass: ${STORAGE_CLASS}

ollama:
  persistence:
    storageClass: ${STORAGE_CLASS}
EOF

    log_success "Values override file created at /tmp/waddlebot-values-override.yaml"
}

deploy_with_helm() {
    log_info "Deploying WaddleBot with Helm..."

    HELM_CHART="${SCRIPT_DIR}/helm/waddlebot"

    if [[ ! -d "$HELM_CHART" ]]; then
        log_error "Helm chart not found at: $HELM_CHART"
        exit 1
    fi

    create_values_override

    # Set context if specified
    if [[ -n "$K8S_CONTEXT" ]]; then
        kubectl config use-context "$K8S_CONTEXT"
    fi

    # Check if already installed
    if helm list -n "$NAMESPACE" | grep -q "waddlebot"; then
        log_info "WaddleBot is already installed. Upgrading..."
        helm upgrade waddlebot "$HELM_CHART" \
            -f /tmp/waddlebot-values-override.yaml \
            --namespace "$NAMESPACE"
    else
        log_info "Installing WaddleBot..."
        helm install waddlebot "$HELM_CHART" \
            -f /tmp/waddlebot-values-override.yaml \
            --namespace "$NAMESPACE" \
            --create-namespace
    fi

    log_success "Helm deployment initiated"
}

update_manifests_for_deployment() {
    log_info "Updating manifests for deployment..."

    MANIFESTS_DIR="${SCRIPT_DIR}/manifests"
    TEMP_MANIFESTS_DIR="/tmp/waddlebot-manifests"

    # Copy manifests to temp directory
    cp -r "$MANIFESTS_DIR" "$TEMP_MANIFESTS_DIR"

    # Update image registry in all deployment files
    find "$TEMP_MANIFESTS_DIR" -name "*.yaml" -type f -exec sed -i \
        "s|localhost:32000/waddlebot|${REGISTRY}/${IMAGE_PREFIX}|g" {} \;

    # Update storage class
    find "$TEMP_MANIFESTS_DIR" -name "*.yaml" -type f -exec sed -i \
        "s|storageClassName: microk8s-hostpath|storageClassName: ${STORAGE_CLASS}|g" {} \;

    # Update ingress class
    sed -i "s|ingressClassName: public|ingressClassName: ${INGRESS_CLASS}|g" \
        "$TEMP_MANIFESTS_DIR/ingress.yaml"

    log_success "Manifests updated for deployment"
}

deploy_with_manifests() {
    log_info "Deploying WaddleBot with raw Kubernetes manifests..."

    update_manifests_for_deployment

    # Set context if specified
    if [[ -n "$K8S_CONTEXT" ]]; then
        kubectl config use-context "$K8S_CONTEXT"
    fi

    # Apply manifests using kustomize
    kubectl apply -k /tmp/waddlebot-manifests

    log_success "Manifest deployment initiated"
}

wait_for_deployment() {
    log_info "Waiting for pods to be ready..."

    # Wait for infrastructure pods
    log_info "Waiting for infrastructure pods..."
    kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=infrastructure \
        -n "$NAMESPACE" \
        --timeout=600s || log_warning "Some infrastructure pods may not be ready yet"

    # Wait for core pods
    log_info "Waiting for core pods..."
    kubectl wait --for=condition=ready pod \
        -l app.kubernetes.io/component=core \
        -n "$NAMESPACE" \
        --timeout=300s || log_warning "Some core pods may not be ready yet"

    # Show deployment status
    log_info "Deployment status:"
    kubectl get pods -n "$NAMESPACE"
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

    if [[ "$CLUSTER_TYPE" == "kind" ]]; then
        echo "  - http://waddlebot.local (via Ingress)"
    elif [[ "$CLUSTER_TYPE" == "minikube" ]]; then
        echo "  - http://waddlebot.local (via Ingress)"
        echo "  - Or use: minikube service hub -n $NAMESPACE"
    else
        echo "  - http://waddlebot.local (via Ingress)"
        echo "  - Or use port-forward: kubectl port-forward -n $NAMESPACE svc/hub 8060:8060"
    fi

    echo ""
    log_info "To view logs:"
    echo "  kubectl logs -n $NAMESPACE deployment/hub --tail=50"
    echo ""
    log_info "To check status:"
    echo "  kubectl get pods -n $NAMESPACE"
}

uninstall() {
    log_info "Uninstalling WaddleBot from Kubernetes..."

    # Set context if specified
    if [[ -n "$K8S_CONTEXT" ]]; then
        kubectl config use-context "$K8S_CONTEXT"
    fi

    if [[ "$DEPLOYMENT_METHOD" == "helm" ]]; then
        log_info "Uninstalling Helm release..."
        helm uninstall waddlebot -n "$NAMESPACE" || log_warning "Helm release not found"
    else
        log_info "Deleting Kubernetes resources..."
        update_manifests_for_deployment
        kubectl delete -k /tmp/waddlebot-manifests || log_warning "Resources may not exist"
    fi

    # Delete namespace
    log_info "Deleting namespace..."
    kubectl delete namespace "$NAMESPACE" --ignore-not-found

    # Cleanup kind cluster if requested
    if [[ "$CLUSTER_TYPE" == "kind" ]]; then
        read -p "Do you want to delete the kind cluster? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            kind delete cluster --name waddlebot
            docker rm -f kind-registry
        fi
    fi

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
        --registry)
            REGISTRY="$2"
            shift 2
            ;;
        --context)
            K8S_CONTEXT="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --storage-class)
            STORAGE_CLASS="$2"
            shift 2
            ;;
        --ingress-class)
            INGRESS_CLASS="$2"
            shift 2
            ;;
        --kind)
            CLUSTER_TYPE="kind"
            shift
            ;;
        --minikube)
            CLUSTER_TYPE="minikube"
            shift
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
    log_info "=== WaddleBot CNCF Kubernetes Installation Script ==="
    echo ""

    if [[ "$UNINSTALL" == "true" ]]; then
        uninstall
        exit 0
    fi

    check_prerequisites

    # Setup cluster if type specified
    if [[ "$CLUSTER_TYPE" == "kind" ]]; then
        setup_kind_cluster
    elif [[ "$CLUSTER_TYPE" == "minikube" ]]; then
        setup_minikube_cluster
    fi

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
