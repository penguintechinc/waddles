{{/*
WaddleBot Helm Chart Helper Templates

This file contains reusable template helpers for the WaddleBot Helm chart.
These helpers ensure consistency across all Kubernetes resources and simplify
template maintenance.
*/}}

{{/*
Expand the name of the chart.
Returns the chart name, truncated to 63 characters.
*/}}
{{- define "waddlebot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "waddlebot.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "waddlebot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
Generates standard Kubernetes labels following app.kubernetes.io conventions.
*/}}
{{- define "waddlebot.labels" -}}
helm.sh/chart: {{ include "waddlebot.chart" . }}
{{ include "waddlebot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels
These labels are used for pod selectors and must remain consistent.
*/}}
{{- define "waddlebot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "waddlebot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Namespace name
Returns the namespace where resources should be created.
*/}}
{{- define "waddlebot.namespace" -}}
{{- if .Values.namespaceOverride }}
{{- .Values.namespaceOverride }}
{{- else if .Values.global.namespace }}
{{- .Values.global.namespace }}
{{- else }}
{{- .Release.Namespace }}
{{- end }}
{{- end }}

{{/*
Image path with registry prefix
Constructs the full image path including registry, repository, and tag.
Usage: {{ include "waddlebot.image" (dict "image" .Values.modules.router "global" .Values.global "defaultTag" .Chart.AppVersion) }}
*/}}
{{- define "waddlebot.image" -}}
{{- $registry := .global.imageRegistry | default "" }}
{{- $repository := .image.repository | required "image.repository is required" }}
{{- $tag := .image.tag | default .defaultTag | default "latest" }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- else }}
{{- printf "%s:%s" $repository $tag }}
{{- end }}
{{- end }}

{{/*
Service Account name
Returns the name of the service account to use.
*/}}
{{- define "waddlebot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "waddlebot.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
PostgreSQL connection URL
Constructs the PostgreSQL connection URL from values.
Supports both external and internal PostgreSQL instances.
Format: postgresql://user:password@host:port/database
*/}}
{{- define "waddlebot.postgres.url" -}}
{{- if .Values.postgresql.enabled }}
{{- $host := printf "%s-postgresql" (include "waddlebot.fullname" .) }}
{{- $port := .Values.postgresql.service.port | default 5432 }}
{{- $user := .Values.postgresql.auth.username | default "waddlebot" }}
{{- $password := .Values.postgresql.auth.password | required "postgresql.auth.password is required" }}
{{- $database := .Values.postgresql.auth.database | default "waddlebot" }}
{{- printf "postgresql://%s:%s@%s:%v/%s" $user $password $host $port $database }}
{{- else }}
{{- $host := .Values.postgresql.external.host | required "postgresql.external.host is required when postgresql.enabled is false" }}
{{- $port := .Values.postgresql.external.port | default 5432 }}
{{- $user := .Values.postgresql.external.username | required "postgresql.external.username is required" }}
{{- $password := .Values.postgresql.external.password | required "postgresql.external.password is required" }}
{{- $database := .Values.postgresql.external.database | default "waddlebot" }}
{{- printf "postgresql://%s:%s@%s:%v/%s" $user $password $host $port $database }}
{{- end }}
{{- end }}

{{/*
PostgreSQL read replica connection URL
Constructs the PostgreSQL read replica connection URL from values.
Falls back to primary database URL if read replica is not configured.
*/}}
{{- define "waddlebot.postgres.readReplicaUrl" -}}
{{- if and .Values.postgresql.enabled .Values.postgresql.readReplica.enabled }}
{{- $host := printf "%s-postgresql-read" (include "waddlebot.fullname" .) }}
{{- $port := .Values.postgresql.readReplica.service.port | default 5432 }}
{{- $user := .Values.postgresql.auth.username | default "waddlebot" }}
{{- $password := .Values.postgresql.auth.password | required "postgresql.auth.password is required" }}
{{- $database := .Values.postgresql.auth.database | default "waddlebot" }}
{{- printf "postgresql://%s:%s@%s:%v/%s" $user $password $host $port $database }}
{{- else if and (not .Values.postgresql.enabled) .Values.postgresql.external.readReplica.enabled }}
{{- $host := .Values.postgresql.external.readReplica.host | required "postgresql.external.readReplica.host is required" }}
{{- $port := .Values.postgresql.external.readReplica.port | default 5432 }}
{{- $user := .Values.postgresql.external.username | required "postgresql.external.username is required" }}
{{- $password := .Values.postgresql.external.password | required "postgresql.external.password is required" }}
{{- $database := .Values.postgresql.external.database | default "waddlebot" }}
{{- printf "postgresql://%s:%s@%s:%v/%s" $user $password $host $port $database }}
{{- else }}
{{- include "waddlebot.postgres.url" . }}
{{- end }}
{{- end }}

