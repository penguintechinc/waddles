{{/*
feature/transit-tls-core -- in-cluster transit encryption for the Postgres (F1) and SeaweedFS S3 (F5) hops.

Self-generated fallback (cert-manager off): one chart-owned ECDSA P-256 / SHA-256 CA ("transit CA") signs a server leaf for
Postgres and one for the SeaweedFS S3 gateway; both live, with the CA certificate (never the CA key), in the single Secret
<fullname>-transit-tls (templates/transit-tls-secret.yaml). Clients mount ONLY ca.crt and verify the server against that CA --
never system roots, never "insecure", never a plaintext fallback.

ONE TRUST ROOT: with global.transitTls.certManager.enabled (dal2alpha/beta/gamma/production) there is no self-generated CA at
all -- templates/transit-data-tls-certificates.yaml asks the shared `waddlebot-internal-ca` ClusterIssuer for the Postgres,
SeaweedFS and Valkey server certs (FQDN-only SANs), and every client mounts the ca.crt cert-manager writes next to them
(waddlebot.transitTls.caSecretName: the SAME CA bytes for all three hops and the existing host-API/gRPC/hub-api trust). Only
where cert-manager is off (local, values-alpha) does the self-generated CA below apply; the legacy Valkey CA is then separate.

F8 (CA prerequisites): the cert-manager `waddlebot-internal-ca` CA (k8s/dal2alpha/internal-ca.yaml) has critical nameConstraints
permitting only `*.<ns>.svc.cluster.local` plus a few bare names, so in cert-manager mode every SAN is the FQDN only and every
client dials the FQDN (waddlebot.transitTls.pgHost / s3Host / fqdn); no CA re-issue is needed. The self-generated fallback CA has
no nameConstraints and also carries the short forms. Both are ECDSA P-256 / ecdsa-with-SHA256.

Negotiation bar (user requirement 2026-11-08): TLS 1.3 preferred, 1.2 minimum, ECDSA P-256, P-256 key exchange,
AES-128-GCM / ChaCha20-Poly1305, session resumption on, pooled keep-alive client connections.
*/}}

{{/* True when the chart renders a workload that terminates or consumes transit TLS (Postgres or SeaweedFS). */}}
{{- define "waddlebot.transitTls.enabled" -}}
{{- if or .Values.infrastructure.postgresql.enabled .Values.infrastructure.seaweedfs.enabled -}}
true
{{- end -}}
{{- end }}

{{- define "waddlebot.transitTls.secretName" -}}
{{- printf "%s-transit-tls" (include "waddlebot.fullname" .) -}}
{{- end }}

{{/* "true" when the shared cert-manager issuer provides every transit cert (no self-generated CA). */}}
{{- define "waddlebot.transitTls.cm" -}}
{{- if .Values.global.transitTls.certManager.enabled -}}
true
{{- end -}}
{{- end }}

{{/* Shared issuer (name/kind): global.internalTransitTls.certManager.* (hub-api HTTPS) wins when present. */}}
{{- define "waddlebot.transitTls.issuerName" -}}
{{- $it := get .Values.global "internalTransitTls" | default dict -}}
{{- $icm := get $it "certManager" | default dict -}}
{{- get $icm "issuerName" | default .Values.global.transitTls.certManager.issuerName -}}
{{- end }}
{{- define "waddlebot.transitTls.issuerKind" -}}
{{- $it := get .Values.global "internalTransitTls" | default dict -}}
{{- $icm := get $it "certManager" | default dict -}}
{{- get $icm "issuerKind" | default .Values.global.transitTls.certManager.issuerKind -}}
{{- end }}

{{/* cert-manager-issued server Secret names (one per server; each also carries the shared ca.crt). */}}
{{- define "waddlebot.transitTls.pgSecretName" -}}
{{- if include "waddlebot.transitTls.cm" . -}}{{ include "waddlebot.fullname" . }}-postgres-tls{{- else -}}{{ include "waddlebot.transitTls.secretName" . }}{{- end -}}
{{- end }}
{{- define "waddlebot.transitTls.s3SecretName" -}}
{{- if include "waddlebot.transitTls.cm" . -}}{{ include "waddlebot.fullname" . }}-seaweedfs-tls{{- else -}}{{ include "waddlebot.transitTls.secretName" . }}{{- end -}}
{{- end }}

