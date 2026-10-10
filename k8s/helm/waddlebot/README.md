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
| `global.imagePullPolicy` | Image pull policy for **every** workload container, including `hub-api`, `svc-{action,ingest,process}-rust`, `svc-presentation` and both `bundle-executor` Deployments (no template hardcodes a policy) | `IfNotPresent` |
| `global.imagePullSecrets` | Global image pull secrets | `[]` |
| `global.storageClass` | Global storage class for PVCs | `standard` |

#### Image pull policy

Every deploy carries a unique per-deploy `global.imageTag` (alpha SHA8, beta `beta-<epoch>`,
gamma `gamma-<epoch>`, production digest), so `IfNotPresent` is correct in every environment
(`values-alpha.yaml`, `-beta`, `-gamma`, `-production` all declare it). `Always` is **not** safe
as a blanket default: for locally-loaded images (kind e2e, alpha local builds) the node already
has the image, `Always` forces a registry pull that fails, and the pod never becomes Ready.
Set `--set global.imagePullPolicy=Always` per deployment only when deploying a mutable tag
(e.g. `latest`); `values-local.yaml` keeps `Always` for that reason.
`tests/test_image_pull_policy_render.py` pins this behavior.

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
| `infrastructure.redis.maxMemoryPolicy` | Eviction policy | `allkeys-lru` |

