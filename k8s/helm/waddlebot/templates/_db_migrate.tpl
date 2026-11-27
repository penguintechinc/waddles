{{/*
Database migration container (migrations image: `alembic upgrade head` + the role/grant
reconciles in migrations/run-alembic.sh). ONE definition, rendered as:

  * an initContainer on hub-api (templates/hub-api.yaml) -- runs on EVERY install and upgrade,
    before the hub-api container. hub-api `/ready` waits for the alembic head revision, so the
    schema MUST exist before hub-api can become Ready. The former `post-install` hook Job
    deadlocked every FRESH install under `helm upgrade --install --wait`: --wait never completes
    while hub-api is unready, and post hooks only run after --wait. Postgres is a regular
    resource created alongside hub-api; run-alembic.sh's bounded wait-for-DB plus kubelet
    init-container retries converge with no manual steps (same pattern as
    _seaweedfs_bucket_init.tpl, fix/escrow-bucket-init-ordering).
  * the `pre-upgrade` hook Job (templates/migrations-job.yaml) -- upgrades migrate BEFORE any new
    pod rolls, so no new-version workload ever sees the old schema; the initContainer that
    follows then finds the schema at head and only re-asserts the idempotent reconciles.

Concurrency: the command is migrations/migrate_locked.py, which holds a session-level
`pg_advisory_lock` for the whole run, so N hub-api replicas plus the Job serialize instead of
racing (alembic/env.py additionally locks `upgrade head` on its own key).

Credentials: the dedicated migrator DSN (`<fullname>-db-migrate-secret`, created by the
provision-secrets pre-install/pre-upgrade hook) is envFrom'd into THIS container only -- env is
per-container, so the long-running hub-api container keeps its own least-privilege role.
*/}}

{{/* Migrations image reference (empty-registry guard shared by both call sites). */}}
{{- define "waddlebot.dbMigrate.image" -}}
{{- $registry := .Values.global.imageRegistry | default "" }}
{{- $repository := .Values.modules.migrations.image | default "migrations" }}
{{- $tag := include "waddlebot.resolveImageTag" (dict "root" . "image" $repository "explicit" .Values.modules.migrations.imageTag) }}
{{- if $registry -}}
{{ $registry }}/{{ $repository }}:{{ $tag }}
{{- else -}}
{{ $repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/* Dedicated scratch volume name (never shared with the hub-api container's /tmp). */}}
{{- define "waddlebot.dbMigrate.tmpVolumeName" -}}db-migrate-tmp{{- end -}}

{{/* The db-migrate container spec (a single list item). Call with the root context. */}}
{{- define "waddlebot.dbMigrate.container" -}}
- name: db-migrate
  image: {{ include "waddlebot.dbMigrate.image" . | quote }}
  imagePullPolicy: {{ .Values.global.imagePullPolicy | default "IfNotPresent" }}
  # Absolute paths only: the Tetragon exec allowlist (values pipeline.migrationsJob.execBinaries)
  # matches the execve filename verbatim.
  command: ["/usr/local/bin/python3", "/app/migrate_locked.py", "/app/run.sh"]
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  envFrom:
  - secretRef:
      name: {{ include "waddlebot.fullname" . }}-db-migrate-secret
  # feature/transit-tls-core (F1): DATABASE_URL already carries sslmode=verify-full&sslrootcert; the
  # libpq env is the belt-and-braces for any code path that rebuilds the DSN.
  env:
  - name: MIGRATE_DB_WAIT_SECONDS
    value: {{ .Values.pipeline.migrationsJob.dbWaitSeconds | quote }}
  - name: MIGRATE_LOCK_TIMEOUT_SECONDS
    value: {{ .Values.pipeline.migrationsJob.lockTimeoutSeconds | quote }}
  - name: PYTHONDONTWRITEBYTECODE
    value: "1"
  {{- include "waddlebot.transitTls.pgClientEnv" . | nindent 2 }}
  resources:
    {{- toYaml .Values.pipeline.migrationsJob.resources | nindent 4 }}
  volumeMounts:
  - name: {{ include "waddlebot.dbMigrate.tmpVolumeName" . }}
    mountPath: /tmp
  {{- include "waddlebot.transitTls.caVolumeMount" . | nindent 2 }}
{{- end -}}

{{/* The db-migrate scratch volume (a single list item). */}}
{{- define "waddlebot.dbMigrate.volume" -}}
- name: {{ include "waddlebot.dbMigrate.tmpVolumeName" . }}
  emptyDir:
    sizeLimit: 64Mi
{{- end -}}
