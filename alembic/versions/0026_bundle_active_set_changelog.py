"""bundle_active_set_changes + bundle_active_set_watermark -- xid-exact change-log (data-plane scale design rev4 Sec7).

Increment 1 of `docs/superpowers/specs/2026-09-28-dataplane-scale-design.md`
(control-plane side only; the Rust data-plane's watermark-consuming
poll loop is a parallel, separately-landed change). Migration path
step 1 (Sec8): "Change-log table + trigger, xid-safe polling -- schema
-only + read-logic, no flag."

**Why exact-xid, not a time margin.** The alpha/rev-2 approach polled
`read_active_set()` on a time-margin heuristic -- safe only as long as
no writer transaction stays open longer than the margin. Rev4 replaces
that with an exact visibility computation: every write to a watched
table appends a row to `bundle_active_set_changes` (this migration's
triggers, below); a small periodic job running **on the primary**
(never a replica -- Sec7 rev3's own rejected approach: a hot standby
learns about a just-started primary transaction only via periodically
-emitted `xl_running_xacts` WAL records, so a replica-computed horizon
can *underestimate* what's still in flight and reintroduce the gap
this mechanism exists to close) computes
`pg_snapshot_xmin(pg_current_snapshot())` and publishes the highest
`seq` whose row is provably no longer part of any in-flight
transaction into the single-row `bundle_active_set_watermark` table.
Every consumer (replica) then does a trivial
`SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1` and
polls `seq > last_seen_seq AND seq <= safe_seq` -- correctness no
longer depends on replication lag at all. The primary-side job itself
(`hub_api/services/bundle_active_set_watermark_job.py`) is a parallel
hub-api change, not part of this migration.

**`writer_xid xid8`, not the implicit `xmin` system column (post-review
correctness fix).** The design's own Sec7 SQL reads the implicit
`xmin` system column directly, but `xmin` is a 32-bit, wraparound
-relative type -- comparing it against `pg_snapshot_xmin()`'s `xid8`
result (64-bit, epoch-extended, wraparound-safe) via `xmin::text::bigint`
is unsound across a `VACUUM FREEZE`/wraparound boundary: a frozen tuple's
`xmin` reads back as `FrozenTransactionId` (2), which is *always* less
than any real horizon regardless of how old or new the row actually is,
and a wrapped-but-unfrozen `xmin` compares incorrectly once the 32-bit
counter has cycled past the horizon's own epoch. `writer_xid xid8`,
populated by the trigger via `pg_current_xact_id()` (itself the same
64-bit epoch-extended id `pg_snapshot_xmin()` returns), compares
natively `xid8 < xid8` -- exact at any table age, no wraparound case.

**One generic trigger function, one row per watched table.** All five
tables (`app_active_versions`, `app_install_approvals`,
`app_source_bindings`, `app_versions`, `ingest_sources` -- exactly the
Sec1/Sec2 "active set" inputs; `tenant_app_availability` does not
exist in this schema) share `fn_bundle_active_set_log_change()`,
parameterized per-trigger via `TG_ARGV` with that table's own natural
-key column names, so `entity_id` is a stable, table-appropriate key
without a bespoke function per table. `tenant_id`/`community_id` are
read out of the changed row's own `to_jsonb()` when present (absent
-> `NULL`, e.g. `app_versions`, which is not tenant-scoped) --
consumers filter by `entity` first, so a `NULL` tenant/community on a
platform-level row is expected, not a data gap. Triggers are `AFTER`
(never block the write) and do exactly one cheap `INSERT`, in the same
transaction as the write they log -- same transaction means the
change-log row's own `writer_xid` (`pg_current_xact_id()`) always
matches the transaction that produced the write it describes, which is
exactly what the safe-horizon computation above depends on.

**Row-level (`FOR EACH ROW`), not statement-level, and why that's fine
here.** A row-level trigger has real per-row overhead that would matter
on a hot data-plane write path -- these five tables are not that: they
are hub-api-owned control-plane/config tables (installs, approvals,
source bindings, published digests, ingest source registration), written
at human/admin-action or publish-time cadence, not per-event. A
statement-level trigger with a transition table would save nothing
meaningful here and would complicate `entity_id` construction (still
needs a per-row natural key) for no real benefit at this write volume.

**Bounded, contiguous-prefix `safe_seq` computation (post-review
correctness/perf fix).** A single unconditional `MAX(seq) WHERE
writer_xid < horizon` -- as the design's own Sec7 SQL literally shows --
has two problems: (1) it re-scans the whole table every tick, growing
with the 48h retention window; (2) more subtly, `seq` allocation order
and transaction-xid order are *not* guaranteed to coincide when a
transaction's xid was assigned at an *earlier* statement than its
watched-table write (a realistic pattern here -- e.g. `bundle_approval
_service._write_approval_and_activate()` touches multiple watched
tables, and other transactions may write an unwatched table first). A
transaction with an older xid can therefore claim a *later* `seq` than
a newer-xid transaction that already committed, and an unconditional
`MAX()` can publish that later `seq` as "safe" while the older-xid
transaction's own, lower-`seq` row is still in flight -- a permanent
gap once a consumer advances `last_seen_seq` past it (`seq > last_seen
AND seq <= safe_seq` would include the missing row's `seq`, but the row
itself doesn't exist yet, so the consumer simply never sees it once it
finally commits). Fixed in the primary-side job (not this migration) by
scanning only `seq > current_safe_seq` (a bounded, `LIMIT`-capped PK
-range scan) and advancing `safe_seq` only across the *contiguous safe
prefix* of that batch, stopping at the first not-yet-safe row rather
than jumping past it -- see `bundle_active_set_watermark_job.py`'s own
docstring for the exact query.

**Retention.** `bundle_active_set_changes` is pruned by
`hub_api/services/bundle_active_set_watermark_job.py` on a slower
cadence than the safe_seq computation itself (default 48h, Sec7
"unchanged from rev 2" -- safe because a replica down longer than that
is already in full-reconcile territory); `idx_bundle_active_set_changes_changed_at`
below exists so that prune `DELETE` is an index range scan, not a
sequential scan of the whole table. `bundle_active_set_watermark.
min_retained_seq` (updated by the same prune pass) is the lowest `seq`
still present after pruning, so a consumer whose own `last_seen_seq` has
fallen below it can detect "I'm stale past retention, do a full
reconcile" instead of silently polling a range that skips pruned rows.

**Writer-side statement/idle timeouts (Sec7 "Bounding staleness at the
source").** A long-running or idle-in-transaction writer on any
watched table holds `safe_seq` back indefinitely (the primary-side
horizon can never advance past a still-open transaction's xid). Set
via `ALTER ROLE ... SET ...` on the two NOLOGIN RBAC-matrix roles that
actually hold write privilege on every watched table today
(`config/postgres/rbac-matrix.yaml`: `hub_api`, and `waddles_publisher`
for `app_versions` specifically) -- guarded by the same `pg_roles`
`IF EXISTS` idiom migration 0025 already established, since this
migration may run before or after the roles exist depending on
environment. Deliberately **not** applied to `migration_runner`: that
role's connections are one-off Alembic-runner Jobs that may
legitimately run a long DDL statement (e.g. backfills), and are not an
ongoing traffic-serving writer of the kind this timeout exists to
bound; a stuck migration is its own, separately-monitored event, not a
candidate for a blanket 30s cap. **Where this actually takes effect:**
`ALTER ROLE <name> SET ...` applies to a session whose *session
authorization* is that exact role. `hub_api`/`waddles_publisher` are
currently NOLOGIN privilege-group roles (created by migration
0020/0022's `render_create_roles_sql()`); no migration in this repo yet
wires a LOGIN credential to either name (that wiring is a Helm
-secret/deployment concern, tracked separately, matching the same
not-yet-wired gap every prior migration's grants to these same role
names already carries). The setting is provisioned now so it is
already in place the moment that wiring lands, exactly as this
migration's sibling grants already do for table privileges.

Revision ID: 0026_bundle_active_set_changelog
Revises: 0025_app_source_bindings
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision = "0026_bundle_active_set_changelog"
down_revision = "0025_app_source_bindings"
branch_labels = None
depends_on = None

#: (table, natural-key columns) -- passed to the shared trigger function as
#: TG_ARGV so entity_id is built from each table's own real key, not a
#: fabricated surrogate. Order matches the design doc's Sec1/Sec2 active-set
#: table list; `tenant_app_availability` is skipped (not present in this
#: schema, per the migration docstring above).
_WATCHED_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("app_active_versions", ("app_id", "tenant_id", "community_id")),
    ("app_install_approvals", ("id",)),
    ("app_source_bindings", ("tenant_id", "community_id", "app_id", "platform", "source_id")),
    ("app_versions", ("id",)),
    ("ingest_sources", ("id",)),
)

#: Guarded per migration 0025's own convention: this migration may land
#: before or after the Rust-side role-provisioning migration in a given
#: environment, so the grant must be a safe no-op either way.
_BUNDLE_READER_ROLE = "waddles_bundle_reader"

#: The RBAC-matrix roles with write privilege on at least one watched
#: table today (config/postgres/rbac-matrix.yaml) -- see the module
#: docstring's "Writer-side statement/idle timeouts" section for why
#: `migration_runner` is deliberately excluded.
_WRITER_ROLES_FOR_TIMEOUTS = ("hub_api", "waddles_publisher")

_STATEMENT_TIMEOUT = "30s"
_IDLE_IN_TRANSACTION_TIMEOUT = "10s"


def _bundle_reader_grant_sql(table: str) -> str:
    """Guarded `GRANT SELECT ON {table} TO waddles_bundle_reader` -- role must exist."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}') THEN\n"
        f"    GRANT SELECT ON {table} TO {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _bundle_reader_revoke_sql(table: str) -> str:
    """Guarded `REVOKE SELECT ON {table} FROM waddles_bundle_reader` -- the `upgrade()` grant's inverse."""
    return (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role/table names are fixed literals, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_BUNDLE_READER_ROLE}') THEN\n"
        f"    REVOKE SELECT ON {table} FROM {_BUNDLE_READER_ROLE};\n"
        f"  END IF;\n"
        f"END $$;"
    )


