{{/*
PR-H -- connector runtime (inbound manager) helpers. Spec:
docs/superpowers/specs/2026-11-05-connector-runtime-inbound-manager.md SS1.3/SS2/SS5/SS7/SS8.
Env names are NORMATIVE (spec SS8). Everything here is inert while every overlay and
svcIngest.connectorRuntime.enabled are false: no helper emits output, so an all-OFF render is
byte-identical to a render without this feature.
*/}}

{{/* "true" when overlay <platform> is ON (dict "root" . "platform" "twitch"); empty otherwise. */}}
{{- define "waddlebot.connectorOverlayOn" -}}
{{- $cr := .root.Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict -}}
{{- $ov := $cr.overlay | default dict -}}
{{- $p := index $ov .platform | default dict -}}
{{- if $p.enabled -}}true{{- end -}}
{{- end }}

{{/* "true" when the runtime is active: master switch OR any platform overlay ON. */}}
{{- define "waddlebot.connectorRuntimeActive" -}}
{{- $cr := .Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict -}}
{{- if or $cr.enabled (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "twitch")) (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "discord")) (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "youtube")) -}}true{{- end -}}
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
OVERLAY_<P>_ENABLED + CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS env list items. Included by BOTH the
Rust and the Python svc-ingest deployments so the two read ONE helm value and cannot disagree.
Emits nothing while the runtime is inactive.
*/}}
{{- define "waddlebot.connectorOverlayEnv" -}}
{{- if include "waddlebot.connectorRuntimeActive" . -}}
{{- $cr := .Values.pipeline.rustDataPlane.svcIngest.connectorRuntime | default dict }}
- name: OVERLAY_TWITCH_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "twitch")) "true") | quote }}
- name: OVERLAY_DISCORD_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "discord")) "true") | quote }}
- name: OVERLAY_YOUTUBE_ENABLED
  value: {{ ternary "true" "false" (eq (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "youtube")) "true") | quote }}
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
{{- range $p := list "twitch" "discord" "youtube" -}}
{{- if index ($ct.digests | default dict) $p -}}
{{- fail (printf "svcIngest.connectorTools.digests.%s is removed -- set pipeline.connectors.digests.%s (single source of truth for SENDER_EXEC_DIGEST_%s and CONNECTOR_PIN_DIGEST_%s)" $p $p ($p | upper) ($p | upper)) -}}
{{- end -}}
{{- end -}}
{{- if include "waddlebot.connectorRuntimeActive" . -}}
{{- if and (include "waddlebot.connectorOverlayOn" (dict "root" . "platform" "discord")) (not $cr.identifyBudgetReady) -}}
{{- fail "discord overlay requires the identify budget (not yet ported)" -}}
{{- end -}}
{{- if not (and $rdp.enabled $si.enabled) -}}
{{- fail "svcIngest.connectorRuntime requires pipeline.rustDataPlane.enabled and pipeline.rustDataPlane.svcIngest.enabled (the runtime lives in the Rust svc-ingest)" -}}
{{- end -}}
{{- if not $ct.enabled -}}
{{- fail "svcIngest.connectorRuntime (any overlay ON) requires svcIngest.connectorTools.enabled (host-api listener, TLS secrets, hub-api resolver)" -}}
{{- end -}}
{{- range $p := list "twitch" "discord" "youtube" -}}
{{- if include "waddlebot.connectorOverlayOn" (dict "root" $ "platform" $p) -}}
{{- $d := include "waddlebot.connectorDigest" (dict "root" $ "platform" $p) -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $d) -}}
{{- fail (printf "svcIngest.connectorRuntime.overlay.%s.enabled requires pipeline.connectors.digests.%s = sha256:<64 lowercase hex> (got %q)" $p $p $d) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $wp := ($cr.webhookPlatforms | default list) -}}
{{- if not (has $wp (list "twitch" "discord")) -}}
{{- fail (printf "svcIngest.connectorRuntime.webhookPlatforms: %q is not one of twitch, discord" $wp) -}}
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

{{/* True when host-api-tls-secret.yaml will (re)generate the stage Secret this render (alpha/local,
no kept-current Secret, no supplied material, not external) -- the one moment its CA is in hand to
also sign the connector server + executor client certs. Mirrors that template's branch order. */}}
{{- define "waddlebot.stageHostApiGenerates" -}}
{{- $ht := .Values.global.hostApiTls -}}
{{- $tier := .Values.global.deploymentTier -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- $cht := $ct.hostApiTls | default dict -}}
{{- if and .Values.pipeline.rustDataPlane.enabled .Values.pipeline.rustDataPlane.svcIngest.enabled $ct.enabled (not .Values.global.hostApiTls.certManager.enabled) (or (eq $tier "alpha") (eq $tier "local")) (not $cht.externalSecret) (not $ht.externalSecret) (not (and $ht.ca.crt $ht.tls.crt $ht.tls.key)) -}}
{{- $existing := lookup "v1" "Secret" .Values.namespace (printf "%s-host-api-tls" (include "waddlebot.fullname" .)) -}}
{{- $current := include "waddlebot.tlsSecretIdentityMatches" (dict "existing" $existing "identity" (include "waddlebot.hostApiStageIdentity" .) "sansSha256" (include "waddlebot.hostApiTlsSansSha256" .)) -}}
{{- if not $current -}}true{{- end -}}
{{- end -}}
{{- end }}

{{/* Emits the svc-ingest connector server Secret + the executor-only client Secret, both signed by
the CA passed in (the stage host-api CA). Called from host-api-tls-secret.yaml's generate branch. */}}
{{- define "waddlebot.connectorHostApiTlsSecrets" -}}
{{- $ := .root -}}
{{- $ca := .ca -}}
{{- $fullname := include "waddlebot.fullname" $ }}
{{- $srvName := include "waddlebot.svcIngestHostApiTlsSecretName" $ }}
{{- $cliName := include "waddlebot.ingestExecutorTlsSecretName" $ }}
{{- $clientId := include "waddlebot.svcIngestHostApiClientIdentity" $ }}
{{- $ns := $.Values.namespace }}
{{- $svc := printf "%s-svc-ingest-rust" $fullname }}
{{- $srv := genSignedCert $svc nil (list $svc (printf "%s.%s.svc" $svc $ns) (printf "%s.%s.svc.cluster.local" $svc $ns)) 365 $ca }}
{{- $cli := genSignedCert $clientId nil (list $clientId) 365 $ca }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ $srvName }}
  namespace: {{ $ns }}
  labels:
    {{- include "waddlebot.labels" $ | nindent 4 }}
    app.kubernetes.io/component: svc-ingest-host-api-tls
  annotations:
    helm.sh/resource-policy: keep
type: Opaque
stringData:
  ca.crt: {{ $ca.Cert | quote }}
  tls.crt: {{ $srv.Cert | quote }}
  tls.key: {{ $srv.Key | quote }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ $cliName }}
  namespace: {{ $ns }}
  labels:
    {{- include "waddlebot.labels" $ | nindent 4 }}
    app.kubernetes.io/component: ingest-executor-tls
  annotations:
    helm.sh/resource-policy: keep
type: Opaque
stringData:
  ca.crt: {{ $ca.Cert | quote }}
  tls.crt: {{ $cli.Cert | quote }}
  tls.key: {{ $cli.Key | quote }}
{{- end }}
