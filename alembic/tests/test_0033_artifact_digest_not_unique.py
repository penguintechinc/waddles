"""Real-Postgres regression tests for 0033_artifact_digest_not_unique.

# regression: global artifact_digest UNIQUE blocked manifest-only release (alpha 2026-10-02)

The old `UNIQUE (artifact_digest)` constraint (migration 0022) rejected a
second `app_versions` row that legitimately reuses byte-identical wasm --
a manifest-only release bump, or two different apps independently
producing the same bytes. This can only be proven against a real
Postgres: whether an INSERT succeeds or raises `UniqueViolation` is
exactly the behavior under test, not something mocked `op.execute` SQL
text can assert (see `pg_docker.py`'s own module docstring).
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, alembic_cli, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_SAME_DIGEST = "sha256:" + "a" * 64


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _insert_version(
    cur: psycopg2.extensions.cursor, *, app_id: str, version: str, digest: str | None
) -> None:
    cur.execute(
        "INSERT INTO app_versions (app_id, version, artifact_digest, language, artifact_kind) "
        "VALUES (%s, %s, %s, 'rust', 'prebuilt')",
        (app_id, version, digest),
    )


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every non-round-trip test below."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0033-artifact-digest") as db:
        yield db


@requires_docker
class TestDigestNoLongerGloballyUnique:
    """Content-addressed storage allows the same digest under several `(app_id, version)` rows."""

    def test_same_digest_two_versions_same_app_succeeds(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    ("waddles.core.ping",),
                )
                # 1.0.2 -> 1.0.3 manifest-only bump, byte-identical wasm.
                _insert_version(
                    cur, app_id="waddles.core.ping", version="1.0.2", digest=_SAME_DIGEST
                )
                _insert_version(
                    cur, app_id="waddles.core.ping", version="1.0.3", digest=_SAME_DIGEST
                )
                cur.execute(
                    "SELECT version FROM app_versions "
                    "WHERE app_id = %s AND artifact_digest = %s ORDER BY version",
                    ("waddles.core.ping", _SAME_DIGEST),
                )
                assert [r[0] for r in cur.fetchall()] == ["1.0.2", "1.0.3"]
        finally:
            conn.close()

    def test_same_digest_two_different_apps_succeeds(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    ("waddles.core.alpha",),
                )
                cur.execute(
                    "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    ("waddles.core.beta",),
                )
                _insert_version(
                    cur, app_id="waddles.core.alpha", version="1.0.0", digest=_SAME_DIGEST
                )
                _insert_version(
                    cur, app_id="waddles.core.beta", version="1.0.0", digest=_SAME_DIGEST
                )
                cur.execute(
                    "SELECT app_id FROM app_versions "
                    "WHERE artifact_digest = %s AND app_id IN %s ORDER BY app_id",
                    (_SAME_DIGEST, ("waddles.core.alpha", "waddles.core.beta")),
                )
                assert [r[0] for r in cur.fetchall()] == [
                    "waddles.core.alpha",
                    "waddles.core.beta",
                ]
        finally:
            conn.close()

    def test_duplicate_app_id_version_still_rejected(self, pg_db: PgTestDatabase) -> None:
        """`UNIQUE(app_id, version)` is untouched -- a true duplicate version row still 409s."""
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                    ("waddles.core.dup",),
                )
                _insert_version(
                    cur, app_id="waddles.core.dup", version="1.0.0", digest=_SAME_DIGEST
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                _insert_version(
                    cur,
                    app_id="waddles.core.dup",
                    version="1.0.0",
                    digest="sha256:" + "b" * 64,
                )
        finally:
            conn.close()

    def test_digest_index_exists_and_is_not_unique(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'app_versions' AND indexname = %s",
                    ("idx_app_versions_artifact_digest",),
                )
                row = cur.fetchone()
                assert row is not None, "idx_app_versions_artifact_digest was not created"
                assert "UNIQUE" not in row[0].upper()
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'app_versions'::regclass "
                    "AND conname = 'app_versions_artifact_digest_key'"
                )
                assert cur.fetchone() is None, "old global UNIQUE(artifact_digest) still present"
        finally:
            conn.close()

    def test_app_id_version_unique_constraint_present(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_constraint c "
                    "WHERE c.conrelid = 'app_versions'::regclass AND c.contype = 'u' "
                    "AND (SELECT array_agg(a.attname ORDER BY a.attname) "
                    "     FROM unnest(c.conkey) AS k(attnum) "
                    "     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum"
                    "    ) = ARRAY['app_id', 'version']::name[]"
                )
                assert cur.fetchone() is not None, "UNIQUE(app_id, version) is missing"
        finally:
            conn.close()


@requires_docker
class TestDowngradeThenUpgrade:
    """Downgrade restores the old global UNIQUE when no duplicate digests exist, then re-upgrades cleanly."""

    def test_downgrade_then_upgrade_round_trip(self) -> None:
        with migrated_postgres("0033-artifact-digest-roundtrip") as db:
            alembic_cli("downgrade", "0032_bundle_reader_role", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'app_versions'::regclass "
                        "AND conname = 'app_versions_artifact_digest_key'"
                    )
                    assert cur.fetchone() is not None, "downgrade did not restore the old UNIQUE"
                    cur.execute(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'app_versions' "
                        "AND indexname = 'idx_app_versions_artifact_digest'"
                    )
                    assert cur.fetchone() is None, "downgrade left the new index behind"

                    cur.execute(
                        "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        ("waddles.core.restored",),
                    )
                    _insert_version(
                        cur,
                        app_id="waddles.core.restored",
                        version="1.0.0",
                        digest=_SAME_DIGEST,
                    )
                with (
                    pytest.raises(psycopg2.errors.UniqueViolation),
                    conn.cursor() as cur,
                ):
                    _insert_version(
                        cur,
                        app_id="waddles.core.restored",
                        version="1.0.1",
                        digest=_SAME_DIGEST,
                    )
            finally:
                conn.close()

            alembic_cli("upgrade", "head", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'app_versions'::regclass "
                        "AND conname = 'app_versions_artifact_digest_key'"
                    )
                    assert cur.fetchone() is None, "re-upgrade did not drop the global UNIQUE again"
            finally:
                conn.close()


@requires_docker
class TestDowngradeFailsOnDuplicateDigests:
    """Downgrading with duplicate digests already stored fails loudly instead of corrupting data."""

    def test_downgrade_refuses_when_digest_shared_across_versions(self) -> None:
        with migrated_postgres("0033-artifact-digest-dupeguard") as db:
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO app_catalog (app_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        ("waddles.core.ping",),
                    )
                    _insert_version(
                        cur, app_id="waddles.core.ping", version="1.0.2", digest=_SAME_DIGEST
                    )
                    _insert_version(
                        cur, app_id="waddles.core.ping", version="1.0.3", digest=_SAME_DIGEST
                    )
            finally:
                conn.close()

            with pytest.raises(RuntimeError) as excinfo:
                alembic_cli("downgrade", "0032_bundle_reader_role", dsn=db.dsn)
            message = str(excinfo.value)
            assert "cannot downgrade 0033_artifact_digest_not_unique" in message
            assert _SAME_DIGEST in message

            # The constraint must still be absent -- the failed downgrade must not have
            # left the schema half-migrated (it raises before the ALTER TABLE ADD CONSTRAINT).
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'app_versions'::regclass "
                        "AND conname = 'app_versions_artifact_digest_key'"
                    )
                    assert cur.fetchone() is None
            finally:
                conn.close()
