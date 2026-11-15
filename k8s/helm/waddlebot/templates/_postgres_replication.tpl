{{/*
PostgreSQL primary + read-replica building blocks (templates/infrastructure/postgres.yaml).
Text defines live here so the ConfigMaps AND the pod-template checksums render from one source.
*/}}

{{/* pg_hba.conf: TLS-only; `local` stays trust (probes, initdb); replication role is hostssl + scram. */}}
{{- define "waddlebot.postgres.hba" -}}
local   all   all                  trust
{{- if include "waddlebot.postgres.replicasActive" . }}
hostssl replication {{ .Values.infrastructure.postgresql.readReplicas.replicationUser }} 0.0.0.0/0 scram-sha-256
hostssl replication {{ .Values.infrastructure.postgresql.readReplicas.replicationUser }} ::/0      scram-sha-256
hostnossl replication all 0.0.0.0/0    reject
hostnossl replication all ::/0         reject
{{- end }}
hostssl all   all   0.0.0.0/0      scram-sha-256
hostssl all   all   ::/0           scram-sha-256
hostnossl all all   0.0.0.0/0      reject
hostnossl all all   ::/0           reject
{{- end }}

{{/*
Primary sidecar reconciler: idempotently (re)asserts the replication role and one physical slot per replica
(replica_0..N-1) over the local trust socket, then re-checks every 5 minutes. A sidecar (not an image
init-script, which only runs on an EMPTY PGDATA and so never on the existing alpha PVC; not a postStart hook,
which races server start and has no retry) also re-creates slots after a scale-up. The password reaches psql
through the environment + a backtick \set (never argv) and statement logging is disabled for the session.
*/}}
{{- define "waddlebot.postgres.bootstrapScript" -}}
set -eu
sock=/var/run/postgresql
sleep_s=5
while :; do
  if pg_isready -q -h "$sock"; then
    if out=$(psql -X -q -v ON_ERROR_STOP=1 -h "$sock" -U "$POSTGRES_USER" -d postgres 2>&1 <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_duration_statement = -1;
\set ru `printf %s "$REPL_USER"`
\set pw `printf %s "$REPL_PASSWORD"`
\set n `printf %s "$REPLICA_COUNT"`
SELECT format('CREATE ROLE %I', :'ru') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ru') \gexec
SELECT format('ALTER ROLE %I WITH REPLICATION LOGIN NOSUPERUSER PASSWORD %L', :'ru', :'pw') \gexec
SELECT pg_create_physical_replication_slot('replica_' || i)
  FROM generate_series(0, :n - 1) AS i
 WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = 'replica_' || i);
SQL
    ); then
      echo "repl-bootstrap: role and $REPLICA_COUNT slot(s) asserted"
      sleep_s=300
    else
      echo "repl-bootstrap: reconcile failed (retrying): $(printf %s "$out" | sed -E "s/PASSWORD '[^']*'/PASSWORD '****'/g")" >&2
      sleep_s=5
    fi
  fi
  sleep "$sleep_s"
done
{{- end }}

{{/* Replica initContainer: pg_basebackup from the primary over verify-full TLS, ONLY when PGDATA has no cluster. */}}
{{- define "waddlebot.postgres.basebackupScript" -}}
set -eu
ord="${HOSTNAME##*-}"
if [ -s "$PGDATA/PG_VERSION" ]; then
  echo "basebackup: PGDATA already initialised, skipping"
  exit 0
fi
mkdir -p "$PGDATA"
chmod 700 "$PGDATA"
i=0
while [ "$i" -lt 60 ]; do
  # No PG_VERSION => any content is a half-finished earlier attempt of THIS init container; safe to clear.
  find "$PGDATA" -mindepth 1 -delete
  if pg_basebackup -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$REPL_USER" -w \
       -D "$PGDATA" -X stream -c fast -R -S "replica_${ord}"; then
    chmod 700 "$PGDATA"
    echo "basebackup: seeded from $PRIMARY_HOST with slot replica_${ord}"
    exit 0
  fi
  i=$((i + 1))
  echo "basebackup: attempt $i failed (primary/slot not ready?), retrying in 5s" >&2
  sleep 5
done
echo "basebackup: gave up after 60 attempts" >&2
exit 1
{{- end }}

{{/*
Replica readiness: in recovery AND the WAL receiver is streaming AND (replay has caught up with everything
received OR replay is younger than MAX_LAG_SECONDS). Receive==replay covers an IDLE primary, where
pg_last_xact_replay_timestamp() goes stale although nothing is behind.
*/}}
{{- define "waddlebot.postgres.readyScript" -}}
set -eu
out=$(psql -X -tA -v ON_ERROR_STOP=1 -h /var/run/postgresql -U "$POSTGRES_USER" -d postgres -v max="${MAX_LAG_SECONDS:-30}" <<'SQL'
SELECT pg_is_in_recovery()
   AND EXISTS (SELECT 1 FROM pg_stat_wal_receiver WHERE status = 'streaming')
   AND (pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn()
        OR COALESCE(EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())), 0) < :max);
SQL
)
[ "$out" = "t" ]
{{- end }}
