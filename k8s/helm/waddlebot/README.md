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
| `global.imagePullPolicy` | Image pull policy for **every** workload container, including `hub-api`, `svc-{action,ingest,process}-rust`, `svc-presentation` and both `svc-bundles-executor` Deployments (no template hardcodes a policy) | `IfNotPresent` |
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

### Cluster security posture (R5: securityContext / PSA / Tetragon / egress / port-scoped ingress)

Shipping (beta/gamma/production) baseline; alpha is exempt from the cluster-level controls. Helpers:
`templates/_networkpolicy.tpl` (`waddlebot.workloadPolicies`, included at the end of every workload
template so the policies exist exactly when the workload does). Tests:
`tests/test_security_posture_r5_render.py`.

| Control | What the chart renders | Knobs |
|---|---|---|
| securityContext | Every container (init containers too) of every Deployment/StatefulSet/Job/CronJob: non-root, no privilege escalation, `drop: [ALL]`, `RuntimeDefault` seccomp. Collectors, postgres (uid 999), redis/valkey (uid 999), qdrant and ollama (uid 1000, model store moved to `$HOME/.ollama` on the same PVC) are now rootless with a **read-only root filesystem** (emptyDirs for `/tmp`, socket/snapshot dirs) | `infrastructure.{postgresql,redis,qdrant,ollama}.{podSecurityContext,securityContext}`; app pods use the global `podSecurityContext`/`securityContext` |
| Pod Security Admission | `pod-security.kubernetes.io/{enforce,audit,warn}: restricted` (+ `-version: latest`) on the chart-managed Namespace. **Only when `namespaceCreate: true`** -- operators who pre-create the namespace must apply the same labels. beta enforces `privileged` (audit/warn stay `restricted`) because the SPIRE agent is an approved ROOT EXCEPTION (uid 0 + hostPath) that PSA rejects at every level; the chart `fail`s if you set restricted/baseline with `spire.enabled=true`. local enforces `baseline` | `podSecurity.{enabled,enforce,audit,warn,version}` |
| Tetragon | One `TracingPolicyNamespaced` per app workload: `sys_execve` kprobe, **Sigkill** on any binary outside the workload's runtime allowlist (interpreter/entrypoint only; shells only for the documented legacy `sh -c` images). Needs the Tetragon agent + CRD in the cluster. Infra images (postgres/redis/qdrant/ollama/seaweedfs/posthog) are not covered (multi-process entrypoints). alpha/local/ci: off | `tetragon.{enabled,action,commonBinaries,runtimes,shellBinaries,runcInit}`; per-workload `binaries`/`shell` args at the include. `runcInit` (default on) keeps exec probes + `kubectl exec` working: runc >= 1.2 execs `/proc/self/fd/<N>` for `runc init`, so a second selector exempts that prefix ONLY when the caller binary is in `runcInit.callerBinaries` (runc paths) and SIGKILLs it from anything else (no fileless memfd exec) -- the allowlist itself gains no binaries. `action: Post` is an audit-only escape hatch for validating a new image's exec set -- never ship it |
| Default-deny egress | Every long-running workload is selected by a `CiliumNetworkPolicy` with an `egress` section: DNS + own namespace (the destination's ingress policy scopes the port) + one outbound profile -- `internal` (nothing else), `https` (TCP/443 to public addresses, or pinned FQDNs), `world` (any port, public only: svc-streaming, svc-rtc). Data-plane pods stay governed by `<release>-dataplane-egress-deny` (egress-proxy only). **RFC1918, CGNAT, link-local/169.254.169.254, loopback and the cluster CIDRs are never reachable** through the world rules; only the egress-proxy's per-tenant private-ip grants reach private space. Jobs/CronJobs are not covered (no long-running listener) | `egressPolicy.{workloadEgress.enabled,privateCidrs,httpsFqdns}`, `networkPolicy.egressProfileOverrides.<component>` (e.g. `seaweedfs: https` with an external KMS) |
| Port-scoped ingress | `default-deny-ingress` CNP for the namespace, plus a per-workload CNP admitting only that workload's declared `containerPort`(s) from the same namespace. `networkPolicy.externalIngressComponents` additionally admit the ingress controller (and Gateway API, when `httproute.enabled`) **on those ports only**. hub-api's internal gRPC port is deliberately not in the namespace-wide rule: only the callers in `<release>-hub-api-grpc` reach it (this closes the previously documented gap) | `networkPolicy.{enabled,ingressNamespace,externalIngressComponents}` |
| Cilium only | No plain `NetworkPolicy` is rendered anywhere (the old `default-deny-ingress` / `allow-same-namespace-and-ingress` pair is replaced) | -- |

- **Alpha** (`values-alpha.yaml`): `networkPolicy.enabled=false`, `egressPolicy.workloadEgress.enabled=false`, `tetragon.enabled=false` -- NodePort Services are reached from the host and the cluster has no Tetragon. Pod specs are still PSA-restricted-compliant.
- **First rollout of a new image:** the Tetragon allowlists are derived from each image's `ENTRYPOINT`/`CMD`. Roll out beta with `--set tetragon.action=Post`, read Tetragon's events for unexpected `execve`s, add them to `tetragon.runtimes.*`/the include's `binaries`, then ship `Sigkill`.
- **Upgrading from a pre-R5 release (redis):** the old pod ran as root and left root-owned AOF/RDB files in `/data`; volumes without fsGroup support (kind/local-path, MicroK8s hostpath) never chown them, so the rootless pod crash-looped. valkey now persists under `/data/valkey`, created by a non-root `migrate-data-dir` initContainer that copies any pre-R5 `appendonlydir`/`dump.rdb` across once (loud on failure). Verified on a kind merge-base -> PR upgrade (key written pre-upgrade is readable after). postgres needs no migration (its entrypoint already owned PGDATA as uid 999).
- **Extending exposure:** add the component to `networkPolicy.externalIngressComponents` to publish it through the ingress/Gateway; never widen the same-namespace rule.
- **Known follow-ups:** the SPIRE subchart (`charts/spire`, packaged as a tracked `.tgz`) ships its own CNPs with an unscoped `host/remote-node` ingress rule and a root hostPath agent; moving SPIRE to its own namespace would let beta enforce `restricted`.

### Availability: shutdown, startup, autoscaling, disruption budgets

Helpers: `templates/_resilience.tpl`, `templates/_autoscaling.tpl`; HPA/PDB: `templates/autoscaling.yaml`.
Tests: `tests/test_scale_and_shutdown_hardening_render.py`.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `shutdown.terminationGracePeriodSeconds` | Pod drain budget before SIGKILL, every Deployment (must exceed the preStop sleep) | `30` |
| `shutdown.preStopSleepSeconds` | `preStop` sleep so the pod leaves Service endpoints before SIGTERM (`0` = off). Native `sleep` action on k8s >= 1.30, `exec sleep` below | `5` |
| `startupProbe.periodSeconds` / `.failureThreshold` | Startup budget (30 x 5s = 150s) for hub-api, the Rust data-plane stages, the Python pipeline stages, svc-streaming, svc-presentation | `5` / `30` |
| `autoscaling.defaults.*` | HPA defaults: `enabled` (off), `minReplicas`, `maxReplicas`, `targetCPUUtilizationPercentage`, optional `targetMemoryUtilizationPercentage` / `behavior` | off, `2`, `10`, `70` |
| `autoscaling.services.<key>.*` | Per-service override (field-by-field over `defaults`); production enables it for hub-api, hub-webui, svc-{action,process}-rust, the single svc-bundles-executor, reputation | `{}` |
| `podDisruptionBudget.enabled` / `.minAvailable` / `.maxUnavailable` | PDB for every registered service that effectively runs >1 replica (`services.<key>` overrides) | `true` / `1` / `""` |

- HPA keys: `hubApi hubWebui svcCore svcProcess svcAction svcProcessRust svcActionRust bundleExecutor reputation router`. PDB-only (never an HPA): `svcIngest svcIngestRust egressProxy`. Needs metrics-server + container resource requests.
- When a service's HPA is enabled its Deployment omits `spec.replicas`, so `helm upgrade` never resets the autoscaler.
- A PDB is skipped for a single effective replica (it would block node drains), so alpha/beta/gamma render none.
- **Singletons are never autoscaled or budgeted:** `svc-streaming` (stateful RTMP/SRT + stream dir, `strategy: Recreate`, replicas 1, 60s grace) and `svc-presentation` (overlay fan-out is in-process).
- **svc-presentation must stay at 1 replica.** With >1, a pushed frame only reaches clients on the pod that received it. The chart fails the render for `pipeline.svcPresentation.replicas > 1` unless `pipeline.svcPresentation.sharedFanout.enabled=true` -- the shared fan-out backend is not built yet (roadmap), so do not set it.

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
`infrastructure.postgresql.serviceRoles.roles`. Passwords are generated at runtime on first run in **every tier** (see
"Secrets: generated on first run"); `serviceRoles.existingSecret` / `serviceRoles.passwords` remain as overrides. The owner credential is a separate Secret (`<release>-db-admin`) read only by Postgres and
the db-migrate hook. See [`docs/DATABASE_CREDENTIALS.md`](../../../docs/DATABASE_CREDENTIALS.md) for rotation,
the burned-credential list, and the upgrade path.

## Secrets: generated on first run

Every secret the platform generates and consumes itself is created **at runtime, create-if-absent, in
every tier** (alpha, beta, gamma, production, local) by two pre-install/pre-upgrade hook Jobs. A fresh
`helm install` needs no manual secret in any tier; only external credentials (below) are supplied by the owner.

* `templates/provision-secrets.yaml` + `files/provision-secrets.sh` -- every symmetric secret, password and HMAC ring.
* `templates/auto-provisioned-keys-job.yaml` -- Ed25519 bundle-signing and ECDSA P-256 service-JWT key pairs (+ public-key ConfigMaps).

**Why not Helm `lookup`:** `lookup` is empty under `helm template | kubectl apply` / ArgoCD, so a template-time
generator would regenerate *every* value (all DB passwords, the root KEK) on each sync. The chart now contains **no**
`lookup`-based generation; Helm never owns these Secrets' data. (TLS/CA certificate Secrets -- `transit-tls`, `host-api-tls`,
`hub-api-grpc-tls`, `valkey-tls`, connector runtime -- still use template-time `lookup`; they are a separate follow-up.)