{{/*
Redis connection URL
Constructs the Redis connection URL from values.
Supports both external and internal Redis instances.
Format: redis://[:password@]host:port[/database]
*/}}
{{- define "waddlebot.redis.url" -}}
{{- if .Values.redis.enabled }}
{{- $host := printf "%s-redis-master" (include "waddlebot.fullname" .) }}
{{- $port := .Values.redis.master.service.port | default 6379 }}
{{- $password := .Values.redis.auth.password | default "" }}
{{- $database := .Values.redis.database | default 0 }}
{{- if $password }}
{{- printf "redis://:%s@%s:%v/%v" $password $host $port $database }}
{{- else }}
{{- printf "redis://%s:%v/%v" $host $port $database }}
{{- end }}
{{- else }}
{{- $host := .Values.redis.external.host | required "redis.external.host is required when redis.enabled is false" }}
{{- $port := .Values.redis.external.port | default 6379 }}
{{- $password := .Values.redis.external.password | default "" }}
{{- $database := .Values.redis.external.database | default 0 }}
{{- if $password }}
{{- printf "redis://:%s@%s:%v/%v" $password $host $port $database }}
{{- else }}
{{- printf "redis://%s:%v/%v" $host $port $database }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Module image helper
Simplified image helper for module-specific images.
Uses global registry and chart version as defaults.
Handles both bare module names and legacy patterns with properly scoped registry.
Usage: {{ include "waddlebot.moduleImage" (dict "root" . "module" "router" "tag" .Values.modules.router.imageTag) }}
*/}}
{{- define "waddlebot.moduleImage" -}}
{{- $registry := .root.Values.global.imageRegistry | default "" }}
{{- $repository := .module | required "module name is required" }}
{{- $tag := .tag | default .root.Chart.AppVersion | default "latest" }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- else }}
{{- printf "%s:%s" $repository $tag }}
{{- end }}
{{- end }}

{{/*
Legacy Module Image (for templates still using global.imageRegistry concatenation)
Builds image reference for legacy modules with correct registry handling.
Same as moduleImage but kept separate for clarity in legacy template conversions.
Usage: {{ include "waddlebot.legacyModuleImage" (dict "root" . "module" "action-platforms" "imageTag" .Values.modules.actionPlatforms.imageTag) }}
*/}}
{{- define "waddlebot.legacyModuleImage" -}}
{{- $registry := .root.Values.global.imageRegistry | default "" }}
{{- $repository := .module | required "module name is required" }}
{{- $tag := .imageTag | default .root.Values.global.imageTag | default "latest" }}
{{- if $registry }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- else }}
{{- printf "%s:%s" $repository $tag }}
{{- end }}
{{- end }}

{{/*
Database host
Returns the PostgreSQL host name.
*/}}
{{- define "waddlebot.postgres.host" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "%s-postgresql" (include "waddlebot.fullname" .) }}
{{- else }}
{{- .Values.postgresql.external.host | required "postgresql.external.host is required when postgresql.enabled is false" }}
{{- end }}
{{- end }}

{{/*
Database port
Returns the PostgreSQL port.
*/}}
{{- define "waddlebot.postgres.port" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.service.port | default 5432 }}
{{- else }}
{{- .Values.postgresql.external.port | default 5432 }}
{{- end }}
{{- end }}

{{/*
Database name
Returns the PostgreSQL database name.
*/}}
{{- define "waddlebot.postgres.database" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.database | default "waddlebot" }}
{{- else }}
{{- .Values.postgresql.external.database | default "waddlebot" }}
{{- end }}
{{- end }}

{{/*
Database username
Returns the PostgreSQL username.
*/}}
{{- define "waddlebot.postgres.username" -}}
{{- if .Values.postgresql.enabled }}
{{- .Values.postgresql.auth.username | default "waddlebot" }}
{{- else }}
{{- .Values.postgresql.external.username | required "postgresql.external.username is required" }}
{{- end }}
{{- end }}

{{/*
Redis host
Returns the Redis host name.
*/}}
{{- define "waddlebot.redis.host" -}}
{{- if .Values.redis.enabled }}
{{- printf "%s-redis-master" (include "waddlebot.fullname" .) }}
{{- else }}
{{- .Values.redis.external.host | required "redis.external.host is required when redis.enabled is false" }}
{{- end }}
{{- end }}

{{/*
Redis port
Returns the Redis port.
*/}}
{{- define "waddlebot.redis.port" -}}
{{- if .Values.redis.enabled }}
{{- .Values.redis.master.service.port | default 6379 }}
{{- else }}
{{- .Values.redis.external.port | default 6379 }}
{{- end }}
{{- end }}

