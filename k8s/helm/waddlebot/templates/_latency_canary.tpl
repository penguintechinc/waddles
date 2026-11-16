{{/*
Command-latency canary ConfigMap entries (alpha only). Empty unless `latencyCanary.enabled`.
When enabled every id must be filled (fail-loud at render, never a silent no-op) and the release
environment must be `alpha`. Snowflakes MUST be quoted strings in values (YAML parses a bare
19-digit number as a lossy float). Nothing here is a secret.
*/}}
{{- define "waddlebot.latencyCanary.env" -}}
{{- $c := .Values.latencyCanary | default dict -}}
{{- if $c.enabled -}}
{{- $env := .Values.global.deploymentTier | default "production" -}}
{{- if ne $env "alpha" -}}
{{- fail (printf "latencyCanary.enabled is alpha-only (global.deploymentTier=%s)" $env) -}}
{{- end -}}
{{- range $k := list "tenantId" "communityId" -}}
{{- $v := index $c $k -}}
{{- if not (and $v (regexMatch "^[1-9][0-9]*$" (toString $v))) -}}
{{- fail (printf "latencyCanary.%s must be a positive integer id (owner fills it; see docs/observability/command-latency.md)" $k) -}}
{{- end -}}
{{- end -}}
{{- range $k := list "guildId" "channelId" -}}
{{- $v := index $c $k -}}
{{- if or (not (kindIs "string" $v)) (not (regexMatch "^[0-9]{1,20}$" $v)) -}}
{{- fail (printf "latencyCanary.%s must be a QUOTED numeric Discord snowflake string (owner fills it; see docs/observability/command-latency.md)" $k) -}}
{{- end -}}
{{- end -}}
{{- $mode := $c.sinkMode | default "post" -}}
{{- if not (has $mode (list "post" "swallow")) -}}
{{- fail "latencyCanary.sinkMode must be post or swallow" -}}
{{- end }}
  FLAG_WADDLES_COMMAND_LATENCY_CANARY: "true"
  CANARY_ENVIRONMENT: "alpha"
  CANARY_TENANT_ID: {{ $c.tenantId | toString | quote }}
  CANARY_COMMUNITY_ID: {{ $c.communityId | toString | quote }}
  CANARY_GUILD_ID: {{ $c.guildId | quote }}
  CANARY_CHANNEL_ID: {{ $c.channelId | quote }}
  CANARY_COMMAND_TEXT: {{ $c.commandText | default "!ping" | quote }}
  CANARY_SINK_MODE: {{ $mode | quote }}
  CANARY_INTERVAL_SECS: {{ $c.intervalSeconds | default 60 | toString | quote }}
{{- end -}}
{{- end -}}