**Policy (enforced in the script, proven by `tests/test_provision_secrets_script.py`):**
1. Existing real value => untouched. Never overwritten, never rotated by an upgrade. A different explicit values override is ignored with a warning (no values logged); to re-seed a key, delete that key from the Secret.
2. A key is *unset* only if absent, empty or `REPLACE_ME*`, and only for the keys listed below (the chart-owned keys); other keys in the same Secret are never touched. `waddlebot-platform-credentials` is owner-supplied and never touched.
3. Unset key: explicit value from values (`global.*`, `infrastructure.*.password`, ...) > validated import from a *named* legacy Secret (Postgres owner password from `waddlebot-secrets`, so an existing PVC keeps working; placeholder/default-looking values are refused, never imported) > fresh CSPRNG value.
4. Concurrency-safe: a missing Secret is written with `kubectl create` (AlreadyExists => adopt the winner), an existing one with a merge patch carrying `resourceVersion` (Conflict => re-read). Two runs never leave two values.
5. Opt-outs kept: `autoProvisionedKeys.<key>.externalSecret`, `sso.encryptionKey.externalSecret`, `postgresql.admin.existingSecret`, `serviceRoles.existingSecret`, `*.existingSecret` => logged, never touched.
6. Created Secrets carry `helm.sh/resource-policy: keep` and ArgoCD `Prune=false` / `IgnoreExtraneous`. Existing ones get those annotations on the first run (so Helm does not delete the no-longer-rendered object on the first upgrade).
7. No value ever reaches argv, stdout/stderr or a ConfigMap: values move through bash variables, a memory-backed `emptyDir` and stdin/`--patch-file`; only Secret and key *names* are logged. No `set -x`.
8. Entropy: OpenSSL `rand` (DRBG seeded from the OS CSPRNG, `/dev/urandom`). Passwords/keys: 32 bytes (256 bit) base64url (43 chars, never a leading `-`); legacy JWT/module/service keys 48 bytes; KEKs/HMAC keys 32 bytes hex (64 chars; rings `kid:<64 hex>`); S3 access key ids 16 alphanumerics; bootstrap admin password 24 alphanumerics (~143 bit).
9. Root-of-trust marker: `waddlebot-tenant-kek` is stamped with annotation `waddles.penguintech.io/root-marker = v1.<kek_id>.<hmac>` where `kek_id` = first 8 bytes of SHA-256(KEK) as hex (identical to hub-api `envelope.crypto.kek_id`) and `hmac` = hex HMAC-SHA256(key = the 32 raw KEK bytes, message = `waddles-root-check`). A present-but-different marker makes the hook (and so the release) **fail** -- a replaced root is never accepted silently. hub-api stores the matching value in its DB marker.
10. Hook RBAC (`<fullname>-provision-secrets`, weight -10, deleted after the pre-* phase succeeds): `get`,`patch` only on the named Secrets below; `create` on Secrets without a name restriction -- **residual risk:** Kubernetes ignores `resourceNames` on `create`, so for the seconds the hook lives it could create a Secret of any name. No list/watch/update/delete, no ConfigMaps, token automounted on the Job pod only. Image pinned by digest (`autoProvisionedKeys.hookImage`).