{{/*
Create the name of the config map for common configuration
*/}}
{{- define "waddlebot.commonConfigName" -}}
{{- printf "%s-config" (include "waddlebot.fullname" .) }}
{{- end }}

{{/*
Create the name of the secret for common secrets
*/}}
{{- define "waddlebot.commonSecretName" -}}
{{- printf "%s-secrets" (include "waddlebot.fullname" .) }}
{{- end }}

{{/*
Platform Credentials Secret Name (fix/helm-platform-credentials)
Returns the name of the Secret holding externally-issued third-party platform
credentials (Discord/Twitch/Slack/YouTube/Spotify/Kick/Teams/Mattermost/
Google Chat OAuth values, AWS/GCP, WaddleAI, PostHog API key). This chart
NEVER renders or writes this Secret -- it is created once by the operator
(alpha/local: `kubectl create secret generic`, see docs/PLATFORM_CREDENTIALS.md)
or by External Secrets/Sealed Secrets (beta/gamma/prod), and is only ever READ
via `secretRef`/`secretKeyRef` with `optional: true`. This is the fix for the
incident where `helm upgrade` overwrote the real alpha Discord bot token with
the values.yaml dev placeholder: a value this chart doesn't render, it can
never clobber.
*/}}
{{- define "waddlebot.platformCredentialsSecretName" -}}
{{- if .Values.platformCredentials.existingSecret }}
{{- .Values.platformCredentials.existingSecret }}
{{- else }}
{{- printf "%s-platform-credentials" (include "waddlebot.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Placeholder Guard (fix/helm-platform-credentials)
Fails the render if a value clearly intended as a dev-only placeholder
("REPLACE_ME", "CHANGE_ME", "changeme", "example") is about to be written into
a rendered Secret. Call as:
  {{- include "waddlebot.rejectPlaceholder" (dict "key" "SOME_KEY" "value" $someValue) }}
*/}}
{{- define "waddlebot.rejectPlaceholder" -}}
{{- $v := .value | toString }}
{{- if $v }}
{{- if regexMatch "(?i)(REPLACE_ME|CHANGE_ME|changeme|example)" $v }}
{{- fail (printf "refusing to render placeholder value for %q -- set a real value or remove the key (see docs/PLATFORM_CREDENTIALS.md)" .key) }}
{{- end }}
{{- end }}
{{- end }}

{{/*
API Key Secret Name
Returns the name of the secret containing API keys.
*/}}
{{- define "waddlebot.apiKeySecretName" -}}
{{- if .Values.apiKeys.existingSecret }}
{{- .Values.apiKeys.existingSecret }}
{{- else }}
{{- printf "%s-api-keys" (include "waddlebot.fullname" .) }}
{{- end }}
{{- end }}

{{/*
License Key Secret Name
Returns the name of the secret containing license keys.
*/}}
{{- define "waddlebot.licenseSecretName" -}}
{{- if .Values.license.existingSecret }}
{{- .Values.license.existingSecret }}
{{- else }}
{{- printf "%s-license" (include "waddlebot.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Ingress API Version
Returns the appropriate API version for Ingress based on Kubernetes version.
*/}}
{{- define "waddlebot.ingress.apiVersion" -}}
{{- if .Capabilities.APIVersions.Has "networking.k8s.io/v1" }}
{{- print "networking.k8s.io/v1" }}
{{- else if .Capabilities.APIVersions.Has "networking.k8s.io/v1beta1" }}
{{- print "networking.k8s.io/v1beta1" }}
{{- else }}
{{- print "extensions/v1beta1" }}
{{- end }}
{{- end }}

{{/*
Return true if cert-manager is enabled
*/}}
{{- define "waddlebot.certManager.enabled" -}}
{{- if and .Values.ingress.enabled .Values.ingress.certManager.enabled }}
{{- true }}
{{- end }}
{{- end }}

{{/*
Return the appropriate cert-manager annotation
*/}}
{{- define "waddlebot.certManager.annotation" -}}
{{- if eq .Values.ingress.certManager.issuer.kind "ClusterIssuer" }}
cert-manager.io/cluster-issuer: {{ .Values.ingress.certManager.issuer.name }}
{{- else }}
cert-manager.io/issuer: {{ .Values.ingress.certManager.issuer.name }}
{{- end }}
{{- end }}

{{/*
fix/chart-fresh-install-hooks (alpha 2026-10-01) -- waddlebot.dbMigrateInitContainer
(ran the migrations image as a per-pod initContainer on hub-api/svc-process-rust/
svc-action-rust, including the CWE-798 PR #256 INITIAL_ADMIN_EMAIL/PASSWORD seed step)
REMOVED. hub-api now bootstraps its own schema at startup (hub_api/bootstrap.py); every
other pod that needs the schema waits on hub-api's own `/ready` instead
(waddlebot.waitForHubApiInitContainer below) rather than each independently re-running
migrations against the same advisory lock. The migrations image itself still exists and
still runs the full migration directory + the admin-seed step -- now ONLY via
templates/migrations-job.yaml's pre-upgrade hook, never per-pod.

KNOWN GAP this removal surfaces (reported, not fixed here -- see PR description): the
INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD seed step lived inside the migrations image's
run path, which a fresh install no longer runs at all (hub-api's create_all()+stamp
bootstrap never executes config/postgres/migrations/081_seed_default_hub_admin.sql or any
other raw-SQL migration body). A fresh install therefore gets no seeded admin account
until the first `helm upgrade` actually fires the pre-upgrade migrate hook.
*/}}

{{/*
Wait-for-hub-api initContainer -- every non-hub-api, non-infrastructure pod in this chart
(Deployments and Jobs alike) waits on hub-api's own `/ready` endpoint before its main
container starts, since hub-api is now the one place that creates/validates the schema
(hub_api/bootstrap.py) and every other service depends on it being there first. Infra
(Postgres/Valkey/SeaweedFS) and hub-api itself are the only exceptions -- hub-api cannot
wait on itself, and infra has no schema dependency to wait for.

Requires N consecutive successful reads (not just one) before exiting 0, so a pod isn't
released the instant hub-api's readiness flips (which could still be mid-rollout/
restarting) -- see waitForHubApi.stableChecks/stableIntervalSeconds in values.yaml.
Exponential backoff (capped) between failed attempts, DEBUG-logged, so a slow hub-api
start doesn't spam logs at full speed. Pinned to the same python:3.13-slim-bookworm
digest hub-api's own Dockerfile already uses (Debian-only base per
devops-containers.md -- curlimages/curl and similar lightweight options are Alpine-based
and excluded on that basis) -- stdlib `urllib.request` only, no extra dependency.
Usage: {{- include "waddlebot.waitForHubApiInitContainer" . | nindent 6 }}
*/}}
{{- define "waddlebot.waitForHubApiInitContainer" -}}
{{- $url := printf "http://%s-hub-api-v3.%s.svc.cluster.local:%v/ready" (include "waddlebot.fullname" .) .Values.namespace .Values.pipeline.hubApi.port }}
- name: wait-for-hub-api
  image: "{{ .Values.waitForHubApi.image }}"
  imagePullPolicy: IfNotPresent
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  env:
  - name: HUB_API_READY_URL
    value: {{ $url | quote }}
  - name: STABLE_CHECKS
    value: {{ .Values.waitForHubApi.stableChecks | quote }}
  - name: STABLE_INTERVAL_SECONDS
    value: {{ .Values.waitForHubApi.stableIntervalSeconds | quote }}
  - name: MAX_BACKOFF_SECONDS
    value: {{ .Values.waitForHubApi.maxBackoffSeconds | quote }}
  command: ["python3", "-c"]
  args:
    - |
      import os, time, random, sys, urllib.request, urllib.error

      url = os.environ["HUB_API_READY_URL"]
      need = int(os.environ["STABLE_CHECKS"])
      interval = float(os.environ["STABLE_INTERVAL_SECONDS"])
      max_backoff = float(os.environ["MAX_BACKOFF_SECONDS"])

      def debug(msg):
          print(f"DEBUG wait-for-hub-api: {msg}", file=sys.stderr, flush=True)

      consecutive = 0
      backoff = 1.0
      attempt = 0
      while consecutive < need:
          attempt += 1
          try:
              with urllib.request.urlopen(url, timeout=3) as resp:
                  ok = resp.status == 200
          except (urllib.error.URLError, OSError) as exc:
              ok = False
              debug(f"attempt {attempt} failed: {exc}")
          if ok:
              consecutive += 1
              backoff = 1.0
              debug(f"attempt {attempt} ok ({consecutive}/{need} consecutive)")
              if consecutive < need:
                  time.sleep(interval)
          else:
              consecutive = 0
              sleep_for = min(backoff, max_backoff) * (0.8 + 0.4 * random.random())
              debug(f"attempt {attempt} not ready, backing off {sleep_for:.1f}s")
              time.sleep(sleep_for)
              backoff = min(backoff * 2, max_backoff)
      print(f"wait-for-hub-api: hub-api stable after {attempt} attempt(s)")
  resources:
    requests:
      cpu: "25m"
      memory: "32Mi"
    limits:
      cpu: "100m"
      memory: "64Mi"
{{- end }}

{{/*
Image pull secrets
*/}}
{{- define "waddlebot.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{/*
gRPC transport TLS (PR #264, A02 HIGH) -- shared helpers for the services/*
Deployments whose app.py now calls flask_core.grpc_tls.bind_secure_port /
secure_channel, which fail closed (GrpcTlsConfigError) if cert material
isn't wired. See templates/grpc-tls-secret.yaml / grpc-tls-certificate.yaml
for how the {{ fullname }}-grpc-tls Secret this mounts gets populated.
*/}}

{{/*
True only when real cert material will exist in the {{ fullname }}-grpc-tls
Secret at deploy time -- either cert-manager mints it (grpcTls.certManager.
enabled) or a full CA+server cert/key was supplied via values. False means
don't render the volume/env block at all, so grpc_tls.py's own fail-closed
GrpcTlsConfigError raises with its clear "GRPC_TLS_CERT_PATH ... required"
message instead of a mounted-but-empty cert file producing an opaque
low-level TLS parse error at the grpc layer.
*/}}
{{- define "waddlebot.grpcTlsMaterialAvailable" -}}
{{- if or .Values.global.grpcTls.certManager.enabled (and .Values.global.grpcTls.ca.crt .Values.global.grpcTls.tls.crt .Values.global.grpcTls.tls.key) -}}
true
{{- end -}}
{{- end }}

{{/*
GRPC_TLS_INSECURE_DEV passthrough -- flask_core's own explicit, dev-only
plaintext escape hatch (refused outright under production posture even if
set). Rendered unconditionally and defaults to "false"; only values-alpha.yaml
overrides global.grpcTls.insecureDev to true, as the documented interim so
alpha doesn't need real cert material.
*/}}
{{- define "waddlebot.grpcTlsInsecureDevEnv" -}}
- name: GRPC_TLS_INSECURE_DEV
  value: {{ .Values.global.grpcTls.insecureDev | default false | quote }}
{{- end }}

{{/*
Server-role env vars: this pod binds a gRPC server (bind_secure_port). Points at
tls.crt/tls.key -- one identity cert (usages: server auth + client auth) serves both
roles, matching cert-manager's fixed Secret key names natively; the static Secret path
(grpc-tls-secret.yaml) mirrors the same 3 keys for that reason.
*/}}
{{- define "waddlebot.grpcTlsServerEnv" -}}
- name: GRPC_TLS_CERT_PATH
  value: /etc/waddlebot/grpc-tls/tls.crt
- name: GRPC_TLS_KEY_PATH
  value: /etc/waddlebot/grpc-tls/tls.key
- name: GRPC_TLS_CA_PATH
  value: /etc/waddlebot/grpc-tls/ca.crt
- name: GRPC_TLS_REQUIRE_CLIENT_CERT
  value: {{ .Values.global.grpcTls.requireClientCert | quote }}
{{- end }}

{{/* Client-role env vars: this pod dials another module's gRPC server (secure_channel). */}}
{{- define "waddlebot.grpcTlsClientEnv" -}}
- name: GRPC_TLS_CA_PATH
  value: /etc/waddlebot/grpc-tls/ca.crt
- name: GRPC_TLS_CLIENT_CERT_PATH
  value: /etc/waddlebot/grpc-tls/tls.crt
- name: GRPC_TLS_CLIENT_KEY_PATH
  value: /etc/waddlebot/grpc-tls/tls.key
{{- end }}

{{/* Cert volume, sourced from the chart-managed {{ fullname }}-grpc-tls Secret. */}}
{{- define "waddlebot.grpcTlsVolume" -}}
- name: grpc-tls
  secret:
    secretName: {{ include "waddlebot.fullname" . }}-grpc-tls
    defaultMode: 0440
{{- end }}

{{- define "waddlebot.grpcTlsVolumeMount" -}}
- name: grpc-tls
  mountPath: /etc/waddlebot/grpc-tls
  readOnly: true
{{- end }}

{{/*
feature/e2e-helm-rust-dataplane -- host-API mTLS helpers (the bundle-executor<->stage wire
protocol, core/bundle_executor/src/tls.rs + core/svc_action/src/host_api.rs), a separate
identity from the gRPC helpers above -- different wire protocol, different port, mounted
from the {{ fullname }}-host-api-tls Secret (templates/host-api-tls-secret.yaml /
host-api-tls-certificate.yaml) rather than -grpc-tls. UNLIKE grpcTlsMaterialAvailable's
Python-side flask_core, the Rust code on both ends fails closed with no plaintext escape
hatch -- see global.hostApiTls's values.yaml comment.
*/}}

{{/*
fix/alpha-host-api-tls -- True only when real cert material will exist in the
{{ fullname }}-host-api-tls Secret at deploy time. Mirrors waddlebot.valkeyTlsMaterialAvailable's
gate-on-the-enabling-flag-not-raw-crt-material fix: templates/host-api-tls-secret.yaml now
guarantees the Secret exists (kept, explicitly supplied, cert-manager-owned, delegated to an
ExternalSecret, generated alpha/local, or the whole release fails to render) whenever
pipeline.rustDataPlane.enabled is true, so gating on .Values.global.hostApiTls.*.crt being
non-empty (the old check) wrongly stayed false on the generate path -- this is exactly what
left alpha's svc-process-rust/svc-action-rust host-api listeners permanently disabled even
though a Secret existed live (see host-api-tls-secret.yaml's KEEP branch). Gate on the
enabling flag alone; callers still get the Rust side's own clear config-error/graceful-
degradation behavior if this is somehow false while the Secret is genuinely missing.
*/}}
{{- define "waddlebot.hostApiTlsMaterialAvailable" -}}
{{- if .Values.pipeline.rustDataPlane.enabled -}}
true
{{- end -}}
{{- end }}

{{/* Server-role env vars: this pod binds the host-API mTLS listener (e.g. svc-action-rust). */}}
{{- define "waddlebot.hostApiTlsServerEnv" -}}
- name: HOST_API_SERVER_CERT_FILE
  value: /etc/waddlebot/host-api-tls/tls.crt
- name: HOST_API_SERVER_KEY_FILE
  value: /etc/waddlebot/host-api-tls/tls.key
- name: HOST_API_CLIENT_CA_FILE
  value: /etc/waddlebot/host-api-tls/ca.crt
{{- end }}

{{/* Client-role env vars: this pod dials a stage's host-API listener (bundle-executor). */}}
{{- define "waddlebot.hostApiTlsClientEnv" -}}
- name: HOST_API_CA_FILE
  value: /etc/waddlebot/host-api-tls/ca.crt
- name: HOST_API_CLIENT_CERT_FILE
  value: /etc/waddlebot/host-api-tls/tls.crt
- name: HOST_API_CLIENT_KEY_FILE
  value: /etc/waddlebot/host-api-tls/tls.key
# fix/chart-host-api-stage-identity -- core/bundle_executor/src/config.rs::
# validate_host_api_tls hard-requires this (Config error, immediate crashloop, if unset);
# core/bundle_executor/src/tls.rs's PinnedIdentityVerifier matches it against the stage
# cert's URI SAN / DNS SAN / Subject CN. Both bundle-executor.yaml and
# bundle-executor-action.yaml pull this helper, so they always get the SAME identity -- the
# shared host-api-tls Secret's one identity cert serves both stages (see that Secret's own
# "one identity cert ... serves both roles" comment), so one expected identity is correct
# for both executors regardless of which stage (svc-process-rust/svc-action-rust) they dial.
- name: HOST_API_STAGE_IDENTITY
  value: {{ include "waddlebot.hostApiStageIdentity" . | quote }}
{{- end }}

{{/*
fix/chart-host-api-stage-identity -- the single source of truth for the host-api-tls
identity cert's CN (and `HOST_API_STAGE_IDENTITY`), read together with this file's
waddlebot.hostApiTls* helpers above and templates/host-api-tls-secret.yaml's/
host-api-tls-certificate.yaml's own comments. Used as:
  - the CN argument to genSignedCert in templates/host-api-tls-secret.yaml (alpha/local
    auto-generate path)
  - the `commonName` field in templates/host-api-tls-certificate.yaml (cert-manager path)
  - HOST_API_STAGE_IDENTITY in waddlebot.hostApiTlsClientEnv above (bundle-executor and
    bundle-executor-action, the two consumers of that helper)
so whichever provisioning path is live, the cert's CN and the executor's expected identity
are byte-for-byte the same string and can never drift apart.

**Why CN, not a real SPIFFE URI SAN:** sprig's genSignedCert/genCA (the alpha/local path)
has no URI SAN support, so a true X.509-SVID URI SAN per security.md's
`spiffe://penguintech.io/<env>/<service>` base isn't achievable there. Rather than carry
two different identity *forms* across the two provisioning paths, both pin by Subject CN;
the CN value itself is still written in the org's SPIFFE-style naming convention for
operator readability, but PinnedIdentityVerifier matches it via its CN-exact-string
fallback, not its URI-SAN branch. If genSignedCert ever gains URI SAN support (or the
cert-manager path alone is extended with a `uris:` SAN), this is the one helper to change.

**Fail-closed:** global.hostApiTls.stageIdentity is an explicit override, REQUIRED when
neither chart-controlled provisioning path applies -- i.e. real material supplied directly
via global.hostApiTls.ca.crt/tls.crt/tls.key, or global.hostApiTls.externalSecret: true --
because in both cases an out-of-band CA mints the cert and this chart has no way to know
what identity is actually baked into it. Mirrors templates/host-api-tls-secret.yaml's own
`fail` for the same "chart doesn't control this material" cases.
*/}}
{{- define "waddlebot.hostApiStageIdentity" -}}
{{- $ht := .Values.global.hostApiTls -}}
{{- $tier := .Values.global.deploymentTier -}}
{{- if $ht.stageIdentity -}}
{{- $ht.stageIdentity -}}
{{- else if or $ht.certManager.enabled (or (eq $tier "alpha") (eq $tier "local")) -}}
{{- printf "spiffe://penguintech.io/%s/%s-host-api" $tier (include "waddlebot.fullname" .) -}}
{{- else -}}
{{- fail (printf "global.hostApiTls.stageIdentity is required when global.hostApiTls material is supplied directly (ca.crt/tls.crt/tls.key) or delegated to global.hostApiTls.externalSecret -- this chart does not mint that certificate, so it cannot derive HOST_API_STAGE_IDENTITY/the expected CN itself. Set global.hostApiTls.stageIdentity to the exact identity (SPIFFE URI SAN or Subject CN) that certificate actually carries.") -}}
{{- end -}}
{{- end }}

{{/* Cert volume, sourced from the chart-managed {{ fullname }}-host-api-tls Secret. */}}
{{- define "waddlebot.hostApiTlsVolume" -}}
- name: host-api-tls
  secret:
    secretName: {{ include "waddlebot.fullname" . }}-host-api-tls
    defaultMode: 0440
{{- end }}

{{- define "waddlebot.hostApiTlsVolumeMount" -}}
- name: host-api-tls
  mountPath: /etc/waddlebot/host-api-tls
  readOnly: true
{{- end }}

{{/*
fix/valkey-tls-alpha -- Valkey (infra-redis) TLS helpers. Server-auth-only TLS (no client
cert/key needed by any consumer, see global.valkeyTls's values.yaml comment): the server
(templates/infrastructure/redis.yaml) mounts the full Secret (ca.crt/tls.crt/tls.key), the
3 Rust data-plane client pods mount only the `valkey-ca.crt` key at
`/etc/waddles/ca/valkey-ca.crt` -- penguin_spine::SpineConfig's own default VALKEY_CA_FILE
path, so no VALKEY_CA_FILE env override is needed on the client side.
*/}}

{{/*
True only when real cert material will exist in the {{ fullname }}-valkey-tls Secret at
deploy time. Callers gate rendering the tls-port args/volumes on this so a missing Secret
falls back cleanly to Valkey's existing plaintext-only listener (dual-listener never even
attempted) instead of a mounted-but-empty file producing an opaque low-level TLS parse
error.
*/}}
{{/*
fix/helm-alpha-self-provisioning -- templates/infrastructure/valkey-tls-secret.yaml now
guarantees the <fullname>-valkey-tls Secret exists (kept, explicitly supplied, generated
alpha/local, or the whole release fails to render) whenever redis.tls.enabled is true --
the material itself is never stored back into .Values (generated inline via genCA/
genSignedCert), so gating on .Values.global.valkeyTls.*.crt being non-empty (the old
check) would wrongly stay false on the generate path. Gate on the enabled flag alone.
*/}}
{{- define "waddlebot.valkeyTlsMaterialAvailable" -}}
{{- if .Values.infrastructure.redis.tls.enabled -}}
true
{{- end -}}
{{- end }}

{{/*
fix/helm-alpha-self-provisioning -- lookup-then-generate-or-require for a single key
inside the monolithic waddlebot-secrets Secret (templates/secrets.yaml). Mirrors
templates/auto-provisioned-secrets.yaml's KEEP-vs-GENERATE policy but at per-key
granularity, since waddlebot-secrets also carries user-supplied pass-through values
(OAuth tokens, AWS/GCP creds, etc.) that must always reflect current .Values and are
therefore never routed through this helper.

Args (dict): ctx (the root "."), key (Secret data key name, e.g. "JWT_SECRET"),
explicit (the value already resolved from .Values, "" if unset/placeholder), length
(random byte-string length, default 32).

Precedence: explicit non-placeholder value from .Values wins outright (operator/
ExternalSecret already deliberately set it) > existing Secret key (KEEP, never
rotated) > fresh random generation (alpha/local only) > fail closed elsewhere.
*/}}
{{- define "waddlebot.autoSecretValue" -}}
{{- $ctx := .ctx -}}
{{- $key := .key -}}
{{- $explicit := .explicit | default "" -}}
{{- $length := .length | default 32 -}}
{{- $hex := .hex | default false -}}
{{- if ne $explicit "" -}}
{{- $explicit -}}
{{- else -}}
{{- $tier := $ctx.Values.global.deploymentTier -}}
{{- $canGenerate := or (eq $tier "alpha") (eq $tier "local") -}}
{{- $existing := lookup "v1" "Secret" $ctx.Values.namespace "waddlebot-secrets" -}}
{{- if (include "waddlebot.secretKeyNonEmpty" (dict "existing" $existing "key" $key)) -}}
{{- index $existing.data $key | b64dec -}}
{{- else if $canGenerate -}}
{{- if $hex -}}
{{- sha256sum (randBytes 32) -}}
{{- else -}}
{{- randAlphaNum (int $length) -}}
{{- end -}}
{{- else -}}
{{- fail (printf "waddlebot-secrets: key %q has no value and global.deploymentTier=%q is outside alpha/local -- auto-generation is alpha/local only. Set the corresponding value explicitly (or pre-populate this Secret via ExternalSecret/SealedSecret) before deploying." $key $tier) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Server-role volume: full cert+key+CA bundle, mounted by templates/infrastructure/redis.yaml. */}}
{{- define "waddlebot.valkeyTlsServerVolume" -}}
- name: valkey-tls
  secret:
    secretName: {{ include "waddlebot.fullname" . }}-valkey-tls
    defaultMode: 0440
{{- end }}

{{- define "waddlebot.valkeyTlsServerVolumeMount" -}}
- name: valkey-tls
  mountPath: /etc/valkey/tls
  readOnly: true
{{- end }}

{{/* Client-role volume: CA only, at penguin_spine's default VALKEY_CA_FILE path/filename. */}}
{{- define "waddlebot.valkeyTlsCaVolume" -}}
- name: valkey-ca
  secret:
    secretName: {{ include "waddlebot.fullname" . }}-valkey-tls
    defaultMode: 0440
    items:
    - key: valkey-ca.crt
      path: valkey-ca.crt
{{- end }}

{{- define "waddlebot.valkeyTlsCaVolumeMount" -}}
- name: valkey-ca
  mountPath: /etc/waddles/ca
  readOnly: true
{{- end }}

{{/*
fix/no-empty-kept-secrets -- shared "is this lookup-KEEP candidate actually usable"
predicates. Every auto-provisioned Secret in this chart (waddlebot-secrets per-key
fields via waddlebot.autoSecretValue above, auto-provisioned-secrets.yaml's symmetric
keys, host-api-tls-secret.yaml, infrastructure/valkey-tls-secret.yaml) follows the same
lookup(KEEP)-then-generate(alpha/local)-or-require(else) policy, and ALL of them had the
same latent bug: `lookup` finding an existing Secret/key was treated as sufficient to
KEEP, even when the stored value was the empty string (e.g. shipped by an earlier
`readerPassword | default ""` render). An empty kept value silently disables whatever it
gates (DB_READER_PASSWORD -> multi-app path off) forever, since KEEP always wins and
generation/fail-closed never fires again. These helpers make "empty" count as "missing"
everywhere a lookup result is consulted, so a pre-existing empty value is regenerated in
alpha/local and fails chart rendering (actionable message) in beta/gamma/production,
exactly like a Secret that never existed at all.

waddlebot.secretKeyNonEmpty -- single scalar key. Args (dict): existing (a `lookup "v1"
"Secret" ...` result, may be nil/empty outside a real cluster -- see
auto-provisioned-secrets.yaml's header comment on `lookup` under `helm template`), key
(data key name). Returns non-empty "true" only when the key is present AND decodes to a
non-empty string.
*/}}
{{- define "waddlebot.secretKeyNonEmpty" -}}
{{- $existing := .existing -}}
{{- $key := .key -}}
{{- if and $existing $existing.data (hasKey $existing.data $key) -}}
{{- if ne (index $existing.data $key | b64dec) "" -}}
true
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
waddlebot.tlsSecretComplete -- full ca.crt/tls.crt/tls.key bundle (host-api-tls-secret.yaml,
infrastructure/valkey-tls-secret.yaml). Args (dict): existing (the `lookup` result).
Returns non-empty "true" only when all three keys are present AND every one decodes to a
non-empty string -- a partially-populated or empty-valued bundle counts as MISSING, same
empty-is-missing rule as waddlebot.secretKeyNonEmpty.
*/}}
{{- define "waddlebot.tlsSecretComplete" -}}
{{- $existing := .existing -}}
{{- if and $existing $existing.data (hasKey $existing.data "ca.crt") (hasKey $existing.data "tls.crt") (hasKey $existing.data "tls.key") -}}
{{- if and (ne (index $existing.data "ca.crt" | b64dec) "") (ne (index $existing.data "tls.crt" | b64dec) "") (ne (index $existing.data "tls.key" | b64dec) "") -}}
true
{{- end -}}
{{- end -}}
{{- end -}}
