{{/*
Reusable snippets for the per-service ES256 machine JWT mechanism
(values.yaml `serviceJwt`).

"waddlebot.serviceJwtBootstrapVolume" -- the projected ServiceAccount
token volume a calling service (svc-process, svc-action, ...) mounts to
bootstrap against hub-api's gRPC ServiceAuthService (MintServiceToken). Include in
any Deployment's `.spec.template.spec.volumes`:

  {{- include "waddlebot.serviceJwtBootstrapVolume" . | nindent 8 }}

"waddlebot.serviceJwtBootstrapVolumeMount" -- the matching container
volumeMount, include in `.spec.template.spec.containers[].volumeMounts`:

  {{- include "waddlebot.serviceJwtBootstrapVolumeMount" . | nindent 12 }}

Not auto-injected into every Deployment template -- migrating the
existing ~47 services is tracked separately (see the v3.1.x GitHub issue
filed alongside this PR); these two helpers exist so that migration is
"include two snippets", not "write the volume spec 47 times".

"waddlebot.serviceJwtIdentitiesJson" -- renders `serviceJwt.identities`
(a map keyed by short service name -> {spiffeId, serviceAccount,
allowedScopes, tenant}) into the JSON array `SERVICE_JWT_IDENTITIES` hub-api's
`flask_core.service_jwt.load_identities_from_env` parses at startup:
`[{"service_id", "k8s_namespace", "k8s_service_account",
"allowed_scopes", "tenant"}, ...]`. `tenant` becomes the issued token's
`tenant` claim (`system` = operator plane, may act for any tenant; any other
value confines the token to that tenant); the internal gRPC server rejects
tokens without it. `spiffeId` is run through `tpl` because its
value in values.yaml is itself a template string (`{{ .Values.global.
environment | default "alpha" }}`) that must be rendered against the
current release context, not treated as a literal. Include only in
hub-api's own Deployment env (the only consumer):

  - name: SERVICE_JWT_IDENTITIES
    value: {{ include "waddlebot.serviceJwtIdentitiesJson" . | quote }}
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

{{- define "waddlebot.serviceJwtIdentitiesJson" -}}
{{- $root := . -}}
{{- $identities := list -}}
{{- $actionConnections := $root.Values.pipeline.rustDataPlane.svcAction.dynamicConnections.enabled -}}
{{- $actionOverlayPush := include "waddlebot.svcActionOverlayPush" $root -}}
{{- $processGameserver := $root.Values.pipeline.rustDataPlane.svcProcess.gameserverNet.enabled -}}
{{- $ingestDynamic := $root.Values.pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled -}}
{{- range $key, $identity := .Values.serviceJwt.identities }}
{{- /* svc-ingest's identity is a cross-tenant `system` credential on an internet-facing pod, so it is
least-privilege: it is ALWAYS rendered, but with ONLY `key:stream-dek:read` (the tenant stream-DEK
fetch it cannot encrypt a single event without) unless
pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled (default false), which adds the rest. */ -}}
{{- if true }}
{{- $scopes := list -}}
{{- range $scope := $identity.allowedScopes }}
{{- /* svc-action keeps its other scopes always, but its `connections:*` scopes (resolve +
resolve:static) render ONLY when pipeline.rustDataPlane.svcAction.dynamicConnections.enabled. */ -}}
{{- /* `presentation.overlay:push` (svc-action -> Rust svc-presentation PUSH JWT) renders ONLY while the Rust svc-presentation is the deployed runtime (waddlebot.svcActionOverlayPush). */ -}}
{{- /* svc-process's `connections:*:gameserver` scopes render ONLY when pipeline.rustDataPlane.svcProcess.gameserverNet.enabled (default false). */ -}}
{{- if and (or (ne $key "svc-action") $actionConnections (not (hasPrefix "connections:" $scope))) (or (ne $key "svc-process") $processGameserver (not (hasPrefix "connections:" $scope))) (or (ne $scope "presentation.overlay:push") $actionOverlayPush) (or (ne $key "svc-ingest") $ingestDynamic (eq $scope "key:stream-dek:read")) }}
{{- $scopes = append $scopes $scope -}}
{{- end }}
{{- end }}
{{- $spiffeId := tpl $identity.spiffeId $root -}}
{{- $identities = append $identities (dict "service_id" $spiffeId "k8s_namespace" $root.Values.namespace "k8s_service_account" $identity.serviceAccount "allowed_scopes" $scopes "tenant" ($identity.tenant | default "")) -}}
{{- end }}
{{- end }}
{{- $identities | toJson -}}
{{- end -}}

{{/*
"waddlebot.svcActionOverlayPush" -- renders the string "true" (else empty) when svc-action must be
able to push overlays to the Rust svc-presentation: the Rust runtime is deployed
(presentation.rust.enabled) and the service itself is on. Gates the `presentation.overlay:push`
identity scope, the SERVICE_JWT_* and PRESENTATION_URL env on svc-action and its egress policies, so
none of it renders in base/beta/prod where the Python svc-presentation is still the runtime.
*/}}
{{- define "waddlebot.svcActionOverlayPush" -}}
{{- if and .Values.pipeline.svcPresentation.enabled .Values.presentation.rust.enabled -}}true{{- end -}}
{{- end -}}

{{/*
`SERVICE_JWT_ACCEPT_LEGACY_EDDSA` env entry for every service that VERIFIES a service JWT
(hub-api, egress-proxy, svc-presentation, Python verifiers). Always rendered with an explicit
"true"/"false" so the verifier's strict parser never falls back on an unset default. Include as:

  {{- include "waddlebot.serviceJwtAcceptLegacyEnv" . | nindent 8 }}
*/}}
{{- define "waddlebot.serviceJwtAcceptLegacyEnv" -}}
- name: SERVICE_JWT_ACCEPT_LEGACY_EDDSA
  value: {{ .Values.serviceJwt.acceptLegacyEddsa | default false | quote }}
{{- end -}}
