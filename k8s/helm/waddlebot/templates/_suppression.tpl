{{/*
waddlebot.suppression.hubApiEnv -- the hub-api container env entries for ban-evasion
suppression (hub_api/services/suppression_keys.py, ban_suppression.py). Included once from
templates/hub-api.yaml (`nindent 8`, the same indentation as the surrounding `env:` list).

TENANT_KEK_HEX is the platform tenant KEK Secret (`autoProvisionedKeys.tenantKek`) -- the same
Secret the per-tenant envelope / external-KMS work wraps tenant DEKs with. It is K_master for the
per-tenant HKDF key behind every `suppression_records` code, so it is mounted into hub-api ONLY
(the PII boundary): no other workload gets it, which is what makes the stored codes useless to
anyone holding a database dump. `optional: true` on purpose, like the SSO key: a deployment that
never erases a banned user must still start; the code paths that need the key fail loudly
(`SuppressionKeyError`) instead of the pod failing. `env` wins over `envFrom`, so this coexists
with the KMS chart's `envFrom` of the same Secret.
*/}}
{{- define "waddlebot.suppression.hubApiEnv" -}}
- name: TENANT_KEK_HEX
  valueFrom:
    secretKeyRef:
      name: {{ .Values.autoProvisionedKeys.tenantKek.secretName }}
      key: {{ .Values.autoProvisionedKeys.tenantKek.secretKey }}
      optional: true
{{- if .Values.suppression.maxRetentionDays }}
- name: WADDLES_SUPPRESSION_MAX_RETENTION_DAYS
  value: {{ .Values.suppression.maxRetentionDays | quote }}
{{- end }}
{{- end -}}