### Generated secrets (name -> keys)

| Secret | Keys (generated if unset) | Consumers |
|---|---|---|
| `<fullname>-db-admin` | `POSTGRES_PASSWORD` | Postgres only (skipped if `postgresql.admin.existingSecret`) |
| `<fullname>-db-credentials` | `PW_<ROLE>` per service role, `PW_WADDLES_PLATFORM_TABLE` (if enabled) | each pod's own key |
| `waddlebot-secrets` | `DB_READER_PASSWORD`, `DB_REPUTATION_PASSWORD`, `REDIS_PASSWORD`, `REDIS_ADMIN_PASSWORD`, `REDIS_{ROUTER,HUB,WORKFLOW,AI,ANALYTICS,SECURITY,LOYALTY,SPOTIFY,YTMUSIC,TWITCH,DISCORD,SLACK}_PASSWORD`, `VALKEY_DURABLE_PASSWORD`, S3 identities `{HUB_API,DATAPLANE,BUCKET_INIT,STREAMING,BUNDLE_STORAGE}_S3_{ACCESS_KEY_ID,SECRET_ACCESS_KEY}`, `JWT_SECRET` (+`SECRET_KEY`, `JWT_SECRET_KEY` derived), `MODULE_SECRET_KEY`, `SERVICE_API_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, `ENVELOPE_BINDING_KEYS` | envFrom (~40 workloads) |
| same | derived every run (not generated): `REDIS_URL`, `VALKEY_URL`, `VALKEY_URL_TLS`, `VALKEY_DURABLE_URL[_TLS[_AUTH]]`, chart config keys (`AWS_REGION`, `OPENWHISK_*`, ...) | |
| `<fullname>-valkey-durable-admin` | `VALKEY_DURABLE_ADMIN_PASSWORD` | durable Valkey only |
| `<fullname>-valkey-kv` | `VALKEY_KV_PASSWORD` (+ derived URLs) | bundle-kv Valkey, svc-action/process |
| `<fullname>-hub-admin-bootstrap` | `INITIAL_ADMIN_PASSWORD` | hub-admin-seed |
| `seaweedfs-sse-kek` | `WEED_S3_SSE_KEK` | SeaweedFS SSE-S3 |
| `waddlebot-tenant-kek` | `TENANT_KEK_HEX` (root KEK, root marker) | hub-api |
| `waddlebot-ots-encryption-key` | `ONE_TIME_SECRET_ENCRYPTION_KEY` | hub-api |
| `waddlebot-sso-encryption-key` | `SSO_ENCRYPTION_KEY` (if `sso.enabled`) | hub-api |
| `<fullname>-bundle-admin-request`, `-outbound-pin`, `-event-sync-request` | `BUNDLE_ADMIN_REQUEST_KEYS`, `OUTBOUND_PIN_KEYS`, `EVENT_SYNC_REQUEST_KEYS` (HMAC rings) | hub-api / svc-* |
| `posthog-secrets` | `SECRET_KEY`, `POSTGRES_PASSWORD`, `CLICKHOUSE_PASSWORD` (if in-cluster PostHog without `existingSecret`) | PostHog |
| `<fullname>-bundle-signing` | `BUNDLE_SIGNING_PRIVATE_KEY` (Ed25519) + public ConfigMap | hub-api / executor |
| `service-jwt-signing-key` | `SERVICE_JWT_PRIVATE_KEY_<kid>` (ES256) + public ConfigMap | hub-api |
| `<fullname>-db-migrate-secret` | derived (`DATABASE_URL`, role passwords JSON, ...) re-applied every run | db-migrate |

Placeholder handling: keys above holding `REPLACE_ME*` or an empty string are treated as unset and replaced; nothing else is.

**Owner-supplied (never generated):** Discord / Twitch / YouTube app credentials and the Discord bot token (Secret `waddlebot-platform-credentials`, see `docs/PLATFORM_CREDENTIALS.md`); per-tenant own-app credentials (tenant settings / `platform_connections`); Google OAuth and SAML SSO provider credentials (`sso.google.existingSecret`, IdP metadata); PostHog API key; license server key; Odoo and OpenWhisk credentials; registry pull credentials (`imagePullSecrets`); Cloudflare / DigitalOcean / CI credentials; TLS certificates for beta/gamma/production (`global.*Tls`, cert-manager or ExternalSecret).

**Seam for the platform secret store:** these class-A K8s Secrets stay generated here. hub-api escrows a sealed copy + keyed fingerprint into the `platform_secrets` table / S3 escrow at bootstrap and alerts on drift -- it never writes these Secrets (its ServiceAccount has no Secret write).

**Existing alpha clusters:** the Secrets already hold real values, so the first upgrade is a no-op for values: the hook logs `unchanged`/`patched (annotations only)` per Secret. Verify with `kubectl logs job/<fullname>-provision-secrets` (a failed Job is kept; a successful one is deleted -- check `helm get hooks` or run with `--debug`).

**Validating (no live cluster required):**
```bash
helm lint k8s/helm/waddlebot -f k8s/helm/waddlebot/values-production.yaml
helm template waddlebot k8s/helm/waddlebot -f k8s/helm/waddlebot/values-production.yaml --kube-version 1.30.0   # renders in every tier, no manual secrets
helm template ... | python3 scripts/ci/check-helm-hook-dependency-ordering.py
python3 -m pytest k8s/helm/waddlebot/tests/test_provision_secrets_script.py k8s/helm/waddlebot/tests/test_provision_secrets_render.py
```

## Self-provisioned platform keys (feature/helm-auto-provision-keys)

Key-pair Secrets (`<fullname>-bundle-signing`: Ed25519, `service-jwt-signing-key`: ECDSA P-256 / ES256) are generated by
`templates/auto-provisioned-keys-job.yaml`, an `openssl`-equipped pre-install/pre-upgrade hook Job (Sprig/Helm has no
function to derive an Ed25519 public key). Same create-if-absent policy as above, in every tier (the former alpha/local-only
gate is removed); `externalSecret: true` opts out. See "Secrets: generated on first run" for the symmetric secrets.

**Public keys flow to consumers automatically, private keys never do:** the hook Job
derives each public key from its Secret and publishes it to a plain
ConfigMap (`<fullname>-bundle-signing-public`, `<fullname>-service-jwt-public` by
default) containing ONLY public key material -- safe for any pod to mount via
`envFrom`/`configMapKeyRef`. The private key Secret itself is read only by this hook
Job (to derive the public key) and by hub-api (the sole signer/issuer). See `templates/
auto-provisioned-keys-job-rbac.yaml` for the least-privilege Role backing this.

### Enterprise SSO key (`sso.*`, `templates/sso.yaml`)

hub-api's enterprise SSO (SAML 2.0 / OIDC / Google; see `docs/SSO.md`) needs one more symmetric
key, `SSO_ENCRYPTION_KEY` (64 lowercase hex chars) in Secret `waddlebot-sso-encryption-key`. It follows the
same runtime create-if-absent mechanism as `tenantKek`/`oneTimeSecret` above (generated in every tier by the
provision-secrets hook, `helm.sh/resource-policy: keep`, never rotated) when `sso.enabled`. hub-api consumes the key
through an `optional: true` `secretKeyRef` (one `include "waddlebot.sso.hubApiEnv"` in
`templates/hub-api.yaml`; the env block lives in `templates/_sso.tpl`), and SSO fails loudly at request
time (HTTP 503 `SSO_UNAVAILABLE`) if the Secret is missing. To manage the key yourself, point an
ExternalSecret at it and set `sso.encryptionKey.externalSecret=true`. Other values: `sso.allowedPrivateHosts` (operator allowlist for
on-prem IdPs behind the SSRF guard), `sso.stateTtlSeconds`, `sso.clockSkewSeconds`, and
`sso.google.existingSecret` (optional shared Google OAuth client -- never inline the values).
Tests: `tests/test_sso_render.py`.

## Enterprise external KMS / BYOK (`kms:`, default OFF)

Customer-managed encryption keys, **on top of** the platform-managed at-rest baseline (never a substitute).
`kms.enabled=false` renders nothing, so the zero-config baseline is unchanged. Full guide, per-provider
setup (AWS KMS / Google Cloud KMS / Azure Key Vault), failure behaviour and the object-storage exit ramp:
[`docs/guides/external-kms-byok.md`](../../../docs/guides/external-kms-byok.md). Templates:
`templates/kms.yaml` (non-secret `-kms` ConfigMap + validation), `templates/_kms.tpl` (helpers; the only
edits to shared templates are one-line `include` hooks in `hub-api.yaml` and `infrastructure/seaweedfs.yaml`).

| Value | Description | Default |
|-------|-------------|---------|
| `kms.enabled` | Master switch; required by both sub-features (a sub-feature without it **fails the render**) | `false` |
| `kms.tenantKeys.enabled` | hub-api accepts per-tenant customer keys (tenants opt in over `/api/v1/tenant/<slug>/kms`) | `false` |
| `kms.tenantKeys.providers` | Any of `aws_kms`, `gcp_kms`, `azure_key_vault`; unlisted providers are refused | `[]` |
| `kms.tenantKeys.aws.credentialsSecret` | Optional Secret (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`); empty = IRSA / Pod Identity | `""` |
| `kms.tenantKeys.gcp.credentialsSecret` | Optional Secret (`ENVELOPE_GCP_CREDENTIALS_JSON`); empty = Workload Identity | `""` |
| `kms.tenantKeys.azure.credentialsSecret` | **Required** with `azure_key_vault` (`ENVELOPE_AZURE_CLIENT_ID`/`ENVELOPE_AZURE_CLIENT_SECRET`) | `""` |
| `kms.tenantKeys.{aws,gcp}.platformPrincipal` | Public identity customers must trust/grant (shown by `GET .../kms`) | `""` |
| `kms.tenantKeys.aws.{kms,sts}EndpointUrl`, `gcp.kmsEndpoint`, `azure.authority` | Optional `https://` overrides | `""` |
| `kms.objectStorage.mode` | SeaweedFS SSE-KMS: `off` \| `kms` \| `drain` (the ungated exit ramp) | `off` |
| `kms.objectStorage.provider` / `keyId` | `aws_kms` \| `gcp_kms` (no native Azure provider in SeaweedFS) / key ARN, alias or CryptoKey name | `""` |
| `kms.objectStorage.entitlementTenant` | Tenant whose Enterprise entitlement gates hub-api's `kms`-mode writes | `system` |
| `kms.objectStorage.aws.{region,endpoint,credentialsSecret.*}` | AWS KMS client for SeaweedFS (credentials from an existing Secret) | `""` |
| `kms.objectStorage.gcp.{projectId,credentialsSecret.*}` | Google Cloud KMS client for SeaweedFS (key file mounted read-only 0400) | `""` |

