"""Real-Postgres regression tests for 0026_bundle_active_set_changelog.

Unlike every sibling `test_00NN_*.py` in this directory (which mock
`alembic.op.execute` and assert emitted SQL text -- see
`test_0019_kick_app.py`'s own docstring), this migration's own
guarantees genuinely require real execution: a trigger firing on a
real write, and the primary-side `safe_seq` horizon's exact-xid
visibility math against a real, concurrently open, uncommitted second
transaction. See `pg_docker.py`'s own module docstring for the harness
this file uses and why it bootstraps a minimal schema rather than the
full 96-file legacy baseline.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import psycopg2.extensions
import pytest
from pg_docker import PgTestDatabase, alembic_cli

pytestmark = pytest.mark.usefixtures("pg_db")


@pytest.fixture
def conn(pg_db: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """One autocommit connection per test, seeded with the FK rows every test needs.

    Truncates the two tables this migration owns before each test so
    tests don't see each other's `seq` values -- everything else
    (tenants/app_catalog/app_versions rows) accumulates harmlessly
    across tests in the same session-scoped container.
    """
    connection = psycopg2.connect(pg_db.dsn)
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute("TRUNCATE bundle_active_set_changes RESTART IDENTITY")
        cur.execute("UPDATE bundle_active_set_watermark SET safe_seq = 0 WHERE id = 1")
    yield connection
    connection.close()


def _seed_app_and_tenant(
    cur: psycopg2.extensions.cursor, app_id: str, slug: str
) -> tuple[int, int]:
    """Insert one `app_catalog` row, one `tenants` row, and one `app_versions` row.

    Returns `(tenant_id, version_id)`.
    """
    cur.execute("INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING", (app_id,))
    cur.execute("INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (slug,))
    tenant_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
        "VALUES (%s, '1.0.0', 'rust', 'prebuilt') RETURNING id",
        (app_id,),
    )
    version_id = cur.fetchone()[0]
    return tenant_id, version_id


class TestTriggersFirePerOp:
    """Every op on every watched table appends exactly one change-log row."""

    def test_app_versions_insert(self, conn: psycopg2.extensions.connection) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.t.insert')")
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES ('waddles.t.insert', '1.0.0', 'rust', 'prebuilt') RETURNING id"
            )
            version_id = cur.fetchone()[0]
            cur.execute(
                "SELECT entity, entity_id, tenant_id, community_id, op "
                "FROM bundle_active_set_changes ORDER BY seq"
            )
            rows = cur.fetchall()
        assert rows == [("app_versions", str(version_id), None, None, "INSERT")]

    def test_app_active_versions_insert_update_delete(
        self, conn: psycopg2.extensions.connection
    ) -> None:
        with conn.cursor() as cur:
            tenant_id, version_id = _seed_app_and_tenant(cur, "waddles.t.aav", "aav-tenant")
            cur.execute(
                "INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id) "
                "VALUES (%s, %s, 0, %s)",
                ("waddles.t.aav", tenant_id, version_id),
            )
            cur.execute(
                "UPDATE app_active_versions SET version_id = %s "
                "WHERE app_id = %s AND tenant_id = %s",
                (version_id, "waddles.t.aav", tenant_id),
            )
            cur.execute(
                "DELETE FROM app_active_versions WHERE app_id = %s AND tenant_id = %s",
                ("waddles.t.aav", tenant_id),
            )
            cur.execute(
                "SELECT entity, entity_id, tenant_id, community_id, op "
                "FROM bundle_active_set_changes WHERE entity = 'app_active_versions' ORDER BY seq"
            )
            rows = cur.fetchall()
        expected_entity_id = f"waddles.t.aav:{tenant_id}:0"
        assert rows == [
            ("app_active_versions", expected_entity_id, tenant_id, 0, "INSERT"),
            ("app_active_versions", expected_entity_id, tenant_id, 0, "UPDATE"),
            ("app_active_versions", expected_entity_id, tenant_id, 0, "DELETE"),
        ]

    def test_ingest_sources_insert(self, conn: psycopg2.extensions.connection) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO tenants (slug) VALUES ('ingest-tenant') RETURNING id")
            tenant_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO ingest_sources (tenant_id, platform, source_id, label) "
                "VALUES (%s, 'twitch', 'chan-1', 'Chan One') RETURNING id",
                (tenant_id,),
            )
            source_row_id = cur.fetchone()[0]
            cur.execute(
                "SELECT entity, entity_id, tenant_id, op FROM bundle_active_set_changes "
                "WHERE entity = 'ingest_sources'"
            )
            rows = cur.fetchall()
        assert rows == [("ingest_sources", str(source_row_id), tenant_id, "INSERT")]


class TestSafeSeqExactVisibility:
    """The primary-side horizon math (Sec7) never counts an in-flight transaction's row."""

    def test_safe_seq_excludes_uncommitted_concurrent_transaction(
        self, pg_db: PgTestDatabase, conn: psycopg2.extensions.connection
    ) -> None:
        # Committed baseline row -- must always be visible to the horizon.
        with conn.cursor() as cur:
            cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.t.committed')")
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES ('waddles.t.committed', '1.0.0', 'rust', 'prebuilt')"
            )

        # A second, real, separately-committed connection opens a transaction,
        # writes to a watched table (firing the trigger, appending a row whose
        # xmin is this transaction's own not-yet-committed xid), and holds it
        # open -- exactly the "in-flight" case Sec7 exists to handle safely.
        in_flight = psycopg2.connect(pg_db.dsn)
        in_flight.autocommit = False
        try:
            with in_flight.cursor() as cur:
                cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.t.inflight')")
                cur.execute(
                    "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                    "VALUES ('waddles.t.inflight', '1.0.0', 'rust', 'prebuilt') RETURNING id"
                )
                in_flight_version_id = cur.fetchone()[0]
                # Deliberately not committed yet -- under read-committed
                # isolation only this same transaction can see its own
                # uncommitted insert, so its seq is read from this cursor.
                cur.execute(
                    "SELECT seq FROM bundle_active_set_changes WHERE entity_id = %s",
                    (str(in_flight_version_id),),
                )
                in_flight_seq = cur.fetchone()[0]

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint AS horizon"
                )
                horizon = cur.fetchone()[0]
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM bundle_active_set_changes "
                    "WHERE xmin::text::bigint < %s",
                    (horizon,),
                )
                safe_seq_while_in_flight = cur.fetchone()[0]

            # The in-flight row exists (the trigger already fired -- AFTER
            # triggers run inside the same still-open transaction) but its
            # own seq must never be included in the published safe_seq.
            assert safe_seq_while_in_flight < in_flight_seq

            in_flight.commit()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint AS horizon"
                )
                horizon_after = cur.fetchone()[0]
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM bundle_active_set_changes "
                    "WHERE xmin::text::bigint < %s",
                    (horizon_after,),
                )
                safe_seq_after_commit = cur.fetchone()[0]

            # Once committed, the horizon has moved past this transaction's
            # xid and safe_seq is free to include its row.
            assert safe_seq_after_commit >= in_flight_seq
        finally:
            in_flight.close()


