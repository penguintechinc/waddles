# Waddles Kubernetes Deployment Guide

Complete technical reference for deploying, managing, and operating Waddles on Kubernetes clusters. Covers kubectl commands, Helm charts, scaling strategies, monitoring, and troubleshooting for production deployments.

---

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Deployment Tool by Environment](#deployment-tool-by-environment)
- [Deployment Methods](#deployment-methods)
- [Kubectl Deployment Steps](#kubectl-deployment-steps)
- [Helm Chart Usage](#helm-chart-usage)
- [Configuration Management](#configuration-management)
- [Scaling](#scaling)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)
- [Production Considerations](#production-considerations)

---

## Overview

Waddles is a microservices-based application designed for Kubernetes deployment. The platform consists of:

- **32 Services**: 24+ application modules + 6 infrastructure services
- **5 Infrastructure Services**: PostgreSQL, Redis, MinIO, Qdrant, Ollama
- **8 Core Modules**: Router, Hub, Identity, Labels, Browser Source, Reputation, Community, AI Researcher
- **5 Collector Modules**: Twitch, Discord, Slack, YouTube Live, Kick
- **9 Interactive Modules**: AI, Alias, Shoutout, Inventory, Calendar, Memories, YouTube Music, Spotify, Loyalty
- **4 Platform Action Modules**: Discord, Slack, Twitch, YouTube

**Deployment Options**:
- Automated installation scripts (MicroK8s, kind, minikube) — wrap Helm
- Helm chart directly (`helm install`/`helm upgrade`) — the only supported method
- GitHub Actions CI/CD (automated Helm deployments to beta)

---

## Prerequisites

### Minimum Requirements

- **RAM**: 8 GB
- **CPU**: 4 cores
- **Disk**: 50 GB
- **Kubernetes**: 1.23+

### Recommended Requirements

- **RAM**: 16 GB
- **CPU**: 8 cores
- **Disk**: 100 GB
- **Kubernetes**: 1.28+

### Required Software

**For Local Development**:
- kubectl 1.23+
- Helm 3.x (for Helm deployments)
- Docker (for building images)
- One of:
  - MicroK8s (Ubuntu/Debian)
  - kind (cross-platform)
  - minikube (cross-platform)
  - kubeadm (production clusters)

**For Production**:
- kubectl 1.23+
- Helm 3.x
- Access to Kubernetes cluster (kubeconfig)
- Container registry access (GHCR, Docker Hub, or private)

---

## Quick Start

### Option 1: MicroK8s (Ubuntu/Debian)

**One-Command Install**:
```bash
cd /home/penguin/code/Waddles/k8s
./install-microk8s.sh --build-images
```

**What it does**:
1. Installs MicroK8s (if needed)
2. Enables addons: dns, storage, registry, ingress
3. Builds all Docker images
4. Pushes images to cluster registry
5. Deploys Waddles with Helm
6. Configures ingress access

**Access**: http://waddlebot.local or http://localhost:30080

### Option 2: kind (Any Linux/macOS)

**One-Command Install**:
```bash
cd /home/penguin/code/Waddles/k8s
./install-k8s.sh --kind --build-images
```

**What it does**:
1. Creates kind cluster with ingress support
2. Sets up local registry
3. Builds and pushes Docker images
4. Deploys Waddles with Helm
5. Configures ingress

**Access**: http://waddlebot.local

### Option 3: minikube (Any OS)

**One-Command Install**:
```bash
cd /home/penguin/code/Waddles/k8s
./install-k8s.sh --minikube --build-images
```

**Access**: http://waddlebot.local or `minikube service hub -n waddlebot`

---

## Deployment Tool by Environment

**Helm is the only supported deployment path, alpha through production** — see
[`QUICKSTART.md`](../QUICKSTART.md#deploy-with-helm). `k8s/kustomize/` exists in this repo but is
deprecated (see [`ARCHITECTURE.md`](../ARCHITECTURE.md#build-status-caveats)); do not use it for
new deployments. The install scripts below (`install-microk8s.sh`, `install-k8s.sh`) also expose a
`--manifests` flag that applies k8s/manifests/ (not a real path — that directory does not exist in
this repo today) via Kustomize, so `--manifests` is non-functional. Use the default `--helm` deployment method.

| Environment | Tool |
|---|---|
| **Beta** (`dal2-beta`) | **Helm** — release tracking, rollback history, atomic upgrades |
| **Alpha** (local/MicroK8s/kind/minikube) | **Helm** |

### Beta Deploy Command

```bash
helm upgrade --install waddlebot k8s/helm/waddlebot \
  --kube-context dal2-beta \
  --namespace waddlebot \
  --create-namespace \
  -f k8s/helm/waddlebot/values.yaml \
  -f k8s/helm/waddlebot/values-beta.yaml \
  --timeout 10m \
  --wait
```

---

## Deployment Methods

### Method 1: Automated Scripts (Recommended for Local)

**MicroK8s Installation**:
```bash
# Full installation with image builds
./install-microk8s.sh --build-images

# Skip MicroK8s setup (if already configured)
./install-microk8s.sh --skip-setup --build-images

# Custom namespace
./install-microk8s.sh --namespace my-waddlebot --build-images

# Uninstall
./install-microk8s.sh --uninstall
```

**CNCF Kubernetes Installation**:
```bash
# kind cluster
./install-k8s.sh --kind --build-images

# minikube cluster
./install-k8s.sh --minikube --build-images

# Existing cluster with custom registry
./install-k8s.sh --registry gcr.io/my-project/waddlebot --build-images

# Custom storage and ingress classes
./install-k8s.sh \
  --storage-class gp2 \
  --ingress-class alb \
  --build-images

# Uninstall
./install-k8s.sh --uninstall
```

### Method 2: Helm Chart (Recommended for Production)

**Install with Default Values**:
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --create-namespace
```

**Install with Local Values**:
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  -f ./k8s/helm/waddlebot/values-local.yaml \
  --namespace waddlebot \
  --create-namespace
```

**Advantages**:
- Template-based configuration
- Easy upgrades: `helm upgrade`
- Rollback support: `helm rollback`
- Release management
- Values file customization

---

## Verify Deployment

**Check all pods**:
```bash
kubectl get pods -n waddlebot
```

**Expected output**:
```
NAME                              READY   STATUS    RESTARTS   AGE
postgres-xxxxx                    1/1     Running   0          5m
redis-xxxxx                       1/1     Running   0          5m
minio-xxxxx                       1/1     Running   0          5m
qdrant-xxxxx                      1/1     Running   0          5m
ollama-xxxxx                      1/1     Running   0          5m
router-xxxxx                      2/2     Running   0          3m
hub-xxxxx                         2/2     Running   0          3m
identity-core-xxxxx               2/2     Running   0          3m
...
```

**Check services**:
```bash
kubectl get svc -n waddlebot
```

**Access application**:
- Navigate to http://waddlebot.local
- Default credentials: admin / admin

---

## Helm Chart Usage

### Chart Structure

```
k8s/helm/waddlebot/
├── Chart.yaml              # Chart metadata
├── values.yaml             # Default configuration
├── values-local.yaml       # Local/dev overrides
├── README.md              # Chart documentation
└── templates/
    ├── _helpers.tpl       # Template helpers
    ├── namespace.yaml
    ├── configmap.yaml
    ├── secrets.yaml
    ├── infrastructure/    # Infrastructure templates
    ├── core/             # Core module templates
    ├── collectors/       # Collector templates
    ├── interactive/      # Interactive templates
    ├── pushing/          # Platform action templates
    └── ingress.yaml
```

### Installation

**Basic Installation**:
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --create-namespace
```

**With Custom Values**:
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  -f ./k8s/helm/waddlebot/values-local.yaml \
  --namespace waddlebot \
  --create-namespace
```

**With CLI Overrides**:
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --create-namespace \
  --set global.imageRegistry=ghcr.io/myorg/waddlebot \
  --set global.imageTag=v0.2.0 \
  --set processing.router.replicas=3
```

**Dry Run** (preview without applying):
```bash
helm install waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --dry-run \
  --debug
```

### Upgrading

**Upgrade with New Values**:
```bash
helm upgrade waddlebot ./k8s/helm/waddlebot \
  -f ./k8s/helm/waddlebot/values-local.yaml \
  --namespace waddlebot
```

**Upgrade Specific Components**:
```bash
helm upgrade waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --set processing.router.replicas=5 \
  --set core.identity.replicas=3
```

**Force Recreation**:
```bash
helm upgrade waddlebot ./k8s/helm/waddlebot \
  --namespace waddlebot \
  --recreate-pods
```

### Rolling Back

**View Release History**:
```bash
helm history waddlebot -n waddlebot
```

**Rollback to Previous**:
```bash
helm rollback waddlebot -n waddlebot
```

**Rollback to Specific Revision**:
```bash
helm rollback waddlebot 2 -n waddlebot
```

### Uninstalling

**Remove Release**:
```bash
helm uninstall waddlebot -n waddlebot
```

**Remove with Namespace**:
```bash
helm uninstall waddlebot -n waddlebot
kubectl delete namespace waddlebot
```

---

## Configuration Management

### Helm Values Configuration

**Global Settings** (`values.yaml`):
```yaml
global:
  imageRegistry: "ghcr.io/penguintechinc/waddlebot"
  imageTag: "latest"
  imagePullPolicy: IfNotPresent
  storageClass: "standard"
```

**Resource Presets**:
```yaml
resourcePresets:
  small:
    cpu: "100m"
    memory: "128Mi"
    cpuLimit: "500m"
    memoryLimit: "512Mi"
  medium:
    cpu: "250m"
    memory: "256Mi"
    cpuLimit: "1000m"
    memoryLimit: "1Gi"
  large:
    cpu: "500m"
    memory: "512Mi"
    cpuLimit: "2000m"
    memoryLimit: "2Gi"
```

**Module Configuration Example**:
```yaml
processing:
  router:
    enabled: true
    name: router
    image: waddlebot-router
    tag: latest
    port: 8000
    replicas: 2
    resourcePreset: large
    autoscaling:
      enabled: true
      minReplicas: 2
      maxReplicas: 10
      targetCPUUtilizationPercentage: 70
    env:
      MODULE_NAME: "router_module"
      PORT: "8000"
      LOG_LEVEL: "INFO"
      WORKERS: "4"
```

### Environment Variables

**Shared Environment Variables** (applied to all modules):
```yaml
sharedEnv:
  # Database
  DB_HOST: "postgresql"
  DB_PORT: "5432"
  DB_NAME: "waddlebot"
  DB_USER: "waddlebot"
  DB_POOL_SIZE: "20"

  # Redis
  REDIS_HOST: "redis"
  REDIS_PORT: "6379"
  REDIS_DB: "0"

  # MinIO
  MINIO_ENDPOINT: "minio:9000"
  MINIO_SECURE: "false"

  # Ollama
  OLLAMA_HOST: "http://ollama:11434"
  OLLAMA_MODEL: "llama3.1"

  # Qdrant
  QDRANT_HOST: "qdrant"
  QDRANT_PORT: "6333"

  # Service URLs
  ROUTER_URL: "http://router:8000"
  HUB_URL: "http://hub:8060"

  # Logging
  LOG_LEVEL: "INFO"
  LOG_FORMAT: "json"
```

### Secrets Management

**Create Kubernetes Secret**:
```bash
kubectl create secret generic waddlebot-secrets \
  --namespace waddlebot \
  --from-literal=POSTGRES_PASSWORD=your-password \
  --from-literal=REDIS_PASSWORD=your-password \
  --from-literal=TWITCH_CLIENT_SECRET=your-secret \
  --from-literal=DISCORD_BOT_TOKEN=your-token
```

**From Environment File**:
```bash
kubectl create secret generic waddlebot-secrets \
  --namespace waddlebot \
  --from-env-file=.env
```

**Update Existing Secret**:
```bash
kubectl create secret generic waddlebot-secrets \
  --namespace waddlebot \
  --from-literal=NEW_SECRET=value \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Storage Classes

**MicroK8s**:
```yaml
global:
  storageClass: "microk8s-hostpath"
```

**kind/minikube**:
```yaml
global:
  storageClass: "standard"
```

**Cloud Providers**:
```yaml
# AWS
global:
  storageClass: "gp3"

# GCP
global:
  storageClass: "standard-rwo"

# Azure
global:
  storageClass: "managed-premium"
```

### Ingress Configuration

**Default Ingress**:
```yaml
ingress:
  enabled: true
  className: "nginx"
  hosts:
    - host: waddlebot.local
      paths:
        - path: /
          pathType: Prefix
          service: hub
          port: 8060
```

**TLS Configuration**:
```yaml
ingress:
  tls:
    - secretName: waddlebot-tls
      hosts:
        - waddlebot.example.com
```

**cert-manager Integration**:
```yaml
ingress:
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
```

---

## Scaling

### Manual Scaling

**Scale Specific Deployment**:
```bash
# Scale router to 5 replicas
kubectl scale deployment router -n waddlebot --replicas=5

# Scale hub to 3 replicas
kubectl scale deployment hub -n waddlebot --replicas=3

# Scale multiple deployments
kubectl scale deployment router hub identity-core -n waddlebot --replicas=3
```

**Verify Scaling**:
```bash
kubectl get deployment -n waddlebot
kubectl get pods -n waddlebot -l app.kubernetes.io/name=router
```

### Horizontal Pod Autoscaling (HPA)

**Prerequisites**:
```bash
# Install Metrics Server
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# Verify metrics available
kubectl top nodes
kubectl top pods -n waddlebot
```

**Apply HPA Configurations**:
```bash
# Apply all HPA configs
kubectl apply -f k8s/hpa/

# Or individually
kubectl apply -f k8s/hpa/router-hpa.yaml
kubectl apply -f k8s/hpa/receiver-modules-hpa.yaml
kubectl apply -f k8s/hpa/action-modules-hpa.yaml
```

**HPA Configuration Example** (Router):
```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: router-hpa
  namespace: waddlebot
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: router
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
        - type: Percent
          value: 100
          periodSeconds: 30
        - type: Pods
          value: 2
          periodSeconds: 30
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
        - type: Pods
          value: 1
          periodSeconds: 60
```

**Monitor HPA**:
```bash
# View HPA status
kubectl get hpa -n waddlebot

# Watch HPA activity
kubectl get hpa -n waddlebot --watch

# Detailed HPA information
kubectl describe hpa router-hpa -n waddlebot
```

**HPA Scaling Strategy**:

| Module Type | Min Replicas | Max Replicas | CPU Target | Memory Target |
|-------------|--------------|--------------|------------|---------------|
| Router      | 2            | 10           | 70%        | -             |
| Receivers   | 2            | 10           | 70%        | 80%           |
| Core        | 2            | 8            | 70%        | 80%           |
| Interactive | 2            | 8            | 70%        | 80%           |

**Scaling Behavior**:
- **Scale-up**: Fast (60s stabilization, doubles every 30s)
- **Scale-down**: Conservative (300s stabilization, -50% every 60s)

### Vertical Pod Autoscaling (VPA)

**Install VPA** (optional):
```bash
git clone https://github.com/kubernetes/autoscaler.git
cd autoscaler/vertical-pod-autoscaler
./hack/vpa-up.sh
```

**Apply VPA**:
```yaml
apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: router-vpa
  namespace: waddlebot
spec:
  targetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: router
  updatePolicy:
    updateMode: "Auto"
```

---

## Monitoring

### Pod Status Monitoring

**View All Pods**:
```bash
kubectl get pods -n waddlebot
kubectl get pods -n waddlebot -o wide
```

**Watch Pod Changes**:
```bash
kubectl get pods -n waddlebot --watch
```

**Filter by Label**:
```bash
# Core modules
kubectl get pods -n waddlebot -l tier=core

# Infrastructure
kubectl get pods -n waddlebot -l tier=infrastructure

# Specific app
kubectl get pods -n waddlebot -l app.kubernetes.io/name=router
```

### Logs

**View Pod Logs**:
```bash
# Latest logs
kubectl logs -n waddlebot deployment/router

# Follow logs (tail -f)
kubectl logs -n waddlebot deployment/router -f

# Last 100 lines
kubectl logs -n waddlebot deployment/router --tail=100

# Logs from previous pod (after crash)
kubectl logs -n waddlebot deployment/router --previous
```

**Logs from Multiple Pods**:
```bash
# All router pods
kubectl logs -n waddlebot -l app.kubernetes.io/name=router --tail=50

# All pods in namespace
kubectl logs -n waddlebot --all-containers=true --tail=20
```

**Logs with Timestamps**:
```bash
kubectl logs -n waddlebot deployment/router --timestamps
```

### Resource Usage

**Node Resources**:
```bash
kubectl top nodes
```

**Pod Resources**:
```bash
# All pods
kubectl top pods -n waddlebot

# Specific deployment
kubectl top pods -n waddlebot -l app.kubernetes.io/name=router

# Sort by CPU
kubectl top pods -n waddlebot --sort-by=cpu

# Sort by memory
kubectl top pods -n waddlebot --sort-by=memory
```

### Events

**View Events**:
```bash
# All events
kubectl get events -n waddlebot

# Sort by time
kubectl get events -n waddlebot --sort-by='.lastTimestamp'

# Watch events
kubectl get events -n waddlebot --watch

# Filter by type
kubectl get events -n waddlebot --field-selector type=Warning
```

### Deployment Status

**View Deployments**:
```bash
kubectl get deployments -n waddlebot
kubectl get deployments -n waddlebot -o wide
```

**Deployment Details**:
```bash
kubectl describe deployment router -n waddlebot
```

**Rollout Status**:
```bash
# Check rollout status
kubectl rollout status deployment/router -n waddlebot

# View rollout history
kubectl rollout history deployment/router -n waddlebot

# View specific revision
kubectl rollout history deployment/router -n waddlebot --revision=2
```

### Services

**View Services**:
```bash
kubectl get svc -n waddlebot
kubectl get svc -n waddlebot -o wide
```

**Service Endpoints**:
```bash
kubectl get endpoints -n waddlebot
kubectl describe endpoints router -n waddlebot
```

### Ingress

**View Ingress**:
```bash
kubectl get ingress -n waddlebot
kubectl describe ingress waddlebot-ingress -n waddlebot
```

**Ingress Controller Logs**:
```bash
# NGINX ingress
kubectl logs -n ingress-nginx deployment/ingress-nginx-controller -f
```

---

## Troubleshooting

### Pods Not Starting

**Symptoms**: Pod stuck in Pending, CrashLoopBackOff, or ImagePullBackOff

**Diagnosis**:
```bash
# Check pod status
kubectl get pods -n waddlebot

# Describe pod for details
kubectl describe pod <pod-name> -n waddlebot

# View pod logs
kubectl logs -n waddlebot <pod-name>

# Check events
kubectl get events -n waddlebot --sort-by='.lastTimestamp' | tail -20
```

**Common Causes**:

1. **Insufficient Resources**
   ```bash
   # Check node capacity
   kubectl describe nodes

   # Check resource requests
   kubectl describe pod <pod-name> -n waddlebot | grep -A 5 Requests
   ```

2. **Image Pull Failures**
   ```bash
   # Check image name and registry
   kubectl get pod <pod-name> -n waddlebot -o yaml | grep image:

   # Test image pull manually
   docker pull ghcr.io/owner/waddlebot/router:latest
   ```

3. **Configuration Errors**
   ```bash
   # Check configmaps
   kubectl get configmap -n waddlebot
   kubectl describe configmap waddlebot-config -n waddlebot

   # Check secrets
   kubectl get secrets -n waddlebot
   ```

### Database Connection Issues

**Symptoms**: Pods logging database connection errors

**Diagnosis**:
```bash
# Check PostgreSQL pod
kubectl get pod -n waddlebot -l app.kubernetes.io/name=postgres

# Check PostgreSQL logs
kubectl logs -n waddlebot deployment/postgres -f

# Test connection from router pod
kubectl exec -n waddlebot deployment/router -- \
  python3 -c "import psycopg2; psycopg2.connect('postgresql://waddlebot:password@postgres:5432/waddlebot')"
```

**Solutions**:
```bash
# Restart PostgreSQL
kubectl rollout restart deployment/postgres -n waddlebot

# Check service exists
kubectl get svc postgres -n waddlebot

# Verify DNS resolution
kubectl run -n waddlebot test-dns --image=busybox --rm -it --restart=Never -- nslookup postgres
```

### Storage Issues

**Symptoms**: Pods can't mount volumes, PVCs pending

**Diagnosis**:
```bash
# Check PVCs
kubectl get pvc -n waddlebot

# Describe PVC
kubectl describe pvc postgres-pvc -n waddlebot

# Check PVs
kubectl get pv

# Check storage class
kubectl get storageclass
```

**Solutions**:
```bash
# MicroK8s: Enable storage
microk8s enable storage

# Check available storage on nodes
kubectl describe nodes | grep -A 5 "Allocated resources"

# Delete and recreate PVC (WARNING: data loss)
kubectl delete pvc postgres-pvc -n waddlebot
helm upgrade waddlebot ./k8s/helm/waddlebot -n waddlebot -f ./k8s/helm/waddlebot/values-<env>.yaml
```

### Ingress Not Working

**Symptoms**: Can't access application via ingress URL

**Diagnosis**:
```bash
# Check ingress
kubectl get ingress -n waddlebot
kubectl describe ingress waddlebot-ingress -n waddlebot

# Check ingress controller
kubectl get pods -n ingress-nginx

# Check ingress controller logs
kubectl logs -n ingress-nginx deployment/ingress-nginx-controller
```

**Solutions**:
```bash
# MicroK8s: Enable ingress
microk8s enable ingress

# kind: Install ingress (done by script)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# minikube: Enable ingress
minikube addons enable ingress

# Verify /etc/hosts
cat /etc/hosts | grep waddlebot.local

# Add if missing
echo "127.0.0.1 waddlebot.local" | sudo tee -a /etc/hosts
```

### Service Communication Issues

**Symptoms**: Services can't communicate with each other

**Diagnosis**:
```bash
# Test DNS resolution
kubectl run -n waddlebot test-dns --image=busybox --rm -it --restart=Never -- nslookup router

# Test service connectivity
kubectl run -n waddlebot test-curl --image=curlimages/curl --rm -it --restart=Never -- curl -v http://router:8000/health

# Check network policies
kubectl get networkpolicies -n waddlebot
```

**Solutions**:
```bash
# Verify service exists
kubectl get svc -n waddlebot

# Check service endpoints
kubectl get endpoints router -n waddlebot

# Restart CoreDNS
kubectl rollout restart deployment/coredns -n kube-system
```

### Performance Issues

**Symptoms**: Slow response times, high latency

**Diagnosis**:
```bash
# Check resource usage
kubectl top pods -n waddlebot

# Check node resources
kubectl top nodes

# Check HPA status
kubectl get hpa -n waddlebot

# View resource limits
kubectl describe deployment router -n waddlebot | grep -A 5 Limits
```

**Solutions**:
```bash
# Scale up manually
kubectl scale deployment router -n waddlebot --replicas=5

# Increase resource limits
kubectl set resources deployment router -n waddlebot \
  --limits=cpu=2000m,memory=2Gi \
  --requests=cpu=500m,memory=512Mi

# Enable HPA
kubectl apply -f k8s/hpa/router-hpa.yaml
```

### Application Errors

**Symptoms**: Application returning errors, crashes

**Diagnosis**:
```bash
# Check pod logs
kubectl logs -n waddlebot deployment/router --tail=100

# Follow logs in real-time
kubectl logs -n waddlebot deployment/router -f

# Check previous pod logs (after crash)
kubectl logs -n waddlebot <pod-name> --previous

# Exec into pod
kubectl exec -n waddlebot deployment/router -it -- /bin/bash
```

**Solutions**:
```bash
# Restart deployment
kubectl rollout restart deployment/router -n waddlebot

# Check environment variables
kubectl exec -n waddlebot deployment/router -- env | sort

# Verify config
kubectl get configmap waddlebot-config -n waddlebot -o yaml
```

### Debugging Commands

**Pod Shell Access**:
```bash
# Bash shell
kubectl exec -n waddlebot deployment/router -it -- /bin/bash

# If bash not available
kubectl exec -n waddlebot deployment/router -it -- /bin/sh

# Run single command
kubectl exec -n waddlebot deployment/router -- ls -la /app
```

**Network Debugging**:
```bash
# DNS lookup
kubectl run -n waddlebot test-dns --image=busybox --rm -it --restart=Never -- nslookup postgres

# HTTP test
kubectl run -n waddlebot test-curl --image=curlimages/curl --rm -it --restart=Never -- curl http://router:8000/health

# Network test
kubectl run -n waddlebot test-net --image=nicolaka/netshoot --rm -it --restart=Never -- bash
```

**Port Forwarding**:
```bash
# Forward local port to service
kubectl port-forward -n waddlebot svc/router 8000:8000

# Forward to specific pod
kubectl port-forward -n waddlebot <pod-name> 8000:8000

# Forward multiple ports
kubectl port-forward -n waddlebot svc/hub 8060:8060 &
kubectl port-forward -n waddlebot svc/router 8000:8000 &
```

---

## Production Considerations

### High Availability

**Multi-Replica Deployments**:
```yaml
processing:
  router:
    replicas: 3  # Minimum for HA
    autoscaling:
      enabled: true
      minReplicas: 3
      maxReplicas: 10
```

**Pod Anti-Affinity**:
```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchExpressions:
              - key: app.kubernetes.io/name
                operator: In
                values:
                  - router
          topologyKey: kubernetes.io/hostname
```

**Pod Disruption Budgets**:
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: router-pdb
  namespace: waddlebot
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: router
```

### External Databases

**Use Managed Services** (recommended):
```yaml
sharedEnv:
  DB_HOST: "postgres.rds.amazonaws.com"
  REDIS_HOST: "redis.cache.amazonaws.com"
```

**Connection Pooling**:
```yaml
sharedEnv:
  DB_POOL_SIZE: "50"
  DB_MAX_OVERFLOW: "20"
  DB_POOL_TIMEOUT: "30"
```

### TLS/SSL Configuration

**cert-manager Installation**:
```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml
```

**ClusterIssuer Configuration**:
```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
```

**Ingress TLS**:
```yaml
ingress:
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
  tls:
    - secretName: waddlebot-tls
      hosts:
        - waddlebot.example.com
```

### Secrets Management

**External Secrets Operator**:
```bash
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets-system --create-namespace
```

**AWS Secrets Manager Integration**:
```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secretsmanager
  namespace: waddlebot
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
```

### Monitoring Stack

**Prometheus Installation**:
```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
```

**Grafana Dashboards**:
```bash
# Access Grafana
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
# Login: admin / prom-operator
```

**ServiceMonitor for Waddles**:
```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: waddlebot
  namespace: waddlebot
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: router
  endpoints:
    - port: http
      interval: 30s
```

### Backup Strategy

**Database Backups**:
```bash
# PostgreSQL backup
kubectl exec -n waddlebot deployment/postgres -- pg_dump -U waddlebot waddlebot > backup.sql

# Automated backup CronJob
apiVersion: batch/v1
kind: CronJob
metadata:
  name: postgres-backup
  namespace: waddlebot
spec:
  schedule: "0 2 * * *"  # Daily at 2 AM
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: postgres:16
              command: ["/bin/sh"]
              args:
                - -c
                - pg_dump -U waddlebot -h postgres waddlebot | gzip > /backup/waddlebot-$(date +%Y%m%d).sql.gz
```

**Velero for Cluster Backups**:
```bash
# Install Velero
velero install --provider aws --bucket waddlebot-backups --secret-file credentials-velero

# Create backup
velero backup create waddlebot-backup --include-namespaces waddlebot

# Restore backup
velero restore create --from-backup waddlebot-backup
```

### Resource Limits Best Practices

**Resource Requests** (guaranteed):
- Set based on baseline usage
- Ensures pod can schedule

**Resource Limits** (maximum):
- Set 2-3x requests
- Allows burst capacity

**Example**:
```yaml
resources:
  requests:
    cpu: 250m      # Baseline
    memory: 256Mi
  limits:
    cpu: 1000m     # 4x burst
    memory: 1Gi    # 4x burst
```

### Network Policies

**Restrict Traffic**:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: router-policy
  namespace: waddlebot
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: router
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              name: waddlebot
      ports:
        - protocol: TCP
          port: 8000
  egress:
    - to:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: postgres
      ports:
        - protocol: TCP
          port: 5432
```

---

## Related Documentation

- [`QUICKSTART.md`](../QUICKSTART.md) — fastest path to a running Helm deployment
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — the v3.0.x 8-container pipeline and build status
- [`WORKFLOWS.md`](../WORKFLOWS.md) — GitHub Actions CI/CD workflows
- [`k8s/hpa/README.md`](../../k8s/hpa/README.md) — the separate Kustomize-based HPA overlay
- [`k8s/helm/waddlebot/README.md`](../../k8s/helm/waddlebot/README.md) — Helm chart documentation

---

**Kubernetes Minimum**: 1.23+
**Helm Version**: 3.x