Helm cannot ask the licence server: the Enterprise entitlement is enforced at runtime by hub-api (per tenant
for tenant keys; at startup and on its own writes for object storage). Treat `kms.objectStorage.mode: kms`
as an Enterprise-only setting. Opt-in end-to-end check of the rendered SeaweedFS mapping against the pinned
image: `make test-seaweedfs-sse-kms`.

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
platform Ed25519 key; the svc-bundles-executor(s) verify that signature before
instantiating any component and fail closed with no configured key.

**fix/chart-autogen-bundle-signing-key: steps 1 and 4 below are now automatic in
every environment** -- `templates/auto-provisioned-keys-job.yaml`'s pre-install/
pre-upgrade hook keeps the `<release>-bundle-signing` Secret and
`<release>-bundle-signing-public` ConfigMap in sync on every `helm install`/
`helm upgrade` (generating the Secret itself on a miss in every tier; `externalSecret: true`
opts out), and
`svc-bundles-executor` now source `BUNDLE_SIGNING_PUBLIC_KEYS`
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

## Connector service bundles (Twitch / Discord / YouTube) -- publish + pin

The executor boot-pins `connectors/<p>/<digest>.wasm` plus a signed sidecar
`connectors/<p>/<digest>.json` (key in `BUNDLE_SIGNING_PUBLIC_KEYS`) and needs
`pipeline.connectors.digests.<p>` (feeds `CONNECTOR_PIN_DIGEST_<P>` and `SENDER_EXEC_DIGEST_<P>`).

