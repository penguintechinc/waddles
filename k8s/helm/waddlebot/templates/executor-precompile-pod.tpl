{{/*
executor-scale task 4 -- shared pod spec for the AOT precompile CronJob
(executor-precompile-cronjob.yaml) and backfill Job (executor-precompile-backfill-job.yaml).

Four sequential steps; compile and sign are SEPARATE containers so the process that runs Cranelift
over attacker-influenced wasm never holds the signing key (security review M1):
  1. initContainer export   (hub-api image)   python3 -m cli.precompile_queue export
  2. initContainer compile  (executor image)  bundle-executor --precompile-compile-only
       NO key mount, bucket READ-only identity (svc-bundles-executor's), /work mounted read-only;
       writes <digest>.cwasm + compiled.json to the `staged` emptyDir.
  3. initContainer sign     (executor image)  bundle-executor --precompile-sign
       key secret mounted 0400 here ONLY; hashes + signs + uploads, never compiles or loads a module;
       `staged` mounted read-only; bucket identity is the write-scoped svc-bundles-precompiler.
  4. container     ack      (hub-api image)   python3 -m cli.precompile_queue ack
The executor image/tag is the SAME as svc-bundles-executor so the engine fingerprint matches. The sign
step writes a report even when items fail (--report-failures-only) so ack can acknowledge successes
and then exit 1 for failures (fail loud, nothing dropped).

M2: the hub-api containers take ONLY the explicit env they read (DB identity via secretKeyRef, Postgres
TLS client env, MODULE_NAME) -- no envFrom of the shared <fullname>-secrets/-config -- and the pod runs
as its own ServiceAccount with no API token (executor-precompile-security.yaml).

Usage: include "waddlebot.executorPrecompile.podSpec" (dict "root" $ "mode" "queue"|"backfill")
*/}}
{{- define "waddlebot.executorPrecompile.podSpec" -}}
{{- $root := .root -}}
{{- $mode := .mode -}}
{{- $cfg := $root.Values.pipeline.hubApi.executorPrecompile -}}
{{- $fullname := include "waddlebot.fullname" $root -}}
{{- $ps := $root.Values.autoProvisionedKeys.precompileSigning -}}
{{- $psSecretName := $ps.secretName | default (printf "%s-executor-precompile-signing" $fullname) -}}
{{- $psCmName := $ps.publicKeyConfigMapName | default (printf "%s-executor-precompile-signing-public" $fullname) -}}
{{- $hubImage := include "waddlebot.legacyModuleImage" (dict "root" $root "module" "hub-api" "imageTag" ($root.Values.hubApi.imageTag | default $root.Values.global.imageTag)) -}}
{{- $execImage := printf "%s:%s" $root.Values.bundleExecutor.image.repository ($root.Values.bundleExecutor.image.tag | default $root.Values.global.imageTag | toString) -}}
restartPolicy: Never
serviceAccountName: {{ include "waddlebot.executorPrecompile.serviceAccountName" $root }}
automountServiceAccountToken: false
{{- with $cfg.runtimeClassName }}
# Value-driven Kata isolation (default off) -- enabling it for an `internal` class is pending user approval.
runtimeClassName: {{ . | quote }}
{{- end }}
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
- name: compile
  image: "{{ $execImage }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy | default "IfNotPresent" }}
  args: ["--precompile-compile-only", "--manifest", "/work/manifest.json", "--out-dir", "/staged"]
  {{- include "waddlebot.executorPrecompile.executorSecurityContext" $root | nindent 2 }}
  env:
  {{- include "waddlebot.executorPrecompile.bucketEnv" (dict "root" $root "accessKey" "DATAPLANE_S3_ACCESS_KEY_ID" "secretKey" "DATAPLANE_S3_SECRET_ACCESS_KEY") | nindent 2 }}
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
  # Public half only (not secret): lets the compile step skip already-built artifacts.
  - name: PRECOMPILE_SIGNING_PUBLIC_KEYS
    valueFrom:
      configMapKeyRef:
        name: {{ $psCmName }}
        key: PRECOMPILE_SIGNING_PUBLIC_KEYS
        optional: true
  resources:
    {{- toYaml $cfg.resources | nindent 4 }}
  volumeMounts:
  - name: work
    mountPath: /work
    readOnly: true
  - name: staged
    mountPath: /staged
  - name: tmp
    mountPath: /tmp
  {{- include "waddlebot.transitTls.caVolumeMount" $root | nindent 2 }}
- name: sign
  image: "{{ $execImage }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy | default "IfNotPresent" }}
  args: ["--precompile-sign", "--manifest", "/work/manifest.json", "--in-dir", "/staged", "--report", "/work/report.json", "--report-failures-only"]
  {{- include "waddlebot.executorPrecompile.executorSecurityContext" $root | nindent 2 }}
  env:
  {{- include "waddlebot.executorPrecompile.bucketEnv" (dict "root" $root "accessKey" "PRECOMPILER_S3_ACCESS_KEY_ID" "secretKey" "PRECOMPILER_S3_SECRET_ACCESS_KEY") | nindent 2 }}
  - name: PRECOMPILE_SIGNING_KEY_FILE
    value: /run/precompile-signing/key
  - name: PRECOMPILE_SIGNING_KEY_ID
    valueFrom:
      secretKeyRef:
        name: {{ $psSecretName }}
        key: PRECOMPILE_SIGNING_KEY_ID
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "1"
      memory: "1Gi"
  volumeMounts:
  - name: work
    mountPath: /work
  - name: staged
    mountPath: /staged
    readOnly: true
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
- name: staged
  emptyDir:
    sizeLimit: {{ $cfg.stagedSizeLimit | default "2Gi" }}
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
{{- end }}

{{/* Dedicated ServiceAccount name for the precompile pods (never the shared waddlebot SA). */}}
{{- define "waddlebot.executorPrecompile.serviceAccountName" -}}
{{- $cfg := .Values.pipeline.hubApi.executorPrecompile -}}
{{- (($cfg.serviceAccount).name) | default (printf "%s-executor-precompile" (include "waddlebot.fullname" .)) -}}
{{- end }}

{{/* Restricted securityContext shared by the two executor-image containers. */}}
{{- define "waddlebot.executorPrecompile.executorSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{/* Bucket env for an executor-image container; the S3 identity (secret key names) is per step. */}}
{{- define "waddlebot.executorPrecompile.bucketEnv" -}}
{{- $root := .root -}}
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
      name: {{ include "waddlebot.fullname" $root }}-secrets
      key: {{ .accessKey }}
- name: BUNDLE_BUCKET_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "waddlebot.fullname" $root }}-secrets
      key: {{ .secretKey }}
- name: OTEL_SERVICE_NAME
  value: svc-bundles-precompiler
{{- end }}

{{/*
Common hub-api CLI container body. Explicit env ONLY (M2): the CLI reads DATABASE_URL (built from the
precompile DB role's own password secretKeyRef) and Postgres TLS client settings -- it never touches
the shared <fullname>-secrets, JWT/S3/Valkey/KMS material, so none of it is injected.
*/}}
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
env:
{{- include "waddlebot.dbEnv" (dict "root" $root "role" "waddles_hub_api") | nindent 0 }}
{{- include "waddlebot.transitTls.pgClientEnv" $root | nindent 0 }}
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
{{- end }}