**`maxMemoryPolicy` and the bundle `storage.kv` capability (PR #425 security review):** the Rust data plane's per-app `storage.kv` live-key-count quota counter (`bundlekv:...:count`) is stored with no TTL. An `allkeys-*` policy (the default above) can evict that counter under memory pressure, silently resetting the quota — `svc-process-rust`/`svc-action-rust` detect this at startup (`CONFIG GET maxmemory-policy`) and log `ERROR` + emit a metric if so, and self-heal the counter via a bounded `SCAN` on the next write, but the right fix is the policy itself. If this instance is used for `storage.kv`, set `maxMemoryPolicy` to `noeviction` or a `volatile-*` policy — both are safe for `storage.kv`'s own (non-volatile) keys; only switch off `allkeys-*` here if no *other* consumer of this same instance (spine streams, usage metering, relay queues) genuinely needs size-bounded eviction, since the policy is instance-wide, not per-key-pattern.

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

## Database accounts (per-service roles)

No workload connects as the database owner/superuser. Every Deployment/Job/CronJob gets its own
least-privilege Postgres role through `{{ include "waddlebot.dbEnv" (dict "root" . "role" "<role>") }}`;
role names/grants live in `config/postgres/service-roles.yaml` and are mirrored in
`infrastructure.postgresql.serviceRoles.roles`. Passwords are generated in alpha/local and **required**
(`serviceRoles.existingSecret`, or `serviceRoles.passwords` from an out-of-git file) in beta/gamma/
production. The owner credential is a separate Secret (`<release>-db-admin`) read only by Postgres and
the db-migrate hook. See [`docs/DATABASE_CREDENTIALS.md`](../../../docs/DATABASE_CREDENTIALS.md) for rotation,
the burned-credential list, and the upgrade path.

## Self-provisioned platform keys (feature/helm-auto-provision-keys)

Deploys must never require a human to run a script before `helm install`/`helm upgrade`
succeeds. Four platform key Secrets are self-provisioned by this chart:

| Secret | Values key | Consumer | Mechanism |
|---|---|---|---|
| `minio-kms` | `autoProvisionedKeys.minioKms` | MinIO static-KMS (PR #439) | `templates/auto-provisioned-secrets.yaml` -- pure Helm template |
| `waddlebot-tenant-kek` | `autoProvisionedKeys.tenantKek` | hub-api tenant DEK broker (PR #442) | `templates/auto-provisioned-secrets.yaml` -- pure Helm template |
| `<fullname>-bundle-signing` | `autoProvisionedKeys.bundleSigning` | hub-api bundle signing (PR #431) | `templates/auto-provisioned-keys-job.yaml` -- pre-install/pre-upgrade hook Job (openssl) |
| `service-jwt-signing-key` | `autoProvisionedKeys.serviceJwt` | hub-api machine JWTs (PR #438) | `templates/auto-provisioned-keys-job.yaml` -- pre-install/pre-upgrade hook Job (openssl) |

**Policy, every Secret:**
1. **Keep** -- if the Secret already exists, it is left completely untouched (never
   regenerated/rotated by this chart). It is also annotated `helm.sh/resource-policy:
   keep` when this chart creates it, so it survives `helm uninstall`.
2. **Generate** -- on a miss, auto-generated ONLY when `global.deploymentTier` is
   `alpha` or `local`.
3. **Fail** -- on a miss anywhere else (beta/gamma/production), the release
   render-fails with an explicit message, UNLESS `autoProvisionedKeys.<key>.
   externalSecret: true` is set (an ExternalSecret/SealedSecret/other out-of-band
   controller owns that Secret name) or an operator pre-creates it directly.

The two symmetric keys (`minioKms`, `tenantKek`) use `lookup` + Helm's own
cryptographically-secure `randBytes`/`sha256sum` template functions -- no extra
tooling needed. The two Ed25519 keys (`bundleSigning`, `serviceJwt`) need an
`openssl`-equipped hook Job instead: Sprig/Helm has no function to derive an Ed25519
public key from a private key, which both public-key-flow requirements below need.

**Public keys flow to consumers automatically, private keys never do:** the hook Job
derives each Ed25519 public key from its Secret and publishes it to a plain
ConfigMap (`<fullname>-bundle-signing-public`, `<fullname>-service-jwt-public` by
default) containing ONLY public key material -- safe for any pod to mount via
`envFrom`/`configMapKeyRef`. The private key Secret itself is read only by this hook
Job (to derive the public key) and by hub-api (the sole signer/issuer); no other
Deployment's RBAC or volume mounts reference it. See `templates/
auto-provisioned-keys-job-rbac.yaml` for the least-privilege Role backing this.

**Validating (no live cluster required):**
```bash
helm lint k8s/helm/waddlebot
helm template waddlebot k8s/helm/waddlebot -f k8s/helm/waddlebot/values-alpha.yaml --kube-version 1.30.0   # renders, keys generated
helm template waddlebot k8s/helm/waddlebot -f k8s/helm/waddlebot/values-local.yaml --kube-version 1.30.0   # renders, keys generated
helm template waddlebot k8s/helm/waddlebot -f k8s/helm/waddlebot/values-beta.yaml  --kube-version 1.30.0   # FAILS closed (no external secret / pre-existing Secret)
```
`lookup` always returns empty in `helm template`/`--dry-run` (no live API server to
query), so every `helm template` run takes the "not found" branch -- this validates
that generation renders correctly and that the fail-guard fires, but it does NOT by
itself prove keep-on-second-run behavior against a real release (two `helm template`
runs will in fact mint two *different* random secret values each time, since both
independently take the "generate" branch -- that's expected, not a bug). Real
keep/idempotency is exercised by `lookup` against the live cluster during an actual
`helm install`/`helm upgrade`, i.e. it is proven at deploy time, not template time.

### Enterprise SSO key (`sso.*`, `templates/sso.yaml`)

hub-api's enterprise SSO (SAML 2.0 / OIDC / Google; see `docs/SSO.md`) needs one more symmetric
key, `SSO_ENCRYPTION_KEY` (64 lowercase hex chars) in Secret `waddlebot-sso-encryption-key`. It follows the
same keep/generate mechanism as `tenantKek`/`oneTimeSecret` above -- `lookup` + `randBytes`, generated
in `alpha`/`local` only, `helm.sh/resource-policy: keep`, never rotated by the chart -- with one
deliberate difference: **a miss in beta/gamma/production does NOT fail the release.** SSO is
entitlement-gated, so a deployment that never enables it must still install. hub-api consumes the key
through an `optional: true` `secretKeyRef` (one `include "waddlebot.sso.hubApiEnv"` in
`templates/hub-api.yaml`; the env block lives in `templates/_sso.tpl`), and SSO fails loudly at request
time (HTTP 503 `SSO_UNAVAILABLE`) until the Secret exists. Enable SSO outside alpha/local by
pre-creating the Secret, or by pointing an ExternalSecret at it and setting
`sso.encryptionKey.externalSecret=true`. Other values: `sso.allowedPrivateHosts` (operator allowlist for
on-prem IdPs behind the SSRF guard), `sso.stateTtlSeconds`, `sso.clockSkewSeconds`, and
`sso.google.existingSecret` (optional shared Google OAuth client -- never inline the values).
Tests: `tests/test_sso_render.py`.

## SPIRE (optional, disabled pending #437)

`spire.enabled` defaults to `false` at the chart level. `values-alpha.yaml` and
`values-local.yaml` also pin it `false` -- SPIRE server/agent crash-loops in that
environment today (https://github.com/penguintechinc/waddles/issues/437), which made
every `helm upgrade` fail via the `waddlebot-spire-auto-enroll` post-upgrade hook Job,
pinning the whole release in `FAILED` status. With `spire.enabled: false` the entire
`waddlebot-spire` subchart (including that hook) no-ops out of the render
(`condition: spire.enabled` in `Chart.yaml`), so it can no longer block a rollout.
Flip back to `true` once #437 is resolved and the hook has been verified green in a
scratch namespace first.

### Recovering a release already stuck FAILED because of this

```bash
# Option A: roll back to the last successful revision
helm history waddlebot --namespace waddlebot
helm rollback waddlebot <last-good-revision> --namespace waddlebot

# Option B: upgrade in place once spire.enabled=false is in the values file being applied
# (the failing hook simply won't render this time, so the upgrade can complete)
helm upgrade waddlebot k8s/helm/waddlebot --namespace waddlebot -f k8s/helm/waddlebot/values-alpha.yaml
```
Option B is sufficient on its own -- a `FAILED` release is not "stuck" in the sense of
refusing further upgrades, it just means the *previous* revision's hook failed; the
next `helm upgrade` (hook now absent) is a normal upgrade attempt from that state.

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

### Bundle Artifact Signing Rollout Order

`feature/bundle-artifact-signing`: hub-api signs each approved bundle with a
platform Ed25519 key; the bundle-executor(s) verify that signature before
instantiating any component and fail closed with no configured key.

**fix/chart-autogen-bundle-signing-key: steps 1 and 4 below are now automatic in
every environment** -- `templates/auto-provisioned-keys-job.yaml`'s pre-install/
pre-upgrade hook keeps the `<release>-bundle-signing` Secret and
`<release>-bundle-signing-public` ConfigMap in sync on every `helm install`/
`helm upgrade` (generating the Secret itself on a miss in alpha/local; requiring
a pre-created Secret or `externalSecret: true` elsewhere), and
`bundle-executor`/`bundle-executor-action` now source `BUNDLE_SIGNING_PUBLIC_KEYS`
from that ConfigMap by default -- see "Self-provisioned platform keys" above.
`make generate-bundle-signing-key` and
`pipeline.rustDataPlane.bundleSigningPublicKeys` remain available as an explicit
override (multi-key rotation, or pre-creating the Secret yourself in beta/
gamma/production), but a plain `helm upgrade` with no manual step now succeeds
end-to-end. Remaining manual step, per environment:

1. **Run the backfill Job once**, for any already-approved version signed
   before this feature existed:
   ```bash
   helm upgrade <release> . -f values-<env>.yaml --set pipeline.hubApi.bundleSigningBackfill.enabled=true
   # then flip it back to false for the next normal upgrade
   ```
   Idempotent -- a stray extra run is a no-op for every already-signed row.

The `<release>-bundle-signing-reconciler` CronJob (enabled by default, every 30
minutes) separately catches a signed-in-Postgres row whose bucket sidecar
upload failed after the fact -- no manual step required for that one.

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