- **At deploy (alpha/dal2alpha, `pipeline.connectors.publishAtSeed: true`)**: the core-bundle-seeder
  Job signs with the platform key it already holds and uploads the wasm built into its image, then
  logs the digests as a values snippet. Set `digests.<p>` from that log, then enable the connector.
- **By hand**: `make publish-connector-bundles ENV=dal2alpha` (`scripts/publish-connector-bundles.sh`) --
  needs `BUNDLE_SIGNING_KEY_FILE` (0600 file with the base64 seed -- user-held), `BUNDLE_SIGNING_KEY_ID`,
  `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
- Digests are build outputs and are not committed per environment. Verify with `make test-connector-publish`.

## Bundle executors (`svc-bundles-executor`): pools, sizing, language classes

The executor is the credential-less WASM sandbox host. It runs as **one StatefulSet per pool**
(`pipeline.rustDataPlane.bundleExecutor`): pod name = `EXECUTOR_ID` (downward API), a port-less
headless Service, `podManagementPolicy: Parallel`, and a per-pool PDB (`maxUnavailable: 1`).

### Why executors are separate (exception, to be revisited)

> **ARCHITECTURE EXCEPTION (approved 2026-11-14, revisit):** bundle executors run as separate
> pods instead of inside the three primary workflow services (gateway/ingest, processor, action).
> The owner has not settled whether this separation is right; it is an approved exception until
> revisited.

| Reason | What the separation buys |
|---|---|
| Isolation | Untrusted bundle code never shares a process, token mount or Secret with a stage. The pod has no `envFrom`, no API token, no listener (egress only to the stage host-API ports, the bundle bucket and DNS) and can optionally run in a Kata micro-VM (`bundleExecutor.runtimeClass`) without moving the stages. |
| Scaling | Executors scale on their own signals (stage queue-wait p95, pool exhaustion) via KEDA or HPA-external, independent of ingest/process/action replica counts. Slot sharding needs stable pod names, hence a StatefulSet. |
| Compile cost | Cranelift compiles are CPU/memory heavy and bursty (an alpha executor was OOMKilled at 16Gi). Keeping them out of the stages protects stage latency; AOT `.cwasm` artifacts make the cost a one-off. |

Cost of the exception: an extra network hop (mTLS host-API round trip) on every bundle
invocation, extra pods (and their idle requests), a second scaling/PDB surface, and the
slot-ownership protocol between stages and executors.

### Sizing policy (all environments)

- **Requests are the bare minimum that schedules and runs the pod healthily** (500m CPU / 1Gi
  memory; the idle footprint is not measured: dal2alpha has no metrics API). **Limits carry the headroom.**
- `EXECUTOR_MEMORY_BUDGET_MB` (the LRU ceiling for resident compiled modules) is checked against
  the memory **limit**, never the request: render fails unless `0 < budget < limit`. The request
  is only checked to be `<=` the limit.
- Overlays: dal2alpha 8 CPU / 24Gi limit per pod (budget 15360); beta/gamma/production 4 CPU /
  8Gi (budget 5120); local alpha 4 CPU / 2560Mi (budget 1536).
- Trade-off of low requests: pods are Burstable and the scheduler no longer reserves their
  working set, so a node under memory pressure evicts them first once the LRU cache has grown
  past the request. Executors therefore carry a soft `topologySpreadConstraints` on
  `kubernetes.io/hostname` (`spreadAcrossNodes`, default true) so replicas land on different
  nodes (a lost node must not take both owners of a slot).

### Language classes (`languageClasses`, values-driven)

```yaml
pipeline:
  rustDataPlane:
    bundleExecutor:
      languageClasses:
        compiled: [rust, csharp, go]
        scripted: [python, javascript, nodejs, typescript, ruby]
      pools:
        - { name: native, languageClass: compiled, stages: [svc-process-rust, svc-action-rust] }
        - { name: script, languageClass: scripted, stages: [svc-process-rust, svc-action-rust] }
