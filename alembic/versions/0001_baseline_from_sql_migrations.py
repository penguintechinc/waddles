"""Baseline migration from legacy SQL files.

Handles two scenarios:
1. Existing DB (beta): schema_migrations table exists with entries → no-op
2. Fresh DB: Executes all legacy SQL migration files in order

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-02-16
"""
import os
import glob
from alembic import op
import sqlalchemy as sa

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None

import re


def _prepare_sql(sql_content):
    """Prepare raw SQL file content for execution via psycopg2.

    Strips explicit BEGIN/COMMIT (alembic manages the transaction)
    and removes psql meta-commands (\\-prefixed lines).
    The result is passed as-is to cursor.execute() which handles
    semicolons, $$ blocks, comments, and string literals natively.
    """
    # Remove psql meta-commands (e.g. \set, \echo)
    lines = sql_content.split('\n')
    lines = [l for l in lines if not l.strip().startswith('\\')]
    content = '\n'.join(lines)
    # Strip standalone BEGIN/COMMIT statements (alembic wraps in txn).
    # Match only bare BEGIN; or COMMIT; on their own lines.
    content = re.sub(r'(?m)^\s*BEGIN\s*;\s*$', '', content)
    content = re.sub(r'(?m)^\s*COMMIT\s*;\s*$', '', content)
    return content.strip()


# Path where legacy SQL files are mounted in the migration container
LEGACY_SQL_DIR = os.environ.get(
    'LEGACY_SQL_DIR',
    os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'postgres', 'migrations')
)


def upgrade() -> None:
    conn = op.get_bind()

    # SECURITY (CWE-798): bridge INITIAL_ADMIN_EMAIL / INITIAL_ADMIN_PASSWORD
    # (if set) into session-scoped custom GUCs. Migration 081 reads these via
    # current_setting() to bootstrap the first super-admin -- unset means no
    # admin account is created (fail closed). Parameterized so the values
    # never touch SQL text directly.
    conn.execute(
        sa.text("SELECT set_config('waddlebot.initial_admin_email', :v, false)"),
        {"v": os.environ.get("INITIAL_ADMIN_EMAIL", "")},
    )
    conn.execute(
        sa.text("SELECT set_config('waddlebot.initial_admin_password', :v, false)"),
        {"v": os.environ.get("INITIAL_ADMIN_PASSWORD", "")},
    )

    # Check if this DB was already migrated by the legacy psql runner
    result = conn.execute(sa.text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM information_schema.tables "
        "  WHERE table_schema = 'public' AND table_name = 'schema_migrations'"
        ")"
    ))
    has_schema_migrations = result.scalar()

    if has_schema_migrations:
        count = conn.execute(sa.text(
            "SELECT COUNT(*) FROM schema_migrations"
        )).scalar()
        if count > 0:
            print(f"[baseline] Legacy schema_migrations has {count} entries — DB already migrated, skipping.")
            return

    # Fresh DB: run all legacy SQL migration files in sorted order
    sql_dir = os.path.abspath(LEGACY_SQL_DIR)
    sql_files = sorted(glob.glob(os.path.join(sql_dir, '*.sql')))

    if not sql_files:
        print(f"[baseline] WARNING: No SQL files found in {sql_dir}")
        print("[baseline] If this is a fresh deployment, tables will be created by autogenerate migrations.")
        return

    print(f"[baseline] Fresh DB detected. Applying {len(sql_files)} legacy SQL migrations...")

    # Create schema_migrations table for tracking
    conn.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version VARCHAR(255) PRIMARY KEY,"
        "  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        "  description TEXT"
        ")"
    ))

    # Multi-pass apply (fresh DB only -- an already-migrated DB returned above,
    # so this never re-orders anything on an incremental upgrade). Pure
    # filename-sorted order cannot express cross-file dependencies: duplicate
    # numeric prefixes (e.g. 031_rls_policies sorts before 031_scoped_database_
    # users, which creates the roles it references) are real forward
    # references. A file that fails is rolled back to its savepoint and
    # deferred; passes repeat until one makes no progress. Files that never
    # apply are a hard failure -- never a silent skip, which is how a fresh
    # replay used to ship an incomplete schema.
    pending = list(sql_files)
    pass_no = 0
    last_errors = {}
    while pending:
        pass_no += 1
        deferred = []
        progressed = False
        for sql_file in pending:
            fname = os.path.basename(sql_file)
            version = fname.replace('.sql', '')

            already = conn.execute(sa.text(
                "SELECT 1 FROM schema_migrations WHERE version = :v"
            ), {"v": version}).fetchone()
            if already:
                print(f"  Skipping (already applied): {fname}")
                progressed = True
                continue

            with open(sql_file, 'r') as f:
                sql_content = f.read()

            # psycopg2 native multi-statement execution handles $$ blocks,
            # /* */ comments, and string literals.
            prepared = _prepare_sql(sql_content)
            if prepared:
                raw_conn = conn.connection.dbapi_connection
                cursor = raw_conn.cursor()
                try:
                    cursor.execute("SAVEPOINT sp_migration")
                    cursor.execute(prepared)
                    cursor.execute("RELEASE SAVEPOINT sp_migration")
                except Exception as e:
                    cursor.execute("ROLLBACK TO SAVEPOINT sp_migration")
                    cursor.execute("RELEASE SAVEPOINT sp_migration")
                    cursor.close()
                    last_errors[fname] = str(e).strip().splitlines()[0]
                    print(f"  Deferred (pass {pass_no}): {fname} -- {last_errors[fname]}")
                    deferred.append(sql_file)
                    continue
                cursor.close()

            print(f"  Applied: {fname}")
            conn.execute(sa.text(
                "INSERT INTO schema_migrations (version) VALUES (:v) ON CONFLICT DO NOTHING"
            ), {"v": version})
            progressed = True

        if deferred and not progressed:
            details = "; ".join(
                f"{os.path.basename(f)}: {last_errors[os.path.basename(f)]}"
                for f in deferred
            )
            raise RuntimeError(
                f"[baseline] {len(deferred)} legacy SQL migration(s) cannot be "
                f"applied on a fresh database: {details}"
            )
        pending = deferred

    print("[baseline] Legacy SQL migrations applied.")


def downgrade() -> None:
    # Downgrade is intentionally not supported for the baseline.
    # Dropping all tables would destroy the database.
    raise RuntimeError(
        "Cannot downgrade baseline migration. "
        "To reset, drop the database and re-run migrations."
    )
