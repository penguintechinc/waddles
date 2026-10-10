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
{{- range $key, $identity := .Values.serviceJwt.identities }}
{{- $spiffeId := tpl $identity.spiffeId $root -}}
{{- $identities = append $identities (dict "service_id" $spiffeId "k8s_namespace" $root.Values.namespace "k8s_service_account" $identity.serviceAccount "allowed_scopes" $identity.allowedScopes "tenant" ($identity.tenant | default "")) -}}
{{- end }}
{{- $identities | toJson -}}
{{- end -}}