```

- Languages are lower-case tokens; **a language may appear in exactly one class** (render fails
  otherwise). Adding a scripted language (for example `ruby`) is a values change only.
- A pool has `languageClass: compiled | scripted | any` (default `any`). Each executor pod gets
  `EXECUTOR_LANGUAGE_CLASS` and `EXECUTOR_LANGUAGES` (comma-separated; `any` = union of both
  classes). The executor validates them at startup (a typo or an empty list for a partitioned
  pool refuses to start) and reports a class-qualified `executor_id` (`compiled:<pod>` /
  `scripted:<pod>`) in `hello`; the stages (`bundle_active_set::partition`) read the same table
  from `EXECUTOR_LANGUAGE_CLASSES` (rendered from `languageClasses`) and only ever load/dispatch
  a bundle onto an executor whose class accepts its `app_versions.language`. A bundle whose
  language is in no class, or whose class has no live executor, is placed nowhere: the stage
  logs ERROR and counts `executor_partition_unroutable_total` (never a fallback to the other
  class). Nothing else changes for the default single `any` pool.
- alpha and dal2alpha ship both pools (`native` compiled, `script` scripted) with their own
  replicas, resources, PDB, `warmPoolSize` and memory budget; beta/gamma/production keep one `any`
  pool until their values opt in.
- **Warm instance pool** (`warmPoolSize`, `warmPoolMaxTotal`, `warmPoolIdleSecs`, per pool): the
  executor keeps up to `warmPoolSize` one-shot pre-instantiated guests per digest so an invoke
  skips instantiation. `0` = off (default). Each guest serves exactly one invocation, so no state
  crosses tenants. Worst-case warm memory is `warmPoolMaxTotal x EXECUTOR_MEMORY_LIMIT_MB`; keep it
  inside `limit - memoryBudgetMb` (a render test enforces this for the shipped envs). Kill switch:
  `FLAG_WADDLES_DISABLE_EXECUTOR_WARM_POOL=true`.
- **Drain concurrency** (`pipeline.rustDataPlane.drainConcurrency`, `drainOrdering`): svc-process and
  svc-action handle up to N ordering groups of a drained batch at once; entries sharing an ordering
  key (default: stream + actor) stay strictly in arrival order. `1` = the old serial loop.
- **Coverage rule:** every stage must be served by exactly one pool **per language class**; an
  `any` pool covers both classes. So the same stage may be listed by a compiled pool and a
  scripted pool, but not by two pools of the same class, and never by none. The rule lives in one
  helper (`waddlebot.executorPools`), the single place the partition layer extends.
- No ingest, build or seeding workflow for any language is part of the chart; this only
  classifies languages and renders the lists.

### AOT precompile prerequisites the executor consumes

Executors verify signed AOT `.cwasm` artifacts before loading them. Pods read
`PRECOMPILE_SIGNING_PUBLIC_KEYS` from the ConfigMap `<fullname>-executor-precompile-signing-public`
(`optional: false`: a missing ConfigMap fails the pod loudly with `CreateContainerConfigError`
instead of running unverified) and `EXECUTOR_PRECOMPILED_PREFIX` (default `precompiled`) names the
object prefix inside the bundle bucket. The ConfigMap is published by the key-provisioning hook;
on alpha/local the keypair is generated, on beta/gamma/production the private-key Secret must
be pre-created or delegated (`autoProvisionedKeys.precompileSigning.externalSecret: true`).
## Kata isolation for the bundle executor (`bundleExecutor.runtimeClass`, default OFF)

The `svc-bundles-executor` StatefulSet pools run untrusted WASM bundles and can optionally run
inside Kata Containers micro-VMs. Every other data-plane pod stays on standard containers with
enforced Tetragon exec allowlists (unchanged). **No values file enables Kata** (a test pins it).

```yaml
bundleExecutor:
  runtimeClass: { enabled: true, name: kata }
  nodeSelector: { katacontainers.io/kata-runtime: "true" }   # KVM-capable nodes
