{{/*
In-cluster HTTP transit TLS (global.internalTransitTls; audit F3/F4/F6,
docs/superpowers/specs/2026-11-08-pod-transit-encryption-audit.md).
*/}}

{{/* Renders "true" (non-empty) when enabled so callers can `{{- if include ... }}`. */}}
{{- define "waddlebot.transitTlsEnabled" -}}
{{- if and .Values.global.internalTransitTls .Values.global.internalTransitTls.enabled -}}true{{- end -}}
{{- end -}}

{{/*
Service FQDN `<fullname>-<name>.<ns>.svc.cluster.local`. The internal CA's X.509 nameConstraints
permit ONLY `*.<ns>.svc.cluster.local` FQDNs (k8s/dal2alpha/internal-ca.yaml), so every TLS URL
and every cert SAN uses this form -- never the bare Service name.
Usage: include "waddlebot.svcFqdn" (dict "root" . "name" "hub-api-v3")
*/}}
{{- define "waddlebot.svcFqdn" -}}
{{- printf "%s-%s.%s.svc.cluster.local" (include "waddlebot.fullname" .root) .name .root.Values.namespace -}}
{{- end -}}

{{/* Base URL of hub-api's HTTP listener: https://<FQDN>:<port> when transit TLS is on, else legacy http://. */}}
{{- define "waddlebot.hubApiBaseUrl" -}}
{{- if include "waddlebot.transitTlsEnabled" . -}}
{{- printf "https://%s:%v" (include "waddlebot.svcFqdn" (dict "root" . "name" "hub-api-v3")) .Values.pipeline.hubApi.port -}}
{{- else -}}
{{- printf "http://%s-hub-api-v3:%v" (include "waddlebot.fullname" .) .Values.pipeline.hubApi.port -}}
{{- end -}}
{{- end -}}

{{/* URL scheme for probes/URLs of components that terminate TLS: HTTPS or HTTP. */}}
{{- define "waddlebot.transitProbeScheme" -}}
{{- if include "waddlebot.transitTlsEnabled" . -}}HTTPS{{- else -}}HTTP{{- end -}}
{{- end -}}

{{/* Name of the cert-manager-issued server Secret for a component. Usage: dict "root" . "component" "hub-api" */}}
{{- define "waddlebot.transitTlsSecretName" -}}
{{- printf "%s-%s-http-tls" (include "waddlebot.fullname" .root) .component -}}
{{- end -}}

{{/*
Client-side env: pin trust to the internal CA and refuse plaintext. The (public) CA PEM comes
straight from the hub-api server Secret's ca.crt key via secretKeyRef -- no volume needed, so any
pod (including init containers) opts in with one include. Empty when disabled.
Usage: {{- include "waddlebot.transitCaEnv" . | nindent 8 }}
*/}}
{{- define "waddlebot.transitCaEnv" -}}
{{- if include "waddlebot.transitTlsEnabled" . }}
- name: INTERNAL_CA_PEM
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.transitTlsSecretName" (dict "root" . "component" "hub-api") }}
      key: ca.crt
- name: INTERNAL_TLS_REQUIRED
  value: "true"
{{- end }}
{{- end -}}

{{/* Server-side TLS env for a component that terminates TLS itself (cert/key paths). */}}
{{- define "waddlebot.transitServerEnv" -}}
- name: TLS_CERT_FILE
  value: /etc/waddlebot/tls/tls.crt
- name: TLS_KEY_FILE
  value: /etc/waddlebot/tls/tls.key
{{- end -}}

{{- define "waddlebot.transitServerVolumeMount" -}}
- name: http-tls
  mountPath: /etc/waddlebot/tls
  readOnly: true
{{- end -}}

{{/* Usage: include "waddlebot.transitServerVolume" (dict "root" . "component" "hub-api") */}}
{{- define "waddlebot.transitServerVolume" -}}
- name: http-tls
  secret:
    secretName: {{ include "waddlebot.transitTlsSecretName" . }}
    items:
    - key: tls.crt
      path: tls.crt
    - key: tls.key
      path: tls.key
{{- end -}}
