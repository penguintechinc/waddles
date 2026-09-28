# Waddles Helm Chart

A comprehensive Helm chart for deploying Waddles, a multi-platform chat bot system with modular, microservices architecture supporting Twitch, Discord, Slack, YouTube Live, and Kick.

## Overview

Waddles is a scalable, production-ready chat bot platform built on a microservices architecture. This Helm chart deploys the complete Waddles ecosystem including:

- **Trigger Modules**: Platform-specific webhook receivers and pollers (Twitch, Discord, Slack, YouTube Live, Kick)
- **Processing Module**: High-performance command router with multi-threading and caching
- **Action Modules**: Interactive response modules (AI, alias, shoutout, inventory, calendar, memories, music)
- **Core Modules**: Platform services (identity, labels, browser source, reputation, community)
- **Admin Modules**: Community management portal (Hub)
- **Infrastructure**: PostgreSQL, Redis, MinIO, Ollama, Qdrant

## Prerequisites

- Kubernetes 1.23+
- Helm v3.8+
- Storage provisioner for PersistentVolumeClaims
- (Optional) NGINX Ingress Controller
- (Optional) cert-manager for TLS certificates

### For Local Development (microk8s)

```bash
# Install microk8s
sudo snap install microk8s --classic

# Enable required addons
microk8s enable dns storage ingress helm3

# Create alias for convenience
alias kubectl='microk8s kubectl'
alias helm='microk8s helm3'
```

## Quick Start

### Local Deployment (microk8s)

1. Clone the repository:
```bash
git clone https://github.com/yourusername/WaddleBot.git
cd WaddleBot/k8s/helm
```

2. Create namespace:
```bash
kubectl create namespace waddlebot
```

3. Create secrets for sensitive data:
```bash
kubectl create secret generic waddlebot-secrets \
  --namespace=waddlebot \
  --from-literal=db-password=your-secure-password \
  --from-literal=redis-password=your-redis-password \
  --from-literal=minio-root-user=admin \
  --from-literal=minio-root-password=your-minio-password
```

4. Install the chart:
```bash
helm install waddlebot ./waddlebot --namespace waddlebot
```

5. Verify deployment:
```bash
kubectl get pods -n waddlebot
kubectl get services -n waddlebot
kubectl get ingress -n waddlebot
```

6. Access the Hub UI:
```bash
# Add to /etc/hosts for local development
echo "127.0.0.1 waddles.local" | sudo tee -a /etc/hosts

# Open in browser
http://waddles.local
```

### Production Deployment

1. Create a custom values file:
```bash
cp waddlebot/values.yaml my-values.yaml
```

2. Edit `my-values.yaml` with your production settings:
```yaml
ingress:
  enabled: true
  className: "nginx"
  hosts:
    - host: waddles.yourdomain.com
      paths:
        - path: /
          pathType: Prefix
          backend:
            service: hub
            port: 8060
  tls:
    - secretName: waddlebot-tls
      hosts:
        - waddlebot.yourdomain.com

infrastructure:
  postgresql:
    persistence:
      size: 100Gi
  redis:
    persistence:
      size: 20Gi
  minio:
    persistence:
      size: 200Gi

processing:
  router:
    replicas: 5
    autoscaling:
      enabled: true
      maxReplicas: 20
```

3. Install with custom values:
```bash
helm install waddlebot ./waddlebot \
  --namespace waddlebot \
  --values my-values.yaml
```

## Configuration

The following table lists the main configurable parameters of the WaddleBot chart and their default values.

### Global Settings

| Parameter | Description | Default |
|-----------|-------------|---------|
| `global.imageRegistry` | Global Docker image registry | `docker.io/waddlebot` |
| `global.imagePullPolicy` | Global image pull policy | `IfNotPresent` |
| `global.imagePullSecrets` | Global image pull secrets | `[]` |
| `global.storageClass` | Global storage class for PVCs | `standard` |

### Namespace

| Parameter | Description | Default |
|-----------|-------------|---------|
| `namespace.create` | Create namespace automatically | `true` |
| `namespace.name` | Namespace name | `waddlebot` |

### Resource Presets

