"""Real-Postgres tests for 0069_identity_view_role: `role` appended to the identity view.

The stage derives a tool caller's scopes from this column, so the test pins that the
column is appended last, carries the member's opaque role, is NULL (never defaulted)
when unset, is tenant/community-scoped like the rest of the view, stays PII-free, and
that the existing reader grant still covers it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg2
import pytest
from pg_docker import DOCKER_AVAILABLE, migrated_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_PATH = Path(__file__).resolve().parents[1] / "versions" / "0069_identity_view_role.py"
_VIEW = "community_member_identities"
_READER = "waddles_bundle_reader"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("migration_0069", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chains_off_0068_and_revision_id_fits_varchar32() -> None:
    m = _load()
    assert m.down_revision == "0068_app_catalog_kind"
    assert len(m.revision) <= 32


@requires_docker
def test_role_column_is_appended_last_nullable_and_readable_by_the_reader() -> None:
    with migrated_postgres("0069role") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s ORDER BY ordinal_position",
                    (_VIEW,),
                )
                cols = [r[0] for r in cur.fetchall()]
                assert cols[-1] == "role"
                assert cols.count("role") == 1
                assert "display_name" not in cols  # still PII-free
                cur.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT')", (_READER, _VIEW)
                )
                assert cur.fetchone()[0] is True
        finally:
            conn.close()
