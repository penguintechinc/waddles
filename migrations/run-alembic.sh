#!/bin/sh
# WaddleBot Database Migration Runner (Alembic)
# Waits for DB readiness, then runs Alembic upgrade head.
set -e

echo "=== WaddleBot DB Migrations (Alembic) ==="

# ── Resolve DATABASE_URL ─────────────────────────────────────────────────────
if [ -z "${DATABASE_URL:-}" ]; then
    DB_HOST="${DB_HOST:-${DATABASE_HOST:-infra-postgres}}"
    DB_PORT="${DB_PORT:-${DATABASE_PORT:-5432}}"
    DB_NAME="${DB_NAME:-${DATABASE_NAME:-waddlebot}}"
    DB_USER="${DB_USER:-${DATABASE_USER:-waddlebot}}"
    DB_PASS="${DB_PASS:-${DATABASE_PASSWORD:-${POSTGRES_PASSWORD:-changeme}}}"
    DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
    export DATABASE_URL
fi

python3 - <<'PY'
import os
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
print(
    "Database: "
    f"driver={url.drivername} host={url.host or '<local>'} "
    f"port={url.port or '<default>'} database={url.database or '<default>'}"
)
PY

# ── Wait for PostgreSQL ──────────────────────────────────────────────────────
echo "Waiting for database..."
i=0
while ! python3 -c "
import os, sqlalchemy, sys
try:
    url = os.environ['DATABASE_URL'].replace('postgresql://', 'postgresql+psycopg2://', 1)
    e = sqlalchemy.create_engine(url)
    with e.connect() as c:
        c.execute(sqlalchemy.text('SELECT 1'))
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; do
    i=$((i+1))
    if [ "$i" -ge 30 ]; then
        echo "ERROR: Database not ready after 60s"
        exit 1
    fi
    echo "  Not ready, retrying in 2s (attempt ${i}/30)..."
    sleep 2
done
echo "Database ready."

# Missing tables are expected on a fresh or partially migrated database. Report
# them before migration so persisted-volume repairs are visible in the logs.
python3 - <<'PY'
import os
import sqlalchemy

required = {"commands", "platform_integrations"}
url = os.environ["DATABASE_URL"].replace(
    "postgresql://", "postgresql+psycopg2://", 1
)
engine = sqlalchemy.create_engine(url)
with engine.connect() as connection:
    present = set(connection.execute(sqlalchemy.text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('commands', 'platform_integrations')"
    )).scalars())
missing = sorted(required - present)
print("Pre-migration schema: " + (
    f"missing {', '.join(missing)}" if missing else "required tables present"
))
PY

# ── Run Alembic ──────────────────────────────────────────────────────────────
echo "Running Alembic upgrade head..."
cd /app
alembic upgrade head

# A zero exit from Alembic is insufficient if the minimum schema is incomplete.
python3 - <<'PY'
import os
import sqlalchemy

required = {"commands", "platform_integrations"}
url = os.environ["DATABASE_URL"].replace(
    "postgresql://", "postgresql+psycopg2://", 1
)
engine = sqlalchemy.create_engine(url)
with engine.connect() as connection:
    present = set(connection.execute(sqlalchemy.text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('commands', 'platform_integrations')"
    )).scalars())
missing = sorted(required - present)
if missing:
    raise SystemExit(
        "ERROR: migration completed with required tables missing: "
        + ", ".join(missing)
    )
print("Post-migration schema: required tables present")
PY

# ── Reconcile waddles_bundle_reader role password ───────────────────────────
# fix/no-empty-kept-secrets -- 0032_bundle_reader_role.py only sets this role's
# password the ONE TIME it creates the role; a DB already at/past that revision
# never re-runs it. The chart's lookup-KEEP bug (now fixed) could otherwise mint
# a brand-new DB_READER_PASSWORD on some later `helm upgrade` with the role's
# actual Postgres password never updated to match. Reconcile unconditionally on
# every migration-job run, after `alembic upgrade head`, via the shared
# scripts/db/bundle_reader_role.py module (same GRANT list 0032 provisions --
# never a second, independently-maintained copy). FAIL LOUD on an empty
# password: never silently leave/set an empty password on this role.
echo "Reconciling waddles_bundle_reader role password..."
if [ -z "${DB_READER_PASSWORD:-}" ]; then
    echo "ERROR: DB_READER_PASSWORD is empty -- refusing to leave/set an empty password on waddles_bundle_reader (this role gates the DB-driven multi-app active-bundle path)." >&2
    exit 1
fi
python3 - <<'PY'
import os
import sys

import sqlalchemy as sa

sys.path.insert(0, "/app/scripts/db")
import bundle_reader_role  # noqa: E402

url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg2://", 1)
engine = sa.create_engine(url)
with engine.begin() as conn:
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": bundle_reader_role.ROLE},
    ).scalar()
    if not exists:
        print(
            f"Role {bundle_reader_role.ROLE} does not exist yet "
            "(0032_bundle_reader_role has not run on this database) -- skipping reconcile."
        )
        sys.exit(0)
    bundle_reader_role.reconcile_role_and_grants(
        conn, os.environ[bundle_reader_role.PASSWORD_ENV]
    )
print(f"Reconciled {bundle_reader_role.ROLE} password and grants.")
PY

# ── Reconcile per-service LOGIN roles (H-1 / H-3) ────────────────────────────
# config/postgres/service-roles.yaml is the single catalog of the least-privilege
# role each chart workload connects as (never the database owner/superuser this
# Job runs as). 0049_per_service_db_roles creates them once; this step re-asserts
# the EXACT catalog on every migrate Job run so (a) a rotated password in the
# Secret reaches Postgres, (b) tables added by later migrations are granted to the
# roles that need them, and (c) out-of-band grant drift is repaired. --strict makes
# a catalog table that does not exist FAIL the Job (never a silent skip); a missing
# / weak WADDLES_DB_SERVICE_ROLE_PASSWORDS fails it too. Passwords are read from the
# environment only -- never an argv, never echoed.
echo "Reconciling per-service DB roles..."
python3 /app/scripts/db/service_roles.py reconcile --strict

echo "=== All migrations complete ==="