| Parameter | Description | Default |
|-----------|-------------|---------|
| `resourcePresets.small.cpu` | Small preset CPU request | `100m` |
| `resourcePresets.small.memory` | Small preset memory request | `128Mi` |
| `resourcePresets.small.cpuLimit` | Small preset CPU limit | `500m` |
| `resourcePresets.small.memoryLimit` | Small preset memory limit | `512Mi` |
| `resourcePresets.medium.cpu` | Medium preset CPU request | `250m` |
| `resourcePresets.medium.memory` | Medium preset memory request | `256Mi` |
| `resourcePresets.medium.cpuLimit` | Medium preset CPU limit | `1000m` |
| `resourcePresets.medium.memoryLimit` | Medium preset memory limit | `1Gi` |
| `resourcePresets.large.cpu` | Large preset CPU request | `500m` |
| `resourcePresets.large.memory` | Large preset memory request | `512Mi` |
| `resourcePresets.large.cpuLimit` | Large preset CPU limit | `2000m` |
| `resourcePresets.large.memoryLimit` | Large preset memory limit | `2Gi` |

### Infrastructure Services

#### PostgreSQL

| Parameter | Description | Default |
|-----------|-------------|---------|
| `infrastructure.postgresql.enabled` | Enable PostgreSQL deployment | `true` |
| `infrastructure.postgresql.image` | PostgreSQL image | `postgres:16` |
| `infrastructure.postgresql.port` | PostgreSQL port | `5432` |
| `infrastructure.postgresql.replicas` | Number of replicas | `1` |
| `infrastructure.postgresql.persistence.enabled` | Enable persistence | `true` |
| `infrastructure.postgresql.persistence.size` | PVC size | `20Gi` |
| `infrastructure.postgresql.readReplicas.enabled` | Enable read replicas | `false` |
| `infrastructure.postgresql.readReplicas.count` | Number of read replicas | `2` |

#### Redis

| Parameter | Description | Default |
|-----------|-------------|---------|
| `infrastructure.redis.enabled` | Enable Redis deployment | `true` |
| `infrastructure.redis.image` | Redis image | `redis:7-alpine` |
| `infrastructure.redis.port` | Redis port | `6379` |
| `infrastructure.redis.persistence.enabled` | Enable persistence | `true` |
| `infrastructure.redis.persistence.size` | PVC size | `5Gi` |

#### MinIO

| Parameter | Description | Default |
|-----------|-------------|---------|
| `infrastructure.minio.enabled` | Enable MinIO deployment | `true` |
| `infrastructure.minio.image` | MinIO image | `minio/minio:latest` |
| `infrastructure.minio.apiPort` | MinIO API port | `9000` |
| `infrastructure.minio.consolePort` | MinIO console port | `9001` |
| `infrastructure.minio.persistence.size` | PVC size | `50Gi` |
| `infrastructure.minio.kms.enabled` | Require a KMS for at-rest (SSE) encryption | `true` |
| `infrastructure.minio.kms.secretName` | Pre-created Secret holding `MINIO_KMS_SECRET_KEY` | `""` (`"minio-kms"` in `values-alpha.yaml`) |
| `infrastructure.minio.kms.secretKey` | Key name inside that Secret | `MINIO_KMS_SECRET_KEY` |
| `infrastructure.minio.kms.autoEncryptBucket` | Apply SSE-S3 default encryption to chart-created buckets | `true` |

##### MinIO KMS (mandatory at-rest encryption)

At-rest encryption is mandatory for every store holding sensitive data (see
`~/.claude/rules/security.md` Encryption). hub-api calls
`put_object(..., ServerSideEncryption="AES256")`, which MinIO refuses with
*"Server side encryption specified but KMS is not configured"* unless a KMS
is wired up — dropping SSE is never an acceptable fix.

This chart configures MinIO's static single-key KMS via the `MINIO_KMS_SECRET_KEY`
env var, sourced from a Kubernetes Secret you pre-create (never chart-managed,
never a literal key in git):

```bash
make generate-minio-kms-key KUBE_CONTEXT=dal2-beta [NAMESPACE=waddlebot]
# or directly:
./scripts/generate-minio-kms-key.sh --context dal2-beta --namespace waddlebot \
  [--secret-name minio-kms] [--key-name waddlebot-minio]
```

This creates/updates a Secret (default name `minio-kms`) with a single key
`MINIO_KMS_SECRET_KEY` in the format `<key-name>:<base64 32 bytes>`. Point the
chart at it by setting `infrastructure.minio.kms.secretName` (via `--set` or
an out-of-git values override) to that Secret's name.

`values.yaml`'s baseline leaves `secretName` empty (fail-closed) and
`templates/infrastructure/minio.yaml` renders a `fail` guard: any release
with `infrastructure.minio.enabled=true` outside alpha (beta/gamma/production)
that hasn't set `infrastructure.minio.kms.secretName` fails to template.
Alpha alone documents a default (`values-alpha.yaml` sets it to `minio-kms`).

