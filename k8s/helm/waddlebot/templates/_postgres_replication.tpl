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

{{/*
Replica initContainer (runs on EVERY pod start): (1) writes a 0600 passfile from the Secret into an emptyDir, (2) pg_basebackup
from the primary over verify-full TLS ONLY when PGDATA has no cluster, (3) rewrites primary_conninfo in postgresql.auto.conf so
it carries NO password (passfile= instead) -- also cleaning replicas bootstrapped by an earlier revision -- and fails loud if a
password= survives.
*/}}
{{- define "waddlebot.postgres.basebackupScript" -}}
set -eu
ord="${HOSTNAME##*-}"
umask 077
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'; }
printf '%s:%s:replication:%s:%s\n' "$PRIMARY_HOST" "$PRIMARY_PORT" "$REPL_USER" "$(esc "$REPL_PASSWORD")" > "$PGPASSFILE"
chmod 600 "$PGPASSFILE"
unset REPL_PASSWORD
if [ -s "$PGDATA/PG_VERSION" ]; then
  echo "basebackup: PGDATA already initialised, skipping"
else
  mkdir -p "$PGDATA"
  chmod 700 "$PGDATA"
  i=0
  seeded=0
  while [ "$i" -lt 60 ]; do
    # No PG_VERSION => any content is a half-finished earlier attempt of THIS init container; safe to clear.
    find "$PGDATA" -mindepth 1 -delete
    if pg_basebackup -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$REPL_USER" -w \
         -D "$PGDATA" -X stream -c fast -R -S "replica_${ord}"; then
      seeded=1
      break
    fi
    i=$((i + 1))
    echo "basebackup: attempt $i failed (primary/slot not ready?), retrying in 5s" >&2
    sleep 5
  done
  if [ "$seeded" -ne 1 ]; then
    echo "basebackup: gave up after 60 attempts" >&2
    exit 1
  fi
  chmod 700 "$PGDATA"
  echo "basebackup: seeded from $PRIMARY_HOST with slot replica_${ord}"
fi
conf="$PGDATA/postgresql.auto.conf"
[ -f "$conf" ] || { echo "basebackup: $conf missing" >&2; exit 1; }
{ grep -v '^[[:space:]]*primary_conninfo' "$conf" || [ "$?" -eq 1 ]; } > "$conf.new"
printf "primary_conninfo = 'host=%s port=%s user=%s sslmode=verify-full sslrootcert=%s passfile=%s application_name=replica_%s'\n" \
  "$PRIMARY_HOST" "$PRIMARY_PORT" "$REPL_USER" "$PGSSLROOTCERT" "$PGPASSFILE" "$ord" >> "$conf.new"
mv "$conf.new" "$conf"
chmod 600 "$conf"
if grep -Eiq 'password[[:space:]]*=' "$conf"; then
  echo "basebackup: FAILED - a password= survived in postgresql.auto.conf" >&2
  exit 1
fi
echo "basebackup: primary_conninfo uses passfile (no password stored)"
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

{{/*
Pre-upgrade hand-over: scale the LEGACY Deployment/postgres to 0 and wait (bounded) for its pods to terminate.
No-op when the Deployment is absent. Legacy pods carry no `app.kubernetes.io/role` label; the StatefulSet pods do,
so the selector can never match (or wait on) the new topology.
*/}}
{{- define "waddlebot.postgres.handoverScript" -}}
set -euo pipefail
ns="${NAMESPACE:?}"
limit="${TIMEOUT_SECONDS:-300}"
found="$(kubectl -n "$ns" get deployment postgres --ignore-not-found -o name)"
if [ -z "$found" ]; then
  echo "postgres-handover: no legacy Deployment/postgres in $ns; nothing to do"
  exit 0
fi
echo "postgres-handover: scaling legacy Deployment/postgres to 0 (PVC untouched)"
kubectl -n "$ns" scale deployment/postgres --replicas=0
start=$(date +%s)
while :; do
  left="$(kubectl -n "$ns" get pods -l 'app.kubernetes.io/name=postgres,!app.kubernetes.io/role' -o name | wc -l)"
  if [ "$left" -eq 0 ]; then
    echo "postgres-handover: legacy postgres pods are gone"
    exit 0
  fi
  if [ $(( $(date +%s) - start )) -ge "$limit" ]; then
    echo "postgres-handover: FAILED - $left legacy postgres pod(s) still present after ${limit}s; aborting the upgrade (two postmasters must never share PGDATA)" >&2
    exit 1
  fi
  sleep 3
done
{{- end }}
