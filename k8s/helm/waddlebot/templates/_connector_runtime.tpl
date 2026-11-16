{{/*
PR-H -- connector runtime (inbound manager) helpers. Spec:
docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md SS1.3/SS2/SS5/SS7/SS8.
Env names are NORMATIVE (spec SS8). Everything here is inert while every connector and
svcIngest.connectorRuntime.enabled are false: no helper emits output, so an all-OFF render is
byte-identical to a render without this feature.
*/}}

{{/* "true" when connector <platform> is ON (dict "root" . "platform" "twitch"); empty otherwise. */}}
{{- define "waddlebot.connectorPlatformOn" -}}
{{- $cr := .root.Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict -}}
{{- $ov := $cr.connector | default dict -}}
{{- $p := index $ov .platform | default dict -}}
{{- if $p.enabled -}}true{{- end -}}
{{- end }}

{{/* "true" when the runtime is active: master switch OR any platform connector ON. */}}
{{- define "waddlebot.connectorRuntimeActive" -}}
{{- $cr := .Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict -}}
{{- if or $cr.enabled (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "twitch")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "discord")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "youtube")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "wix")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "spectrum")) -}}true{{- end -}}
{{- end }}

{{/* Single source of truth for connector digests (dict "root" . "platform" "twitch"). */}}
{{- define "waddlebot.connectorDigest" -}}
{{- $d := (.root.Values.pipeline.connectors | default dict).digests | default dict -}}
{{- index $d .platform | default "" -}}
{{- end }}

{{/* Name of the DISTINCT executor client-cert Secret (ca.crt/tls.crt/tls.key; SAN = executor identity only). */}}
{{- define "waddlebot.ingestExecutorTlsSecretName" -}}
{{- printf "%s-ingest-executor-tls" (include "waddlebot.fullname" .) -}}
{{- end }}

{{/* svc-ingest Service DNS name the executor dials (host-api port). */}}
{{- define "waddlebot.svcIngestHostApiServiceName" -}}
{{- printf "%s-svc-ingest-rust" (include "waddlebot.fullname" .) -}}
{{- end }}

{{/* Server SAN the executor pins on its dial (matches the generated svc-ingest server cert). */}}
{{- define "waddlebot.svcIngestHostApiServerIdentity" -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- ($ct.hostApiTls | default dict).serverIdentity | default (include "waddlebot.svcIngestHostApiServiceName" .) -}}
{{- end }}

{{/*
CONNECTOR_<P>_ENABLED + CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS env list items. Included by BOTH the
Rust and the Python svc-ingest deployments so the two read ONE helm value and cannot disagree.
Emits nothing while the runtime is inactive.
*/}}
{{- define "waddlebot.connectorPlatformEnv" -}}
{{- if include "waddlebot.connectorRuntimeActive" . -}}
{{- $cr := .Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict }}
- name: CONNECTOR_TWITCH_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "twitch")) "true") | quote }}
- name: CONNECTOR_DISCORD_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "discord")) "true") | quote }}
- name: CONNECTOR_YOUTUBE_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "youtube")) "true") | quote }}
- name: CONNECTOR_WIX_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "wix")) "true") | quote }}
- name: CONNECTOR_SPECTRUM_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "spectrum")) "true") | quote }}
- name: CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS
  value: {{ join "," ($cr.webhookPlatforms | default list) | quote }}
{{- end -}}
{{- end }}

{{/* Render-time validation (helm `fail`); emits nothing. Called from connector-runtime-validate.yaml. */}}
{{- define "waddlebot.connectorRuntimeValidate" -}}
{{- $rdp := .Values.pipeline.rustDataPlane -}}
{{- $si := $rdp.svcIngest -}}
{{- $cr := $si.connectorRuntime | default dict -}}
{{- $ct := $si.connectorTools | default dict -}}
{{- range $p := list "twitch" "discord" "youtube" "wix" -}}
{{- if index ($ct.digests | default dict) $p -}}
{{- fail (printf "svcIngest.connectorTools.digests.%s is removed -- set pipeline.connectors.digests.%s (single source of truth for SENDER_EXEC_DIGEST_%s and CONNECTOR_PIN_DIGEST_%s)" $p $p ($p | upper) ($p | upper)) -}}
{{- end -}}
{{- end -}}
{{- if include "waddlebot.connectorRuntimeActive" . -}}
{{- if and (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "discord")) (not $cr.identifyBudgetReady) -}}
{{- fail "discord connector requires the identify budget (not yet ported)" -}}
{{- end -}}
{{- if not (and $rdp.enabled $si.enabled) -}}
{{- fail "svcIngest.connectorRuntime requires pipeline.rustDataPlane.enabled and pipeline.rustDataPlane.svcIngest.enabled (the runtime lives in the Rust svc-ingest)" -}}
{{- end -}}
{{- if not $ct.enabled -}}
{{- fail "svcIngest.connectorRuntime (any connector ON) requires svcIngest.connectorTools.enabled (host-api listener, TLS secrets, hub-api resolver)" -}}
{{- end -}}
{{- range $p := list "twitch" "discord" "youtube" "wix" "spectrum" -}}
{{- if include "waddlebot.connectorPlatformOn" (dict "root" $ "platform" $p) -}}
{{- $d := include "waddlebot.connectorDigest" (dict "root" $ "platform" $p) -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $d) -}}
{{- fail (printf "svcIngest.connectorRuntime.connector.%s.enabled requires pipeline.connectors.digests.%s = sha256:<64 lowercase hex> (got %q)" $p $p $d) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $wp := ($cr.webhookPlatforms | default list) -}}
{{- if not (has $wp (list "twitch" "discord" "wix")) -}}
{{- fail (printf "svcIngest.connectorRuntime.webhookPlatforms: %q is not one of twitch, discord, wix" $wp) -}}
{{- end -}}
{{- end -}}
{{- if ($cr.webhookPlatforms | default list) -}}
{{- $rk := $cr.routeKeySecret | default dict -}}
{{- if not (and $rk.existingSecret $rk.key) -}}
{{- fail "svcIngest.connectorRuntime.webhookPlatforms non-empty requires routeKeySecret.existingSecret and routeKeySecret.key (CONNECTOR_ROUTE_KEY_SECRET is only ever read from an existing Secret)" -}}
{{- end -}}
{{- end -}}
{{- range $k := list "reconcileIntervalSecs" "maxConnections" "startConcurrency" "discordStartPaceMs" -}}
{{- if lt (int (index $cr $k)) 1 -}}
{{- fail (printf "svcIngest.connectorRuntime.%s must be a positive integer" $k) -}}
{{- end -}}
{{- end -}}
{{- $cid := include "waddlebot.svcIngestHostApiClientIdentity" . -}}
{{- if or (eq $cid (include "waddlebot.hostApiStageIdentity" .)) (has $cid (splitList "," (include "waddlebot.hostApiTlsDesiredDnsNames" .))) -}}
{{- fail (printf "svcIngest.connectorTools.hostApiTls.clientIdentity %q must be DISTINCT from every stage/shared host-api identity (a stage cert must never be accepted as the executor)" $cid) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/* Executor trust-bundle Secret name: operator-supplied when connectorTools.hostApiTls.executorCaBundleSecretName
is set (externalSecret mode), else the chart-built `<fullname>-executor-ca-bundle` (stage CA + connector CA). */}}
{{- define "waddlebot.executorCaBundleSecretName" -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- $ht := $ct.hostApiTls | default dict -}}
{{- $ht.executorCaBundleSecretName | default (printf "%s-executor-ca-bundle" (include "waddlebot.fullname" .)) -}}
{{- end }}