When `infrastructure.minio.kms.autoEncryptBucket` is true, the `minio-init`
Job also runs `mc encrypt set sse-s3` on every chart-created bucket so objects
written without an explicit `ServerSideEncryption` header are still encrypted.

**Production note:** this static-key mechanism is a stopgap. For production, a
real external KMS such as MinIO KES or HashiCorp Vault is recommended instead
of a static key — see `~/.claude/rules/security.md` Encryption tiering
(baseline platform-managed keys vs. Enterprise-tier customer-managed/external
KMS). Migrating to KES/Vault does not require an application change — only
MinIO's own KMS backend configuration.

#### Ollama

| Parameter | Description | Default |
|-----------|-------------|---------|
| `infrastructure.ollama.enabled` | Enable Ollama AI backend | `true` |
| `infrastructure.ollama.image` | Ollama image | `ollama/ollama:latest` |
| `infrastructure.ollama.port` | Ollama port | `11434` |
| `infrastructure.ollama.persistence.size` | PVC size for models | `30Gi` |
| `infrastructure.ollama.gpu.enabled` | Enable GPU support | `false` |
| `infrastructure.ollama.gpu.count` | Number of GPUs | `1` |

#### Qdrant

| Parameter | Description | Default |
|-----------|-------------|---------|
| `infrastructure.qdrant.enabled` | Enable Qdrant vector DB | `true` |
| `infrastructure.qdrant.image` | Qdrant image | `qdrant/qdrant:latest` |
| `infrastructure.qdrant.port` | Qdrant HTTP port | `6333` |
| `infrastructure.qdrant.grpcPort` | Qdrant gRPC port | `6334` |
| `infrastructure.qdrant.persistence.size` | PVC size | `10Gi` |

### Processing Module (Router)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `processing.router.enabled` | Enable router module | `true` |
| `processing.router.image` | Router image name | `waddlebot-router` |
| `processing.router.tag` | Router image tag | `latest` |
| `processing.router.port` | Router port | `8000` |
| `processing.router.replicas` | Number of replicas | `2` |
| `processing.router.autoscaling.enabled` | Enable HPA | `true` |
| `processing.router.autoscaling.minReplicas` | Minimum replicas | `2` |
| `processing.router.autoscaling.maxReplicas` | Maximum replicas | `10` |
| `processing.router.autoscaling.targetCPUUtilizationPercentage` | Target CPU % | `70` |

### Admin Module (Hub)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `admin.hub.enabled` | Enable hub module | `true` |
| `admin.hub.image` | Hub image name | `waddlebot-hub` |
| `admin.hub.tag` | Hub image tag | `latest` |
| `admin.hub.port` | Hub port | `8060` |
| `admin.hub.replicas` | Number of replicas | `2` |

### Core Modules

| Parameter | Description | Default |
|-----------|-------------|---------|
| `core.identity.enabled` | Enable identity core module | `true` |
| `core.labels.enabled` | Enable labels core module | `true` |
| `core.browserSource.enabled` | Enable browser source module | `true` |
| `core.reputation.enabled` | Enable reputation module | `true` |
| `core.community.enabled` | Enable community module | `true` |
| `core.aiResearcher.enabled` | Enable AI researcher module | `true` |

### Trigger Modules (Receivers)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `trigger.receivers.twitch.enabled` | Enable Twitch collector | `true` |
| `trigger.receivers.discord.enabled` | Enable Discord collector | `true` |
| `trigger.receivers.slack.enabled` | Enable Slack collector | `true` |
| `trigger.receivers.youtubeLive.enabled` | Enable YouTube Live collector | `true` |
| `trigger.receivers.kick.enabled` | Enable Kick collector | `true` |

### Action Modules (Interactive)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `action.interactive.ai.enabled` | Enable AI interaction module | `true` |
| `action.interactive.alias.enabled` | Enable alias module | `true` |
| `action.interactive.shoutout.enabled` | Enable shoutout module | `true` |
| `action.interactive.inventory.enabled` | Enable inventory module | `true` |
| `action.interactive.calendar.enabled` | Enable calendar module | `true` |
| `action.interactive.memories.enabled` | Enable memories module | `true` |
| `action.interactive.youtubeMusic.enabled` | Enable YouTube Music module | `true` |
| `action.interactive.spotify.enabled` | Enable Spotify module | `true` |
| `action.interactive.loyalty.enabled` | Enable loyalty module | `true` |