{{/* Secret every CLIENT mounts ca.crt from: the one shared trust root. */}}
{{- define "waddlebot.transitTls.caSecretName" -}}
{{- if include "waddlebot.transitTls.cm" . -}}
{{- if .Values.infrastructure.postgresql.enabled -}}{{ include "waddlebot.transitTls.pgSecretName" . }}{{- else -}}{{ include "waddlebot.transitTls.s3SecretName" . }}{{- end -}}
{{- else -}}{{ include "waddlebot.transitTls.secretName" . }}{{- end -}}
{{- end }}

{{/* Server volume `items` (key->path) for Postgres/SeaweedFS: the cert-manager Secret uses tls.crt/tls.key, the static one prefixed keys. */}}
{{- define "waddlebot.transitTls.pgItems" -}}
{{- if include "waddlebot.transitTls.cm" . -}}
- key: tls.crt
  path: tls.crt
- key: tls.key
  path: tls.key
{{- else -}}
- key: postgres.crt
  path: tls.crt
- key: postgres.key
  path: tls.key
{{- end -}}
{{- end }}
{{- define "waddlebot.transitTls.s3Items" -}}
- key: ca.crt
  path: ca.crt
{{ if include "waddlebot.transitTls.cm" . -}}
- key: tls.crt
  path: tls.crt
- key: tls.key
  path: tls.key
{{- else -}}
- key: seaweedfs.crt
  path: tls.crt
- key: seaweedfs.key
  path: tls.key
{{- end }}
{{- end }}

{{/* FQDN of any in-chart Service: args (dict "root" . "name" <svc>) -- the form every transit URL uses. */}}
{{- define "waddlebot.transitTls.fqdn" -}}
{{- printf "%s.%s.svc.cluster.local" .name .root.Values.namespace -}}
{{- end }}

{{/* Path the CA is mounted at inside every client container. */}}
{{- define "waddlebot.transitTls.caPath" -}}
/etc/waddles/transit-ca/ca.crt
{{- end }}

{{/* FQDN clients dial for Postgres -- always in the SAN list, valid under the CA nameConstraints suffix too. */}}
{{- define "waddlebot.transitTls.pgHost" -}}
{{- printf "%s.%s.svc.cluster.local" .Values.infrastructure.postgresql.service.name .Values.namespace -}}
{{- end }}

{{/* FQDN clients dial for the SeaweedFS S3 gateway. */}}
{{- define "waddlebot.transitTls.s3Host" -}}
{{- printf "%s.%s.svc.cluster.local" .Values.infrastructure.seaweedfs.service.name .Values.namespace -}}
{{- end }}

{{/* https S3 endpoint (no trailing slash) every in-chart S3 client uses. */}}
{{- define "waddlebot.transitTls.s3Endpoint" -}}
{{- printf "https://%s:%v" (include "waddlebot.transitTls.s3Host" .) .Values.infrastructure.seaweedfs.service.s3Port -}}
{{- end }}

