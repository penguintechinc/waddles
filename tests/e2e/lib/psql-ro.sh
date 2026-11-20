#!/usr/bin/env bash
# Read-only psql against the alpha primary via kubectl exec.
# Usage: psql-ro.sh "<SQL>" [extra psql flags]
# Forces default_transaction_read_only=on so a sweep can never mutate data.
set -euo pipefail
SQL="${1:?usage: psql-ro.sh <sql> [psql flags]}"
shift || true
KCTX="${KUBE_CONTEXT:-dal2alpha}"
NS="${KUBE_NAMESPACE:-waddlebot}"
exec kubectl --context "$KCTX" -n "$NS" exec postgres-0 -c postgres -- \
  env PGOPTIONS="-c default_transaction_read_only=on" \
  psql -U "${PGUSER_SWEEP:-waddlebot}" -d "${PGDB_SWEEP:-waddlebot}" -v ON_ERROR_STOP=1 -At "$@" -c "$SQL"
