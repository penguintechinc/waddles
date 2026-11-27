{{/*
SeaweedFS bucket initialization (creates bucketName, "recordings", the bundle-executor bucket,
the platform-secret escrow bucket and -- bundled mode -- the bundle-storage bucket if absent,
sets default SSE bucket encryption, and enables escrow versioning). ONE definition, rendered as:

  * an initContainer on hub-api (templates/hub-api.yaml) -- the default. hub-api's platform
    vault refuses to boot without the escrow bucket (hub_api/services/platform_secrets/
    bootstrap.py `escrow_unavailable`), so the bucket MUST exist before hub-api's main
    container starts. It used to be a post-install/post-upgrade hook Job, which deadlocked
    every FRESH install under `helm upgrade --wait` (deploy-dal2alpha.yml): --wait never
    completes while hub-api crash-loops, and post hooks only run after --wait. The init
    container waits (bounded) for the S3 gateway, then creates idempotently (create-if-absent,
    success if present); on failure the kubelet retries it with backoff, so a fresh install
    converges with no manual steps. hub-api's runtime fail-closed check is unchanged.
  * a post-install/post-upgrade hook Job (templates/infrastructure/seaweedfs.yaml) ONLY when
    hub-api is disabled -- nothing then blocks `--wait` on the buckets.

amazon/aws-cli (Apache-2.0): SeaweedFS's S3 API is AWS-S3-compatible, no vendor client needed.
*/}}

{{/* Fail the render when the script budget does not leave margin under the Job deadline. */}}
{{- define "waddlebot.seaweedfsBucketInit.validate" -}}
{{- $bij := .Values.infrastructure.seaweedfs.bucketInitJob }}
{{- if gt (int $bij.totalBudgetSeconds) (sub (int $bij.activeDeadlineSeconds) 60) }}
{{- fail (printf "infrastructure.seaweedfs.bucketInitJob.totalBudgetSeconds (%d) must be <= activeDeadlineSeconds (%d) - 60" (int $bij.totalBudgetSeconds) (int $bij.activeDeadlineSeconds)) }}
{{- end }}
{{- end -}}