### Action Modules (Platform Actions)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `action.platformActions.discord.enabled` | Enable Discord action module | `true` |
| `action.platformActions.slack.enabled` | Enable Slack action module | `true` |
| `action.platformActions.twitch.enabled` | Enable Twitch action module | `true` |
| `action.platformActions.youtube.enabled` | Enable YouTube action module | `true` |

### Ingress

| Parameter | Description | Default |
|-----------|-------------|---------|
| `ingress.enabled` | Enable ingress | `true` |
| `ingress.className` | Ingress class name | `nginx` |
| `ingress.annotations` | Ingress annotations | See values.yaml |
| `ingress.hosts` | Ingress hosts configuration | See values.yaml |
| `ingress.tls` | TLS configuration | See values.yaml |

### Shared Environment Variables

| Parameter | Description | Default |
|-----------|-------------|---------|
| `sharedEnv.DB_HOST` | Database host | `postgresql` |
| `sharedEnv.DB_PORT` | Database port | `5432` |
| `sharedEnv.DB_NAME` | Database name | `waddlebot` |
| `sharedEnv.REDIS_HOST` | Redis host | `redis` |
| `sharedEnv.REDIS_PORT` | Redis port | `6379` |
| `sharedEnv.ROUTER_URL` | Router service URL | `http://router:8000` |
| `sharedEnv.HUB_URL` | Hub service URL | `http://hub:8060` |
| `sharedEnv.LOG_LEVEL` | Logging level | `INFO` |
| `sharedEnv.RELEASE_MODE` | Enable license enforcement | `false` |

### Security

| Parameter | Description | Default |
|-----------|-------------|---------|
| `podSecurityContext.runAsNonRoot` | Run as non-root user | `true` |
| `podSecurityContext.runAsUser` | User ID to run as | `1000` |
| `podSecurityContext.fsGroup` | Filesystem group | `1000` |
| `securityContext.allowPrivilegeEscalation` | Allow privilege escalation | `false` |
| `securityContext.readOnlyRootFilesystem` | Read-only root filesystem | `false` |

## Installation

### Install from local chart

```bash
helm install waddlebot ./waddlebot \
  --namespace waddlebot \
  --create-namespace
```

### Install with custom values

```bash
helm install waddlebot ./waddlebot \
  --namespace waddlebot \
  --create-namespace \
  --values my-values.yaml
```

### Install specific modules only

```yaml
# minimal-values.yaml
infrastructure:
  postgresql:
    enabled: true
  redis:
    enabled: true
  ollama:
    enabled: false
  qdrant:
    enabled: false
  minio:
    enabled: false

processing:
  router:
    enabled: true

admin:
  hub:
    enabled: true

# Enable only Discord
trigger:
  receivers:
    discord:
      enabled: true
    twitch:
      enabled: false
    slack:
      enabled: false
```

```bash
helm install waddlebot ./waddlebot \
  --namespace waddlebot \
  --values minimal-values.yaml
```

## Upgrading

### Upgrade to new version

```bash
helm upgrade waddlebot ./waddlebot \
  --namespace waddlebot \
  --values my-values.yaml
```

### Upgrade with new values

```bash
helm upgrade waddlebot ./waddlebot \
  --namespace waddlebot \
  --set processing.router.replicas=5
```

### Rollback to previous version

```bash
helm rollback waddlebot 1 --namespace waddlebot
```

## Uninstallation

### Uninstall the chart

```bash
helm uninstall waddlebot --namespace waddlebot
```

### Clean up PVCs (WARNING: This will delete all data)

```bash
kubectl delete pvc -n waddlebot --all
```

### Delete namespace

```bash
kubectl delete namespace waddlebot
```

## Troubleshooting

### Pods not starting

1. Check pod status:
```bash
kubectl get pods -n waddlebot
kubectl describe pod <pod-name> -n waddlebot
```

2. Check pod logs:
```bash
kubectl logs <pod-name> -n waddlebot
kubectl logs <pod-name> -n waddlebot --previous  # Previous container logs
```

### Database connection issues

1. Check PostgreSQL pod:
```bash
kubectl get pod -n waddlebot -l app=postgresql
kubectl logs -n waddlebot -l app=postgresql
```

2. Test database connection:
```bash
kubectl exec -it -n waddlebot <postgres-pod> -- psql -U waddlebot -d waddlebot
```

3. Verify secrets:
```bash
kubectl get secrets -n waddlebot
kubectl describe secret waddlebot-secrets -n waddlebot
```

### Ingress not working

1. Check ingress status:
```bash
kubectl get ingress -n waddlebot
kubectl describe ingress waddlebot -n waddlebot
```

