{{/*
feature/transit-tls-core -- in-cluster transit encryption for the Postgres (F1) and SeaweedFS S3 (F5) hops.

One chart-owned, ECDSA P-256 / SHA-256 internal CA ("transit CA") signs a server leaf for Postgres and one
for the SeaweedFS S3 gateway; both live, together with the CA certificate (never the CA key), in the single
Secret <fullname>-transit-tls (templates/transit-tls-secret.yaml). Clients mount ONLY ca.crt from it
(waddlebot.transitTls.caVolume) and verify the server against that CA -- never system roots, never
"insecure", never a plaintext fallback. The Valkey hops keep their own CA (templates/infrastructure/
valkey-tls-secret.yaml, global.valkeyTls) and are NOT part of this Secret.

F8 (CA prerequisites): this CA is chart-owned and carries NO nameConstraints, and every leaf SAN list includes
the <svc>.<ns>.svc.cluster.local FQDN. The cert-manager waddlebot-internal-ca ClusterIssuer
(k8s/dal2alpha/internal-ca.yaml) is deliberately NOT used for these hops: its critical nameConstraints permit only
the FQDN suffix plus 7 bare names, so a short-name dial (infra-postgres) can never validate against it. Clients
here dial the FQDN anyway (waddlebot.transitTls.pgHost / s3Host), so a future move to that CA needs no URL change.

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
{{- list $s (printf "%s.%s" $s $ns) (printf "%s.%s.svc" $s $ns) (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
{{- end }}

{{- define "waddlebot.transitTls.s3DnsNames" -}}
{{- $s := .Values.infrastructure.seaweedfs.service.name -}}
{{- $ns := .Values.namespace -}}
{{- list $s (printf "%s.%s" $s $ns) (printf "%s.%s.svc" $s $ns) (printf "%s.%s.svc.cluster.local" $s $ns) | toJson -}}
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
    secretName: {{ include "waddlebot.transitTls.secretName" . }}
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
