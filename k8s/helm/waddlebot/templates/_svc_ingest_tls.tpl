{{/* P6a -- svc-ingest's OWN host-api server Secret name (never the shared executor/stage host-api-tls). */}}
{{- define "waddlebot.svcIngestHostApiTlsSecretName" -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- ($ct.hostApiTls | default dict).secretName | default (printf "%s-svc-ingest-host-api-tls" (include "waddlebot.fullname" .)) -}}
{{- end }}

{{/* The ONLY client identity svc-ingest's listener accepts (the ingest-dedicated executor cert). */}}
{{- define "waddlebot.svcIngestHostApiClientIdentity" -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- ($ct.hostApiTls | default dict).clientIdentity | default (printf "%s-ingest-executor" (include "waddlebot.fullname" .)) -}}
{{- end }}
