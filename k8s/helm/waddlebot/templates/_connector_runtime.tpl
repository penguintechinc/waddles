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
{{- if or $cr.enabled (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "twitch")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "discord")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "youtube")) (include "waddlebot.connectorPlatformOn" (dict "root" . "platform" "spectrum")) -}}true{{- end -}}
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
{{- range $p := list "twitch" "discord" "youtube" -}}
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
{{- range $p := list "twitch" "discord" "youtube" "spectrum" -}}
{{- if include "waddlebot.connectorPlatformOn" (dict "root" $ "platform" $p) -}}
{{- $d := include "waddlebot.connectorDigest" (dict "root" $ "platform" $p) -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $d) -}}
{{- fail (printf "svcIngest.connectorRuntime.connector.%s.enabled requires pipeline.connectors.digests.%s = sha256:<64 lowercase hex> (got %q)" $p $p $d) -}}
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

{{/* Executor trust-bundle Secret name: operator-supplied when connectorTools.hostApiTls.executorCaBundleSecretName
is set (externalSecret mode), else the chart-built `<fullname>-executor-ca-bundle` (stage CA + connector CA). */}}
{{- define "waddlebot.executorCaBundleSecretName" -}}
{{- $ct := .Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- $ht := $ct.hostApiTls | default dict -}}
{{- $ht.executorCaBundleSecretName | default (printf "%s-executor-ca-bundle" (include "waddlebot.fullname" .)) -}}
{{- end }}

{{/* Stage host-api CA PEM known at render time: supplied value, else the live stage Secret. "" when
neither is available (fresh install whose stage Secret is generated in host-api-tls-secret.yaml, which
passes its own CA to connectorTrustSecrets instead). */}}
{{- define "waddlebot.stageCaPem" -}}
{{- $ht := .Values.global.hostApiTls -}}
{{- if $ht.ca.crt -}}{{ $ht.ca.crt }}
{{- else -}}
{{- $s := lookup "v1" "Secret" .Values.namespace (printf "%s-host-api-tls" (include "waddlebot.fullname" .)) -}}
{{- if $s -}}{{ index $s.data "ca.crt" | b64dec }}{{- end -}}
{{- end -}}
{{- end }}

{{/* SEPARATE connector trust domain: the svc-ingest connector server cert and the executor-only client
cert are issued by a connector CA that is NOT the stage CA (stage servers only chain-verify clients,
core/svc_action/src/host_api.rs, core/svc_process/src/host_api.rs, so a stage-CA cert for svc-ingest
would be accepted as a stage client). The executor trusts BOTH CAs via a bundle mounted over its
HOST_API_CA_FILE (stage dial still SAN-pins the stage identity, core/bundle_executor/src/tls.rs).
dict "root" . "stageCa" <PEM or "">. Emits the Secrets that must be minted/updated this render. */}}
{{- define "waddlebot.connectorTrustSecrets" -}}
{{- $ := .root -}}
{{- $ct := $.Values.pipeline.rustDataPlane.svcIngest.connectorTools | default dict -}}
{{- $ht := $ct.hostApiTls | default dict -}}
{{- $fullname := include "waddlebot.fullname" $ -}}
{{- $srvName := include "waddlebot.svcIngestHostApiTlsSecretName" $ -}}
{{- $cliName := include "waddlebot.ingestExecutorTlsSecretName" $ -}}
{{- $bundleName := include "waddlebot.executorCaBundleSecretName" $ -}}
{{- $clientId := include "waddlebot.svcIngestHostApiClientIdentity" $ -}}
{{- $ns := $.Values.namespace -}}
{{- $tier := $.Values.global.deploymentTier -}}
{{- $srvExisting := lookup "v1" "Secret" $ns $srvName -}}
{{- $cliExisting := lookup "v1" "Secret" $ns $cliName -}}
{{- $connCa := "" -}}
{{- if and $srvExisting $cliExisting -}}
{{- $connCa = index $srvExisting.data "ca.crt" | b64dec -}}
{{- else if $ht.externalSecret -}}
{{- if not $ht.executorCaBundleSecretName -}}
{{- fail (printf "svcIngest.connectorTools.hostApiTls.externalSecret=true requires hostApiTls.executorCaBundleSecretName: a Secret (key ca.crt) holding the stage CA + the connector CA, mounted over the executor's HOST_API_CA_FILE. %q/%q must chain to the connector CA, NOT the stage CA (stage servers only chain-verify clients)." $srvName $cliName) -}}
{{- end -}}
{{- else if not (or (eq $tier "alpha") (eq $tier "local")) -}}
{{- fail (printf "svcIngest.connectorTools.hostApiTls: Secrets %q and %q not found in %q and global.deploymentTier=%q is outside alpha/local -- supply them (ca.crt/tls.crt/tls.key each, issued by a connector CA SEPARATE from the stage CA; server cert SAN = the svc-ingest Service; client cert identity %q) or set hostApiTls.externalSecret=true with executorCaBundleSecretName" $srvName $cliName $ns $tier $clientId) -}}
{{- else -}}
{{- $ca := genCA (printf "%s-connector-host-api-ca" $fullname) 365 -}}
{{- $svc := printf "%s-svc-ingest-rust" $fullname -}}
{{- $srv := genSignedCert $svc nil (list $svc (printf "%s.%s.svc" $svc $ns) (printf "%s.%s.svc.cluster.local" $svc $ns)) 365 $ca -}}
{{- $cli := genSignedCert $clientId nil (list $clientId) 365 $ca -}}
{{- $connCa = $ca.Cert -}}
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
{{- end -}}
{{- if not $ht.externalSecret }}
{{- if not .stageCa }}
{{- fail (printf "svcIngest.connectorTools: cannot build %q -- the stage host-api CA is not available at render time (no global.hostApiTls.ca.crt and Secret %s-host-api-tls not found). Provision the stage Secret first, or use hostApiTls.externalSecret=true with executorCaBundleSecretName." $bundleName $fullname) }}
{{- end }}
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ $bundleName }}
  namespace: {{ $ns }}
  labels:
    {{- include "waddlebot.labels" $ | nindent 4 }}
    app.kubernetes.io/component: executor-ca-bundle
type: Opaque
stringData:
  ca.crt: {{ printf "%s\n%s" (.stageCa | trim) ($connCa | trim) | quote }}
{{- end }}
{{- end }}