{{/* The bucket-init container spec (a single list item). Call with the root context. */}}
{{- define "waddlebot.seaweedfsBucketInit.container" -}}
{{- include "waddlebot.seaweedfsBucketInit.validate" . }}
{{- $bij := .Values.infrastructure.seaweedfs.bucketInitJob }}
- name: seaweedfs-bucket-init
  image: {{ .Values.infrastructure.seaweedfs.bucketInitImage }}
  imagePullPolicy: IfNotPresent
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    seccompProfile:
      type: RuntimeDefault
    capabilities:
      drop: [ALL]
  command:
  - /bin/sh
  - -c
  - |
    set -eu
    # pipefail is absent from some sh builds; enable when supported.
    (set -o pipefail) 2>/dev/null && set -o pipefail
    export AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID}"
    export AWS_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY}"
    export AWS_DEFAULT_REGION="us-east-1"
    # feature/transit-tls-core (F5): https against the FQDN, internal CA pinned (aws cli reads AWS_CA_BUNDLE).
    export AWS_CA_BUNDLE="{{ include "waddlebot.transitTls.caPath" . }}"
    ENDPOINT="{{ include "waddlebot.transitTls.s3Endpoint" . }}"
    # Fail FAST and DIAGNOSABLY: the Job controller reaps the pod on
    # activeDeadlineSeconds (no logs survive), so the script enforces ONE total budget
    # (TOTAL_BUDGET) strictly below that deadline minus a margin for scheduling/image pull/
    # termination (default activeDeadlineSeconds 600 - 120 = 480). The budget is checked before every
    # call and caps each call's timeout to the remaining time, so a stall makes THIS script
    # exit non-zero with an `elapsed/budget` log line, leaving a Failed pod whose logs
    # `kubectl logs` can still read.
    TOTAL_BUDGET={{ int $bij.totalBudgetSeconds }}
    # Per-call cap (also used by the readiness wait loop): aws-cli v2 is a python cold start
    # and list-buckets is authenticated, so ~10s was intermittently killed on dal2alpha.
    CALL_TIMEOUT={{ int $bij.callTimeoutSeconds }}
    # Wall-clock seconds to keep retrying the readiness probe (time-based, not sleep-counted).
    WAIT_MAX={{ int $bij.waitMaxSeconds }}
    START_TS=$(date +%s)
    log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) bucket-init: $*"; }
    elapsed() { echo $(( $(date +%s) - START_TS )); }
    remaining() { echo $(( TOTAL_BUDGET - $(elapsed) )); }
    trap 'rc=$?; if [ "${rc}" -ne 0 ]; then log "ERROR: exiting rc=${rc} elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s"; fi' EXIT
    # budget_check: abort the whole script (main shell only) once the total budget is spent.
    budget_check() {
      if [ "$(remaining)" -le 0 ]; then
        log "ERROR: total budget exhausted elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s while: $*"
        exit 1
      fi
    }
    command -v timeout >/dev/null 2>&1 || { log "ERROR: 'timeout' not found in image; cannot bound aws calls"; exit 1; }
    # Absolute aws path: `timeout` re-execs its argument via execvp, which would execve() every
    # PATH entry in turn (/usr/local/sbin/aws first) -- the Tetragon exec allowlist on the
    # hub-api pod (waddlebot.seaweedfsBucketInit.execBinaries) SIGKILLs any non-allowlisted
    # execve filename, even one that would have failed with ENOENT.
    AWS_BIN=$(command -v aws) || { log "ERROR: 'aws' not found in image"; exit 1; }
    # capped_timeout <max>: min(max, remaining budget), never below 1s.
    capped_timeout() {
      r=$(remaining)
      if [ "${r}" -lt 1 ]; then echo 1; elif [ "${r}" -lt "$1" ]; then echo "${r}"; else echo "$1"; fi
    }
    awsx() {
      budget_check "aws $*"
      timeout "$(capped_timeout "${CALL_TIMEOUT}")" "${AWS_BIN}" --endpoint-url "$ENDPOINT" "$@"
    }
    # Bounded wait for the S3 gateway (post-hook can fire before SeaweedFS is Ready).
    log "waiting for S3 endpoint ${ENDPOINT} (max ${WAIT_MAX}s, total budget ${TOTAL_BUDGET}s)"
    WAIT_START=$(date +%s)
    attempt=0
    while :; do
      attempt=$((attempt + 1))
      budget_check "waiting for S3 endpoint ${ENDPOINT}"
      call_to=$(capped_timeout "${CALL_TIMEOUT}")
      t0=$(date +%s)
      rc=0
      err=$(timeout "${call_to}" "${AWS_BIN}" --endpoint-url "$ENDPOINT" s3api list-buckets 2>&1 >/dev/null) || rc=$?
      [ "${rc}" -eq 0 ] && break
      took=$(( $(date +%s) - t0 ))
      case "${rc}" in
        124) why="timeout after ${call_to}s (killed by our own per-call cap; not refused/TLS/403)" ;;
        *) why="aws exited rc=${rc} (refused/TLS/auth: see stderr)" ;;
      esac
      log "attempt ${attempt} failed rc=${rc} took=${took}s: ${why}; stderr: $(echo "${err}" | tail -n 3)"
      waited=$(( $(date +%s) - WAIT_START ))
      if [ "${waited}" -ge "${WAIT_MAX}" ]; then
        log "ERROR: S3 endpoint ${ENDPOINT} not ready after ${waited}s/${WAIT_MAX}s attempts=${attempt} last_rc=${rc} elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s; last error: $(echo "${err}" | tail -n 3)"
        exit 1
      fi
      sleep 3
    done
    waited=$(( $(date +%s) - WAIT_START ))
    log "S3 endpoint ready after ${waited}s (elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s)"
    # "recordings" (svc-streaming egress) and bundleExecutor.bucketName
    # ("waddles-bundles", core/bundle_executor's BUNDLE_BUCKET_NAME) --
    # both consumers expect their bucket to already exist; previously
    # only bucketName + "recordings" were created here, leaving
    # "waddles-bundles" as a pre-existing gap.
    EXPECTED={{ if eq .Values.bundleStorage.mode "bundled" }}5{{ else }}4{{ end }}
    VERIFIED=0
    for bucket in "{{ .Values.infrastructure.seaweedfs.bucketName }}" "recordings" "{{ .Values.pipeline.rustDataPlane.bundleExecutor.bucketName }}" "{{ .Values.infrastructure.seaweedfs.escrow.bucketName }}"{{ if eq .Values.bundleStorage.mode "bundled" }} "{{ .Values.bundleStorage.bucketName }}"{{ end }}; do
      budget_check "bucket ${bucket}"
      log "bucket ${bucket}: ensure exists (elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s)"
      # Best-effort mb (masked on purpose): the head-bucket || create-bucket gate below is the real check.
      { awsx s3 mb "s3://${bucket}" --region us-east-1 2>&1 | grep -qi "already"; } || true
      awsx s3api head-bucket --bucket "${bucket}" 2>/dev/null \
        || awsx s3api create-bucket --bucket "${bucket}" --region us-east-1
    {{- if and .Values.infrastructure.seaweedfs.encryption.enabled .Values.infrastructure.seaweedfs.encryption.autoEncryptBucket (or .Values.infrastructure.seaweedfs.encryption.secretName (include "waddlebot.kms.objectStorageActive" .)) }}
      # Default bucket encryption (SSE-S3) -- objects written without an
      # explicit ServerSideEncryption header are still encrypted at rest,
      # requires WEED_S3_SSE_KEK to already be configured on the server.
      # kms.objectStorage.mode=kms (templates/_kms.tpl) swaps the default to
      # SSE-KMS under the operator's key, ON TOP of the baseline above.
      log "bucket ${bucket}: put-bucket-encryption"
      awsx s3api put-bucket-encryption --bucket "${bucket}" \
        --server-side-encryption-configuration '{{ include "waddlebot.kms.bucketEncryptionConfig" . }}'
    {{- end }}
      awsx s3api head-bucket --bucket "${bucket}"
      VERIFIED=$((VERIFIED + 1))
      log "bucket ${bucket}: verified (${VERIFIED}/${EXPECTED})"
    done
    {{- if .Values.infrastructure.seaweedfs.escrow.versioning }}
    # Platform-secret escrow bucket: versioning keeps every prior object/manifest version so a
    # poisoned or mistaken overwrite is recoverable. Loud (not fatal) if unsupported.
    log "bucket {{ .Values.infrastructure.seaweedfs.escrow.bucketName }}: put-bucket-versioning"
    awsx s3api put-bucket-versioning --bucket "{{ .Values.infrastructure.seaweedfs.escrow.bucketName }}" \
      --versioning-configuration Status=Enabled \
      || echo "WARNING: could not enable versioning on {{ .Values.infrastructure.seaweedfs.escrow.bucketName }} -- escrow history is NOT retained" >&2
    {{- end }}
    awsx s3 ls
    if [ "${EXPECTED}" -lt 1 ] || [ "${VERIFIED}" -ne "${EXPECTED}" ]; then
      log "ERROR: ${VERIFIED}/${EXPECTED} buckets verified"
      exit 1
    fi
    log "${VERIFIED}/${EXPECTED} buckets verified (elapsed=$(elapsed)s/budget=${TOTAL_BUDGET}s)"
  env:
  # Dedicated "bucket-init" identity (Admin) -- never the shared rootUser/rootPassword admin
  # credential. Exposed ONLY to this run-to-completion container's env (env is per-container:
  # the long-running hub-api container never sees it). See values.yaml
  # infrastructure.seaweedfs.identities.bucketInit.
  - name: S3_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: waddlebot-secrets
        key: BUCKET_INIT_S3_ACCESS_KEY_ID
  - name: S3_SECRET_ACCESS_KEY
    valueFrom:
      secretKeyRef:
        name: waddlebot-secrets
        key: BUCKET_INIT_S3_SECRET_ACCESS_KEY
  resources:
    {{- toYaml $bij.resources | nindent 4 }}
  volumeMounts:
  {{- include "waddlebot.transitTls.caVolumeMount" . | nindent 2 }}
{{- end -}}