2. Verify NGINX ingress controller:
```bash
kubectl get pods -n ingress-nginx
```

3. Check ingress logs:
```bash
kubectl logs -n ingress-nginx -l app.kubernetes.io/component=controller
```

### Service connectivity issues

1. Test service endpoints:
```bash
kubectl get endpoints -n waddlebot
```

2. Test pod-to-pod communication:
```bash
kubectl exec -it -n waddlebot <pod-name> -- curl http://router:8000/health
```

3. Port forward for direct access:
```bash
kubectl port-forward -n waddlebot svc/waddlebot-hub 8060:8060
```

### Storage issues

1. Check PVCs:
```bash
kubectl get pvc -n waddlebot
kubectl describe pvc <pvc-name> -n waddlebot
```

2. Check storage class:
```bash
kubectl get storageclass
```

3. Verify provisioner:
```bash
kubectl get pods -n kube-system | grep provisioner
```

### Resource constraints

1. Check resource usage:
```bash
kubectl top pods -n waddlebot
kubectl top nodes
```

2. Describe pod for resource limits:
```bash
kubectl describe pod <pod-name> -n waddlebot | grep -A 5 "Limits"
```

3. Check HPA status:
```bash
kubectl get hpa -n waddlebot
kubectl describe hpa <hpa-name> -n waddlebot
```

### Common issues and solutions

#### Issue: ImagePullBackOff
**Solution**: Verify image names and pull secrets
```bash
# Check if images exist
docker pull docker.io/waddlebot/waddlebot-router:latest

# Add image pull secret
kubectl create secret docker-registry regcred \
  --docker-server=docker.io \
  --docker-username=<username> \
  --docker-password=<password> \
  --namespace=waddlebot
```

#### Issue: CrashLoopBackOff
**Solution**: Check logs and configuration
```bash
kubectl logs <pod-name> -n waddlebot
kubectl get events -n waddlebot --sort-by='.lastTimestamp'
```

#### Issue: Pending PVC
**Solution**: Check storage provisioner
```bash
# For microk8s, ensure storage addon is enabled
microk8s enable storage

# For other clusters, verify storage class
kubectl get storageclass
```

#### Issue: DNS resolution failures
**Solution**: Verify CoreDNS
```bash
kubectl get pods -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns
```

## Monitoring and Observability

### Enable Prometheus monitoring

```yaml
monitoring:
  prometheus:
    enabled: true
    serviceMonitor:
      enabled: true
      interval: 30s
```

### View metrics

```bash
kubectl port-forward -n waddlebot svc/waddlebot-router 8000:8000
curl http://localhost:8000/metrics
```

### Check health endpoints

```bash
# Router health
kubectl exec -it -n waddlebot <router-pod> -- curl http://localhost:8000/health

# Hub health
kubectl exec -it -n waddlebot <hub-pod> -- curl http://localhost:8060/health
```

## Production Best Practices

### High Availability

1. Use multiple replicas for critical services:
```yaml
processing:
  router:
    replicas: 3

admin:
  hub:
    replicas: 3
```

2. Enable autoscaling:
```yaml
processing:
  router:
    autoscaling:
      enabled: true
      minReplicas: 3
      maxReplicas: 20
```

3. Use pod anti-affinity for distribution across nodes:
```yaml
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchExpressions:
              - key: app
                operator: In
                values:
                  - router
          topologyKey: kubernetes.io/hostname
```

### Security

1. Use secrets for sensitive data:
```bash
kubectl create secret generic waddlebot-secrets \
  --from-literal=db-password=$(openssl rand -base64 32) \
  --from-literal=redis-password=$(openssl rand -base64 32)
```

2. Enable network policies:
```yaml
networkPolicy:
  enabled: true
```

3. Use TLS for ingress:
```yaml
ingress:
  tls:
    - secretName: waddlebot-tls
      hosts:
        - waddlebot.yourdomain.com
```

### Backup and Recovery

1. Backup PostgreSQL:
```bash
kubectl exec -it -n waddlebot <postgres-pod> -- \
  pg_dump -U waddlebot waddlebot > backup.sql
```

2. Backup PVCs using Velero or similar tools

3. Regular snapshot of persistent volumes

## Support

- Documentation: `/docs` in repository
- Issues: GitHub Issues
- License: See LICENSE file

## Contributing

Contributions are welcome! Please read the development guidelines in `/docs/development-rules.md`.

## License

WaddleBot is licensed under the terms specified in the LICENSE file. Integration with PenguinTech License Server is required for production deployments when `RELEASE_MODE=true`.
