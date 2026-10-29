"""Real-Postgres test for 0068_app_catalog_kind: nullable `app_catalog.kind` exists at head."""

from __future__ import annotations

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@requires_docker
def test_app_catalog_kind_column_is_nullable_varchar() -> None:
    """After `upgrade head`, `app_catalog.kind` is a nullable VARCHAR(20)."""
    with migrated_postgres("0068kind") as db:
        conn = psycopg2.connect(db.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_nullable, character_maximum_length FROM "
                    "information_schema.columns WHERE table_name='app_catalog' "
                    "AND column_name='kind'"
                )
                assert cur.fetchall() == [("YES", 20)]
        finally:
            conn.close()
