{{/*
executor-scale task 4 -- shared pod spec for the AOT precompile CronJob
(executor-precompile-cronjob.yaml) and backfill Job (executor-precompile-backfill-job.yaml).

Three sequential steps sharing one emptyDir (/work):
  1. initContainer export     (hub-api image)   python3 -m cli.precompile_queue export
  2. initContainer precompile (executor image)  bundle-executor --precompile
  3. container     ack        (hub-api image)   python3 -m cli.precompile_queue ack
The precompiler uses the SAME image/tag as svc-bundles-executor so its engine fingerprint is the
one the executors look up. It writes a report even when items fail (--report-failures-only) so the
ack step can acknowledge successes and then exit 1 for the failures (fail loud, nothing dropped).

Secrets: the dedicated precompile Ed25519 key is mounted as a FILE into the precompile container
only (never env, never the hub-api containers); bucket creds are the svc-bundles-precompiler
identity (Write limited to precompiled/).

Usage: include "waddlebot.executorPrecompile.podSpec" (dict "root" $ "mode" "queue"|"backfill")
*/}}
{{- define "waddlebot.executorPrecompile.podSpec" -}}
{{- $root := .root -}}
{{- $mode := .mode -}}
{{- $cfg := $root.Values.pipeline.hubApi.executorPrecompile -}}
{{- $fullname := include "waddlebot.fullname" $root -}}
{{- $ps := $root.Values.autoProvisionedKeys.precompileSigning -}}
{{- $psSecretName := $ps.secretName | default (printf "%s-executor-precompile-signing" $fullname) -}}
{{- $hubImage := include "waddlebot.legacyModuleImage" (dict "root" $root "module" "hub-api" "imageTag" ($root.Values.hubApi.imageTag | default $root.Values.global.imageTag)) -}}
restartPolicy: Never
serviceAccountName: {{ include "waddlebot.serviceAccountName" $root }}
automountServiceAccountToken: false
{{- include "waddlebot.imagePullSecrets" $root | nindent 0 }}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
initContainers:
- name: export
  image: "{{ $hubImage }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy }}
  command: ["python3", "-m", "cli.precompile_queue", "export", "--out", "/work/manifest.json"
    {{- if eq $mode "backfill" }}, "--all-active"{{ else }}, "--limit", {{ $cfg.batchLimit | quote }}{{ end }}]
  {{- include "waddlebot.executorPrecompile.hubApiContainer" (dict "root" $root "name" "export") | nindent 2 }}
- name: precompile
  image: "{{ $root.Values.bundleExecutor.image.repository }}:{{ $root.Values.bundleExecutor.image.tag | default $root.Values.global.imageTag | toString }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy | default "IfNotPresent" }}
  args: ["--precompile", "--manifest", "/work/manifest.json", "--report", "/work/report.json", "--report-failures-only"]
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
  env:
  - name: PRECOMPILE_SIGNING_KEY_FILE
    value: /run/precompile-signing/key
  - name: PRECOMPILE_SIGNING_KEY_ID
    valueFrom:
      secretKeyRef:
        name: {{ $psSecretName }}
        key: PRECOMPILE_SIGNING_KEY_ID
  {{- if $root.Values.pipeline.rustDataPlane.bundleSigningPublicKeys }}
  - name: BUNDLE_SIGNING_PUBLIC_KEYS
    value: {{ $root.Values.pipeline.rustDataPlane.bundleSigningPublicKeys | toJson | quote }}
  {{- else }}
  - name: BUNDLE_SIGNING_PUBLIC_KEYS
    valueFrom:
      configMapKeyRef:
        name: {{ $root.Values.autoProvisionedKeys.bundleSigning.publicKeyConfigMapName | default (printf "%s-bundle-signing-public" $fullname) }}
        key: BUNDLE_SIGNING_PUBLIC_KEYS
  {{- end }}
  - name: BUNDLE_BUCKET_ENDPOINT
    value: "{{ include "waddlebot.transitTls.s3Endpoint" $root }}"
  - name: BUNDLE_BUCKET_CA_FILE
    value: {{ include "waddlebot.transitTls.caPath" $root | quote }}
  - name: BUNDLE_BUCKET_NAME
    value: {{ $root.Values.pipeline.rustDataPlane.bundleExecutor.bucketName | quote }}
  - name: BUNDLE_BUCKET_REGION
    value: {{ $root.Values.pipeline.rustDataPlane.bundleExecutor.bucketRegion | quote }}
  - name: BUNDLE_BUCKET_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: {{ $fullname }}-secrets
        key: PRECOMPILER_S3_ACCESS_KEY_ID
  - name: BUNDLE_BUCKET_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef:
        name: {{ $fullname }}-secrets
        key: PRECOMPILER_S3_SECRET_ACCESS_KEY
  - name: OTEL_SERVICE_NAME
    value: svc-bundles-precompiler
  resources:
    {{- toYaml $cfg.resources | nindent 4 }}
  volumeMounts:
  - name: work
    mountPath: /work
  - name: tmp
    mountPath: /tmp
  - name: precompile-signing
    mountPath: /run/precompile-signing
    readOnly: true
  {{- include "waddlebot.transitTls.caVolumeMount" $root | nindent 2 }}
containers:
- name: ack
  image: "{{ $hubImage }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy }}
  command: ["python3", "-m", "cli.precompile_queue", "ack", "--report", "/work/report.json", "--manifest", "/work/manifest.json"{{ if eq $mode "backfill" }}, "--backfill"{{ end }}]
  {{- include "waddlebot.executorPrecompile.hubApiContainer" (dict "root" $root "name" "ack") | nindent 2 }}
volumes:
- name: work
  emptyDir:
    sizeLimit: 64Mi
- name: tmp
  emptyDir: {}
- name: precompile-signing
  secret:
    secretName: {{ $psSecretName }}
    defaultMode: 0400
    items:
    - key: PRECOMPILE_SIGNING_PRIVATE_KEY
      path: key
{{- include "waddlebot.transitTls.caVolume" $root | nindent 0 }}
{{- include "waddlebot.valkeyTlsCaVolume" $root | nindent 0 }}
{{- end }}

{{/* Common hub-api CLI container body (env, DB identity, security, mounts). */}}
{{- define "waddlebot.executorPrecompile.hubApiContainer" -}}
{{- $root := .root -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
envFrom:
- configMapRef:
    name: {{ include "waddlebot.fullname" $root }}-config
- secretRef:
    name: {{ include "waddlebot.fullname" $root }}-secrets
env:
{{- include "waddlebot.dbEnv" (dict "root" $root "role" "waddles_hub_api") | nindent 0 }}
{{- include "waddlebot.transitTls.clientEnv" $root | nindent 0 }}
- name: MODULE_NAME
  value: "executor-precompile-{{ .name }}"
resources:
  {{- toYaml $root.Values.pipeline.hubApi.executorPrecompile.hubApiResources | nindent 2 }}
volumeMounts:
- name: work
  mountPath: /work
- name: tmp
  mountPath: /tmp
{{- include "waddlebot.transitTls.caVolumeMount" $root | nindent 0 }}
{{- include "waddlebot.valkeyTlsCaVolumeMount" $root | nindent 0 }}
{{- end }}
