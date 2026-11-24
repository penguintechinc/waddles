{{/*
executor-scale task 4 -- shared pod spec for the AOT precompile CronJob
(executor-precompile-cronjob.yaml). The per-release enqueue Job (executor-precompile-enqueue-job.yaml)
only queues rows and reuses the hub-api container helper below, not this pod spec.

Four sequential steps; compile and sign are SEPARATE containers so the process that runs Cranelift
over attacker-influenced wasm never holds the signing key (security review M1):
  1. initContainer export   (hub-api image)   python3 -m cli.precompile_queue export
  2. initContainer compile  (executor image)  bundle-executor --precompile-compile-only
       --compile-concurrency N (bounded parallel; N validated against the container limits);
       skips digests that already have a valid signed artifact (public keys REQUIRED); NO key mount, bucket READ-only identity (svc-bundles-executor's), /work mounted read-only;
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

Usage: include "waddlebot.executorPrecompile.podSpec" (dict "root" $)
*/}}
{{- define "waddlebot.executorPrecompile.podSpec" -}}
{{- $root := .root -}}
{{- $cfg := $root.Values.pipeline.hubApi.executorPrecompile -}}
{{- $fullname := include "waddlebot.fullname" $root -}}
{{- $ps := $root.Values.autoProvisionedKeys.precompileSigning -}}
{{- $psSecretName := $ps.secretName | default (printf "%s-executor-precompile-signing" $fullname) -}}
{{- $psCmName := $ps.publicKeyConfigMapName | default (printf "%s-executor-precompile-signing-public" $fullname) -}}
{{- $hubImage := include "waddlebot.legacyModuleImage" (dict "root" $root "module" "hub-api" "imageTag" $root.Values.hubApi.imageTag) -}}
{{- $execImage := printf "%s:%s" $root.Values.bundleExecutor.image.repository (include "waddlebot.resolveImageTag" (dict "root" $root "image" "svc-bundles-executor" "explicit" $root.Values.bundleExecutor.image.tag)) -}}
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
  command: ["python3", "-m", "cli.precompile_queue", "export", "--out", "/work/manifest.json", "--limit", {{ $cfg.batchLimit | quote }}]
  {{- include "waddlebot.executorPrecompile.hubApiContainer" (dict "root" $root "name" "export") | nindent 2 }}
- name: compile
  image: "{{ $execImage }}"
  imagePullPolicy: {{ $root.Values.global.imagePullPolicy | default "IfNotPresent" }}
  args: ["--precompile-compile-only", "--manifest", "/work/manifest.json", "--out-dir", "/staged", "--compile-concurrency", {{ $cfg.compileConcurrency | int | toString | quote }}]
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
  # Public half only (not secret): REQUIRED -- it is how the compile step skips digests that
  # already have a valid signed artifact. Missing = the pod fails to start (loud), never a
  # silent recompile of everything.
  - name: PRECOMPILE_SIGNING_PUBLIC_KEYS
    valueFrom:
      configMapKeyRef:
        name: {{ $psCmName }}
        key: PRECOMPILE_SIGNING_PUBLIC_KEYS
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
  command: ["python3", "-m", "cli.precompile_queue", "ack", "--report", "/work/report.json", "--manifest", "/work/manifest.json"]
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

{{/*
Fails the render unless compileConcurrency fits the compile container: 1..32, <= the CPU limit in
whole cores (one Cranelift compile = one core; wasmtime has no parallel-compilation), and
compileConcurrency x compileMemoryPerWorkerMi <= the memory limit. Accepts "4"/"4000m" CPU and
Gi/Mi/G/M memory quantities.
*/}}
{{- define "waddlebot.executorPrecompile.validateConcurrency" -}}
{{- $cfg := .Values.pipeline.hubApi.executorPrecompile -}}
{{- $n := int $cfg.compileConcurrency -}}
{{- if or (lt $n 1) (gt $n 32) -}}
{{- fail (printf "pipeline.hubApi.executorPrecompile.compileConcurrency must be 1..32, got %v" $cfg.compileConcurrency) -}}
{{- end -}}
{{- $cpu := toString $cfg.resources.limits.cpu -}}
{{- $cpuMilli := 0 -}}
{{- if hasSuffix "m" $cpu -}}
{{- $cpuMilli = int (trimSuffix "m" $cpu) -}}
{{- else -}}
{{- $cpuMilli = int (mulf (float64 $cpu) 1000) -}}
{{- end -}}
{{- if gt (mul $n 1000) $cpuMilli -}}
{{- fail (printf "pipeline.hubApi.executorPrecompile.compileConcurrency (%d) exceeds resources.limits.cpu (%s)" $n $cpu) -}}
{{- end -}}
{{- $mem := toString $cfg.resources.limits.memory -}}
{{- $memMi := 0 -}}
{{- if hasSuffix "Gi" $mem -}}{{- $memMi = mul (int (trimSuffix "Gi" $mem)) 1024 -}}
{{- else if hasSuffix "Mi" $mem -}}{{- $memMi = int (trimSuffix "Mi" $mem) -}}
{{- else if hasSuffix "G" $mem -}}{{- $memMi = div (mul (int (trimSuffix "G" $mem)) 1000000000) 1048576 -}}
{{- else if hasSuffix "M" $mem -}}{{- $memMi = div (mul (int (trimSuffix "M" $mem)) 1000000) 1048576 -}}
{{- else -}}
{{- fail (printf "pipeline.hubApi.executorPrecompile.resources.limits.memory %q: use a Gi/Mi/G/M quantity" $mem) -}}
{{- end -}}
{{- $need := mul $n (int $cfg.compileMemoryPerWorkerMi) -}}
{{- if gt $need $memMi -}}
{{- fail (printf "pipeline.hubApi.executorPrecompile.compileConcurrency (%d) x compileMemoryPerWorkerMi (%v) = %dMi exceeds resources.limits.memory (%s)" $n $cfg.compileMemoryPerWorkerMi $need $mem) -}}
{{- end -}}
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
{{- /* No OTEL_* env here: the pod has no OTLP endpoint/egress rule, so a lone OTEL_SERVICE_NAME was inert
       and broke the "disabled => zero OTEL_* env" contract (test_otel_signoz_render). */}}
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