class TestRetention:
    """Age-based prune (Sec7 default 48h) never touches rows inside the window."""

    def test_prune_deletes_only_rows_older_than_retention_window(
        self, conn: psycopg2.extensions.connection
    ) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.t.retention')")
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES ('waddles.t.retention', '1.0.0', 'rust', 'prebuilt')"
            )
            # Backdate it past the default 48h retention window.
            cur.execute(
                "UPDATE bundle_active_set_changes SET changed_at = now() - interval '72 hours' "
                "WHERE entity_id = (SELECT id::TEXT FROM app_versions WHERE app_id = 'waddles.t.retention')"
            )
            cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.t.fresh')")
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES ('waddles.t.fresh', '1.0.0', 'rust', 'prebuilt')"
            )

            cur.execute(
                "DELETE FROM bundle_active_set_changes "
                "WHERE changed_at < now() - (48.0 * interval '1 hour') RETURNING seq"
            )
            pruned = cur.fetchall()

            cur.execute(
                "SELECT entity_id FROM bundle_active_set_changes WHERE entity = 'app_versions'"
            )
            remaining_entity_ids = {row[0] for row in cur.fetchall()}

        assert len(pruned) == 1
        old_id_row = None
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_versions WHERE app_id = 'waddles.t.fresh'")
            old_id_row = cur.fetchone()
        assert str(old_id_row[0]) in remaining_entity_ids


class TestMigrationUpDown:
    """`alembic downgrade -1` / `upgrade head` against the real container, round-tripped."""

    def test_downgrade_then_upgrade_round_trips_schema(self, pg_db: PgTestDatabase) -> None:
        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('bundle_active_set_changes') IS NOT NULL, "
                    "to_regclass('bundle_active_set_watermark') IS NOT NULL"
                )
                assert tuple(cur.fetchone()) == (True, True)

        alembic_cli("downgrade", "-1", dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('bundle_active_set_changes') IS NULL, "
                    "to_regclass('bundle_active_set_watermark') IS NULL"
                )
                assert tuple(cur.fetchone()) == (True, True)
                cur.execute(
                    "SELECT COUNT(*) FROM pg_trigger WHERE tgname = 'trg_bundle_active_set_log'"
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT COUNT(*) FROM pg_proc WHERE proname = 'fn_bundle_active_set_log_change'"
                )
                assert cur.fetchone()[0] == 0

        alembic_cli("upgrade", "head", dsn=pg_db.dsn)

        with psycopg2.connect(pg_db.dsn) as check_conn:
            check_conn.autocommit = True
            with check_conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('bundle_active_set_changes') IS NOT NULL, "
                    "to_regclass('bundle_active_set_watermark') IS NOT NULL"
                )
                assert tuple(cur.fetchone()) == (True, True)
                cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT COUNT(*) FROM pg_trigger WHERE tgname = 'trg_bundle_active_set_log'"
                )
                assert cur.fetchone()[0] == 5