def _timeout_sql(role: str) -> tuple[str, str]:
    """Guarded `ALTER ROLE ... SET statement_timeout/idle_in_transaction_session_timeout` -- role must exist."""
    statement = (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    EXECUTE 'ALTER ROLE {role} SET statement_timeout = ''{_STATEMENT_TIMEOUT}''';\n"
        f"  END IF;\n"
        f"END $$;"
    )
    idle = (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    EXECUTE 'ALTER ROLE {role} SET idle_in_transaction_session_timeout = "
        f"''{_IDLE_IN_TRANSACTION_TIMEOUT}''';\n"
        f"  END IF;\n"
        f"END $$;"
    )
    return statement, idle


def _reset_timeout_sql(role: str) -> tuple[str, str]:
    """`ALTER ROLE ... RESET ...` -- the `upgrade()` timeout's inverse."""
    statement = (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    EXECUTE 'ALTER ROLE {role} RESET statement_timeout';\n"
        f"  END IF;\n"
        f"END $$;"
    )
    idle = (
        f"DO $$ BEGIN\n"  # noqa: S608  # nosec B608 -- role name is a fixed literal, never user input
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    EXECUTE 'ALTER ROLE {role} RESET idle_in_transaction_session_timeout';\n"
        f"  END IF;\n"
        f"END $$;"
    )
    return statement, idle


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_active_set_changes (
            seq BIGSERIAL PRIMARY KEY,
            entity TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            tenant_id INTEGER,
            community_id INTEGER,
            op TEXT NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
            writer_xid xid8 NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_active_set_changes_changed_at "
        "ON bundle_active_set_changes (changed_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_active_set_changes_entity "
        "ON bundle_active_set_changes (entity, seq)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_bundle_active_set_changes_writer_xid "
        "ON bundle_active_set_changes (writer_xid)"
    )
    op.execute(
        "COMMENT ON TABLE bundle_active_set_changes IS "
        "'Append-only change-log of every write to an active-set input table "
        "(data-plane scale design Sec7) -- writer_xid (xid8, set via "
        "pg_current_xact_id()) is the exact-visibility key the primary-side "
        "safe_seq job compares against pg_snapshot_xmin(); never the "
        "32-bit implicit xmin system column (unsound across wraparound/freeze)'"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bundle_active_set_watermark (
            id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            safe_seq BIGINT NOT NULL DEFAULT 0,
            min_retained_seq BIGINT NOT NULL DEFAULT 0,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "INSERT INTO bundle_active_set_watermark (id, safe_seq, min_retained_seq) "
        "VALUES (1, 0, 0) ON CONFLICT (id) DO NOTHING"
    )
    op.execute(
        "COMMENT ON TABLE bundle_active_set_watermark IS "
        "'Single-row published safe_seq (data-plane scale design Sec7) -- "
        "written only by the primary-side job "
        "(hub_api/services/bundle_active_set_watermark_job.py), read-only "
        "everywhere else. min_retained_seq is the lowest surviving seq after "
        "the last retention prune, so a consumer can detect falling behind "
        "retention'"
    )

    # One shared, cheap AFTER trigger function. SECURITY DEFINER so it can
    # always write bundle_active_set_changes regardless of which watched
    # table's writer role fired it (same convention as
    # fn_app_versions_audit(), migration 0022) -- kept rather than dropped
    # because svc-owned writer roles (see the RBAC matrix) are granted
    # write access to their own watched tables but not necessarily to
    # bundle_active_set_changes itself, and this function is the only
    # writer that table needs. `SET search_path = pg_catalog, pg_temp`
    # (post-review hardening) closes the SECURITY DEFINER search-path
    # hijack vector (a malicious same-named function/type earlier in an
    # attacker-controlled search_path) -- every reference below is
    # schema-qualified (`public.bundle_active_set_changes`) so the
    # function still resolves correctly with search_path locked down.
    # TG_ARGV carries the calling table's own natural-key column names
    # (see _WATCHED_TABLES above).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_bundle_active_set_log_change() RETURNS trigger AS $$
        DECLARE
            v_row JSONB;
            v_entity_id TEXT := '';
            v_tenant_id INTEGER;
            v_community_id INTEGER;
            i INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                v_row := to_jsonb(OLD);
            ELSE
                v_row := to_jsonb(NEW);
            END IF;

            FOR i IN 0 .. TG_NARGS - 1 LOOP
                IF i > 0 THEN
                    v_entity_id := v_entity_id || ':';
                END IF;
                v_entity_id := v_entity_id || COALESCE(v_row ->> TG_ARGV[i], '');
            END LOOP;

            v_tenant_id := NULLIF(v_row ->> 'tenant_id', '')::INTEGER;
            v_community_id := NULLIF(v_row ->> 'community_id', '')::INTEGER;

            INSERT INTO public.bundle_active_set_changes
                (entity, entity_id, tenant_id, community_id, op, writer_xid)
            VALUES
                (TG_TABLE_NAME::TEXT, v_entity_id, v_tenant_id, v_community_id, TG_OP,
                 pg_current_xact_id());

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        """
    )

    for table, key_columns in _WATCHED_TABLES:
        args = ", ".join(f"'{col}'" for col in key_columns)
        op.execute(f"DROP TRIGGER IF EXISTS trg_bundle_active_set_log ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER trg_bundle_active_set_log
                AFTER INSERT OR UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION fn_bundle_active_set_log_change({args})
            """
        )

    # Only the two tables this migration owns -- the five watched tables
    # keep whichever reader grants their own migrations already decided
    # (e.g. app_source_bindings, migration 0025); this migration must not
    # re-decide access to tables it doesn't own.
    op.execute(_bundle_reader_grant_sql("bundle_active_set_changes"))
    op.execute(_bundle_reader_grant_sql("bundle_active_set_watermark"))

    for role in _WRITER_ROLES_FOR_TIMEOUTS:
        statement_sql, idle_sql = _timeout_sql(role)
        op.execute(statement_sql)
        op.execute(idle_sql)


def downgrade() -> None:
    for role in reversed(_WRITER_ROLES_FOR_TIMEOUTS):
        statement_sql, idle_sql = _reset_timeout_sql(role)
        op.execute(idle_sql)
        op.execute(statement_sql)

    op.execute(_bundle_reader_revoke_sql("bundle_active_set_watermark"))
    op.execute(_bundle_reader_revoke_sql("bundle_active_set_changes"))

    for table, _ in _WATCHED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_bundle_active_set_log ON {table}")
    op.execute("DROP FUNCTION IF EXISTS fn_bundle_active_set_log_change()")

    op.execute("DROP TABLE IF EXISTS bundle_active_set_watermark")
    op.execute("DROP TABLE IF EXISTS bundle_active_set_changes")