```

- Renders `runtimeClassName` on every executor pool only when enabled; enabled with an empty
  `name` fails the render (never a silent fallback to runc; a missing RuntimeClass leaves the pod Pending).
- `nodeSelector` / `affinity` apply to the executor only, and only when enabled (default empty).
- `runtimeClass.resources` + `runtimeClass.memoryBudgetMb` replace the per-pool executor
  resources and LRU budget when enabled; the budget is checked against the override's memory
  LIMIT, so size both together per cluster. The RuntimeClass `overhead` (guest kernel + agent) is
  added to the pod request by Kubernetes automatically; size resources for the workload only.
- Tetragon: inside a Kata guest the host's Tetragon cannot see the executor's `execve` calls, so
  a Kata executor **omits** its `TracingPolicyNamespaced` (the micro-VM boundary is the control)
  and **keeps** its `CiliumNetworkPolicy`. Policies on all other data-plane pods are untouched.
- Extension seam: the Kata switch is global to the executor. Per-pool Kata (for example only the
  scripted pools) needs the executor Tetragon policy selected per pool label first; both are
  localised to `waddlebot.executorEffective` and the `workloadPolicies` call in
  `templates/svc-bundles-executor.yaml`.
- Prerequisite: the `RuntimeClass` and KVM nodes are cluster-admin managed (not by this chart).
## Helm matrix gate (`make test-helm-matrix`)

One command runs what CI and review need for this chart: for every helm major present (v3 and v4,
the chart must work with both) and every environment (alpha, dal2alpha, beta, gamma, production,
local, ci) it runs `helm lint`, `helm template` and `kubeconform -strict`, then the render
scripts in `scripts/ci/` (hook dependency ordering, placeholder credentials, manifest invariants,
seeder Valkey TLS), `check_helm_hook_phases.py`, `check_wait_for_hub_api.py`, the executor render
test and the chart tests. It prints counts, and zero items examined (no helm, no documents, no
hooks, no passing tests) is a failure. `make test-helm-matrix-quick` skips the chart tests.
Point it at specific binaries with `HELM3=` / `HELM4=`; keep renders and logs with
`HELM_MATRIX_OUT=dir`. Beta/gamma/production/ci render with `--set global.deploymentTier=alpha`,
as CI does, because those tiers need real secrets.