{{/* SAN lists as JSON arrays (single source for the Secret and its identity hash). */}}
{{- define "waddlebot.transitTls.pgDnsNames" -}}
{{- $s := .Values.infrastructure.postgresql.service.name -}}
{{- $ns := .Values.namespace -}}
{{- if include "waddlebot.transitTls.cm" . -}}
{{- list (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
{{- else -}}
{{- list $s (printf "%s.%s" $s $ns) (printf "%s.%s.svc" $s $ns) (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
{{- end -}}
{{- end }}

{{- define "waddlebot.transitTls.s3DnsNames" -}}
{{- $s := .Values.infrastructure.seaweedfs.service.name -}}
{{- $ns := .Values.namespace -}}
{{- if include "waddlebot.transitTls.cm" . -}}
{{- list (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
{{- else -}}
{{- list $s (printf "%s.%s" $s $ns) (printf "%s.%s.svc" $s $ns) (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
{{- end -}}
{{- end }}

{{/* Valkey (cache + durable + kv) server SANs for cert-manager: only names inside the issuer's nameConstraints suffix. */}}
{{- define "waddlebot.transitTls.valkeyCmDnsNames" -}}
{{- $out := list -}}
{{- range (include "waddlebot.valkeyTlsDesiredDnsNames" . | splitList ",") -}}
{{- if hasSuffix ".svc.cluster.local" . -}}{{- $out = append $out . -}}{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end }}

{{/* Identity hash over the SANs + algorithm marker: a changed Service name/namespace regenerates (alpha) or fails closed. */}}
{{- define "waddlebot.transitTls.sansSha256" -}}
{{- printf "ecdsa-p256|%s|%s" (include "waddlebot.transitTls.pgDnsNames" .) (include "waddlebot.transitTls.s3DnsNames" .) | sha256sum -}}
{{- end }}

{{/* Pod-template annotation so server pods roll when the SAN identity changes. */}}
{{- define "waddlebot.transitTls.podChecksum" -}}
{{- include "waddlebot.transitTls.sansSha256" . -}}
{{- end }}

{{/* Client volume: ca.crt ONLY (no server key ever reaches a client pod). */}}
{{- define "waddlebot.transitTls.caVolume" -}}
{{- if include "waddlebot.transitTls.enabled" . -}}
- name: transit-ca
  secret:
    secretName: {{ include "waddlebot.transitTls.caSecretName" . }}
    defaultMode: 0444
    items:
    - key: ca.crt
      path: ca.crt
{{- end -}}
{{- end }}

{{- define "waddlebot.transitTls.caVolumeMount" -}}
{{- if include "waddlebot.transitTls.enabled" . -}}
- name: transit-ca
  mountPath: /etc/waddles/transit-ca
  readOnly: true
{{- end -}}
{{- end }}

{{/*
libpq-level Postgres client TLS: PGSSLMODE=verify-full + PGSSLROOTCERT (the CA above). Honoured by psycopg2, asyncpg
(SQLAlchemy asyncpg dialect rejects a ?sslmode= query param, so the env is the one mechanism that reaches every Python
DB stack) and sqlx (core/* Rust crates build their URL in code; PgConnectOptions reads these vars). verify-full never
falls back to plaintext: a failed handshake or hostname mismatch is a connection error.
*/}}
{{- define "waddlebot.transitTls.pgClientEnv" -}}
{{- if .Values.infrastructure.postgresql.enabled -}}
- name: PGSSLMODE
  value: "verify-full"
- name: PGSSLROOTCERT
  value: {{ include "waddlebot.transitTls.caPath" . | quote }}
{{- end -}}
{{- end }}

{{/* Postgres + S3 client TLS env together (what every DB/S3-consuming workload includes after waddlebot.dbEnv). */}}
{{- define "waddlebot.transitTls.clientEnv" -}}
{{- with (include "waddlebot.transitTls.pgClientEnv" .) }}{{ . }}
{{ end -}}
{{- include "waddlebot.transitTls.s3ClientEnv" . -}}
{{- end }}

{{/* Secret metadata head shared by every provisioning branch of transit-tls-secret.yaml. Args: root, name, sans. */}}
{{- define "waddlebot.transitTls.secretHead" -}}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .name }}
  namespace: {{ .root.Values.namespace }}
  labels:
    {{- include "waddlebot.labels" .root | nindent 4 }}
    app.kubernetes.io/component: transit-tls
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-30"
    "helm.sh/hook-delete-policy": before-hook-creation
    waddlebot.io/cert-sans-sha256: {{ .sans | quote }}
type: Opaque
{{- end }}

{{/* S3 client TLS env: boto3/aws-cli (AWS_CA_BUNDLE) and the Rust object_store clients (S3_CA_FILE). */}}
{{- define "waddlebot.transitTls.s3ClientEnv" -}}
{{- if .Values.infrastructure.seaweedfs.enabled -}}
- name: S3_CA_FILE
  value: {{ include "waddlebot.transitTls.caPath" . | quote }}
- name: AWS_CA_BUNDLE
  value: {{ include "waddlebot.transitTls.caPath" . | quote }}
{{- end -}}
{{- end }}
