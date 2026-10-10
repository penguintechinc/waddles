{{/*
waddlebot.sso.hubApiEnv -- the hub-api container env entries for enterprise SSO. Included
once from templates/hub-api.yaml (`nindent 8`, the same indentation as the surrounding
`env:` list). Emits nothing when `sso.enabled` is false.

SSO_ENCRYPTION_KEY is `optional: true` on purpose (see templates/sso.yaml): a missing key
must not stop the pod from starting for deployments that never use SSO; the SSO code paths
fail loudly instead. The shared Google client is NOT optional once `sso.google.existingSecret`
is set -- naming a Secret is a promise it exists.
*/}}
{{- define "waddlebot.sso.hubApiEnv" -}}
{{- if .Values.sso.enabled }}
- name: SSO_ENCRYPTION_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.sso.encryptionKey.secretName }}
      key: {{ .Values.sso.encryptionKey.secretKey }}
      optional: true
- name: SSO_STATE_TTL_SECONDS
  value: {{ .Values.sso.stateTtlSeconds | quote }}
- name: SSO_CLOCK_SKEW_SECONDS
  value: {{ .Values.sso.clockSkewSeconds | quote }}
{{- if .Values.sso.allowedPrivateHosts }}
- name: SSO_ALLOWED_PRIVATE_HOSTS
  value: {{ join "," .Values.sso.allowedPrivateHosts | quote }}
{{- end }}
{{- if .Values.sso.google.existingSecret }}
- name: SSO_GOOGLE_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.sso.google.existingSecret }}
      key: {{ .Values.sso.google.clientIdKey }}
- name: SSO_GOOGLE_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.sso.google.existingSecret }}
      key: {{ .Values.sso.google.clientSecretKey }}
{{- end }}
{{- end }}
{{- end -}}
