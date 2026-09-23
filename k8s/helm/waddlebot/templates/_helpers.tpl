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
DB Migration initContainer
Runs database migrations before the application container starts.
Uses advisory locking to handle concurrent pod startup safely.

Also carries INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD (PR #256,
CWE-798): this is the chart's one shared init path, mirroring
docker-compose.yml's db-migrations service, which is the actual live
first-run super-admin bootstrap trigger (config/postgres/migrations/
081_seed_default_hub_admin.sql via run-migrations.sh) --
admin/hub_module/backend's own adminBootstrap.js is a forward-compatible
fallback for if SKIP_DB_INIT is ever unset, not the primary path. Both
keys default to "" in templates/secrets.yaml; empty/unset means no admin
account is created (fail closed) -- exactly the desired default for
beta/gamma/production until an operator sets them.
Usage: {{- include "waddlebot.dbMigrateInitContainer" . | nindent 6 }}
*/}}
{{- define "waddlebot.dbMigrateInitContainer" -}}
{{- $registry := .Values.global.imageRegistry | default "" }}
{{- $repository := .Values.modules.migrations.image | default "waddlebot-migrations" }}
{{- $tag := .Values.global.imageTag }}
- name: db-migrate
  {{- if $registry }}
  image: "{{ $registry }}/{{ $repository }}:{{ $tag }}"
  {{- else }}
  image: "{{ $repository }}:{{ $tag }}"
  {{- end }}
  imagePullPolicy: {{ .Values.global.imagePullPolicy }}
  env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ include "waddlebot.fullname" . }}-secrets
        key: DATABASE_URL
  - name: INITIAL_ADMIN_EMAIL
    valueFrom:
      secretKeyRef:
        name: {{ include "waddlebot.fullname" . }}-secrets
        key: INITIAL_ADMIN_EMAIL
        optional: true
  - name: INITIAL_ADMIN_PASSWORD
    valueFrom:
      secretKeyRef:
        name: {{ include "waddlebot.fullname" . }}-secrets
        key: INITIAL_ADMIN_PASSWORD
        optional: true
  resources:
    requests:
      cpu: "50m"
      memory: "64Mi"
    limits:
      cpu: "200m"
      memory: "128Mi"
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
True only when real cert material will exist in the {{ fullname }}-host-api-tls Secret at
deploy time -- either cert-manager mints it or a full CA+cert/key was supplied via values.
Callers gate rendering the volume/env blocks on this so a missing Secret produces the
Rust side's own clear "HOST_API_SERVER_CERT_FILE and HOST_API_SERVER_KEY_FILE must both be
set" config error at startup (host-api listener disabled, rest of the pod keeps serving --
see host_api.rs's graceful-degradation comment) instead of a mounted-but-empty file
producing an opaque low-level TLS parse error.
*/}}
{{- define "waddlebot.hostApiTlsMaterialAvailable" -}}
{{- if or .Values.global.hostApiTls.certManager.enabled (and .Values.global.hostApiTls.ca.crt .Values.global.hostApiTls.tls.crt .Values.global.hostApiTls.tls.key) -}}
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
