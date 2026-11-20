{{/*
PostHog (Community Edition, trimmed) helper templates.

Wires self-hosted PostHog into the waddlebot chart as the feature-flag
execution plane, disabled by default (posthog.enabled). All resources are
flat-named (posthog-web, posthog-postgres, ...) to match this chart's
existing infrastructure/*.yaml naming convention and deploy into the same
namespace/release as the rest of waddlebot.
*/}}

{{/* Common labels for PostHog-owned resources. */}}
{{- define "waddlebot.posthog.labels" -}}
app.kubernetes.io/part-of: posthog
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Name of the Secret holding PostHog's own SECRET_KEY / POSTGRES_PASSWORD / CLICKHOUSE_PASSWORD. */}}
{{- define "waddlebot.posthog.secretName" -}}
{{- if .Values.posthog.existingSecret -}}
{{ .Values.posthog.existingSecret }}
{{- else -}}
posthog-secrets
{{- end -}}
{{- end -}}

{{/* Fully digest-pinned PostHog app image ref (web + worker share one image). */}}
{{- define "waddlebot.posthog.image" -}}
{{ .Values.posthog.image.repository }}:{{ .Values.posthog.image.tag }}@{{ .Values.posthog.image.digest }}
{{- end -}}

{{/*
POSTHOG_HOST as exposed to app modules via the shared ConfigMap.
Precedence: explicit posthog.host value > in-cluster posthog-web Service
(when posthog.enabled) > external hosted default (license.penguintech.io).
This is what makes the toggle meaningful even when posthog.enabled=false —
app modules still get a valid POSTHOG_HOST pointed at the hosted plane.
*/}}
{{- define "waddlebot.posthog.effectiveHost" -}}
{{- if .Values.posthog.host -}}
{{ .Values.posthog.host }}
{{- else if .Values.posthog.enabled -}}
{{ printf "http://posthog-web:%v" .Values.posthog.web.port }}
{{- else -}}
{{ .Values.posthog.licenseServer.host | default "https://license.penguintech.io" }}
{{- end -}}
{{- end -}}

{{/*
Optional env var carrying the project API key (`phc_...`) for the license-server-hosted PostHog.
Renders nothing unless posthog.licenseServer.existingSecret is set. `optional: true` -- a missing
Secret/key leaves the var unset so flags fall back to FLAG_* env then default OFF; the pod still
starts. Explicit `env` beats `envFrom`, so this wins over any envFrom-provided value.
Call: include "waddlebot.posthog.licenseKeyEnv" (dict "root" $ "name" "POSTHOG_API_KEY")
*/}}
{{- define "waddlebot.posthog.licenseKeyEnv" -}}
{{- $ls := .root.Values.posthog.licenseServer -}}
{{- if $ls.existingSecret }}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ $ls.existingSecret | quote }}
      key: {{ $ls.secretKey | default "POSTHOG_API_KEY" | quote }}
      optional: true
{{- end }}
{{- end -}}

{{/* Shared PostHog app env (web + worker containers). */}}
{{- define "waddlebot.posthog.appEnv" -}}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.posthog.secretName" . }}
      key: SECRET_KEY
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.posthog.secretName" . }}
      key: POSTGRES_PASSWORD
- name: DATABASE_URL
  value: "postgres://{{ .Values.posthog.postgres.username }}:$(POSTGRES_PASSWORD)@posthog-postgres:5432/{{ .Values.posthog.postgres.database }}"
- name: REDIS_URL
  value: "redis://posthog-redis:6379/"
- name: CLICKHOUSE_HOST
  value: "posthog-clickhouse"
- name: CLICKHOUSE_DATABASE
  value: {{ .Values.posthog.clickhouse.database | quote }}
- name: CLICKHOUSE_USER
  value: {{ .Values.posthog.clickhouse.username | quote }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.posthog.secretName" . }}
      key: CLICKHOUSE_PASSWORD
      optional: true
- name: CLICKHOUSE_SECURE
  value: "false"
- name: CLICKHOUSE_VERIFY
  value: "false"
- name: KAFKA_HOSTS
  value: "posthog-kafka:9092"
- name: OBJECT_STORAGE_ENABLED
  value: "false"
- name: SITE_URL
  value: {{ include "waddlebot.posthog.effectiveHost" . | quote }}
- name: DISABLE_SECURE_SSL_REDIRECT
  value: "true"
- name: IS_BEHIND_PROXY
  value: "true"
- name: TRUST_ALL_PROXIES
  value: "true"
- name: DEPLOYMENT
  value: "k8s-helm-waddlebot"
{{- end -}}
