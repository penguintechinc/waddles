{{/*
Reusable snippets for the per-service EdDSA machine JWT mechanism
(feature/eddsa-machine-jwt, values.yaml `serviceJwt`).

"waddlebot.serviceJwtBootstrapVolume" -- the projected ServiceAccount
token volume a calling service (svc-process, svc-action, ...) mounts to
bootstrap against hub-api's /internal/service-token endpoint. Include in
any Deployment's `.spec.template.spec.volumes`:

  {{- include "waddlebot.serviceJwtBootstrapVolume" . | nindent 8 }}

"waddlebot.serviceJwtBootstrapVolumeMount" -- the matching container
volumeMount, include in `.spec.template.spec.containers[].volumeMounts`:

  {{- include "waddlebot.serviceJwtBootstrapVolumeMount" . | nindent 12 }}

Not auto-injected into every Deployment template -- migrating the
existing ~47 services is tracked separately (see the v3.1.x GitHub issue
filed alongside this PR); these two helpers exist so that migration is
"include two snippets", not "write the volume spec 47 times".
*/}}

{{- define "waddlebot.serviceJwtBootstrapVolume" -}}
- name: service-jwt-bootstrap-token
  projected:
    sources:
      - serviceAccountToken:
          audience: {{ .Values.serviceJwt.bootstrapAudience | quote }}
          expirationSeconds: {{ .Values.serviceJwt.bootstrapTokenProjection.expirationSeconds }}
          path: {{ .Values.serviceJwt.bootstrapTokenProjection.path }}
{{- end -}}

{{- define "waddlebot.serviceJwtBootstrapVolumeMount" -}}
- name: service-jwt-bootstrap-token
  mountPath: /var/run/secrets/waddlebot/service-jwt
  readOnly: true
{{- end -}}
