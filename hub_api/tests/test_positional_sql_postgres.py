"""Real-Postgres regression: `$N` SQL in hub_api services must execute under psycopg2.

pydal passes SQL to the driver verbatim and psycopg2 only understands `%s`, so `$1` raised
`there is no parameter $1` (HTTP 500) on Postgres while the SQLite unit suite stayed green.
These tests run the actual service functions against a throwaway Postgres container. Skipped
(never failed) without the docker CLI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydal import DAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    empty_postgres,
)

from services import community_chat, community_inventory  # noqa: E402
from services.community_interaction import can_create_channel  # noqa: E402
from services.community_relay import _ensure_relay_tables, relay_message  # noqa: E402
from services.floor_ban_erasure import _postgres_call  # noqa: E402
from services.sql_util import execute_positional  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_FUNCTIONS = """
ALTER TABLE communities ADD COLUMN IF NOT EXISTS config text;
-- pydal maps booleans to CHAR(1); real migrations use BOOLEAN (queries compare to `true`).
ALTER TABLE mirror_groups ALTER COLUMN is_active DROP DEFAULT;
ALTER TABLE mirror_groups ALTER COLUMN is_active TYPE boolean USING (is_active = 'T');
ALTER TABLE mirror_group_members ALTER COLUMN is_active DROP DEFAULT;
ALTER TABLE mirror_group_members ALTER COLUMN is_active TYPE boolean USING (is_active = 'T');
ALTER TABLE community_members ALTER COLUMN is_active DROP DEFAULT;
ALTER TABLE community_members ALTER COLUMN is_active TYPE boolean USING (is_active = 'T');
ALTER TABLE inventory_log ADD COLUMN IF NOT EXISTS performed_by_user_id integer;
ALTER TABLE inventory_log ADD COLUMN IF NOT EXISTS details text;
ALTER TABLE inventory_log ADD COLUMN IF NOT EXISTS quantity_change integer;
CREATE FUNCTION get_inventory_summary(cid integer)
RETURNS TABLE(a bigint, b bigint, c bigint, d bigint, e bigint, f bigint) LANGUAGE sql AS
$f$ SELECT 1::bigint, 2::bigint, 3::bigint, 4::bigint, 5::bigint, 6::bigint
   WHERE cid IS NOT NULL $f$;
CREATE FUNCTION reputation_floor_ban_erase_subject(t uuid, ten bigint, p text, c bytea)
RETURNS TABLE(r_deleted integer, r_pseudonymised integer, r_pending integer) LANGUAGE sql AS
$f$ SELECT 3, 2, 1 WHERE t IS NOT NULL AND ten IS NOT NULL AND p IS NOT NULL $f$;
"""


@pytest.fixture(scope="module")
def dal(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """A pydal DAL over a real, empty Postgres with the community tables created."""
    with empty_postgres(f"positional-sql-{os.getpid()}") as db:
        pdal = DAL(
            _url(db), folder=str(tmp_path_factory.mktemp("pydal")), migrate=True, pool_size=2
        )
        try:
            _ensure_relay_tables(pdal, migrate=True)
            pdal.executesql(_FUNCTIONS)
            pdal.commit()
            yield pdal
        finally:
            pdal.close()


def _url(db: PgTestDatabase) -> str:
    return db.dsn.replace("postgresql://", "postgres://", 1)


@pytest.fixture(autouse=True)
def _clean_txn(dal: Any) -> Iterator[None]:
    """Never let one failing statement poison the next test's transaction."""
    yield
    dal.rollback()


def test_helper_adapts_every_marker_shape(dal: Any) -> None:
    """`$N`, repeats, out-of-order, casts and `ANY($N)` list binding all run on psycopg2."""
    rows = execute_positional(
        dal, "SELECT $2::int + $1::int, $2::int, $1::int WHERE 5 = ANY($3)", [1, 2, [5, 6]]
    )
    assert list(rows[0]) == [3, 2, 1]


def test_raw_dollar_sql_really_fails_on_postgres(dal: Any) -> None:
    """Proves this fixture reproduces the production failure (not a vacuous pass)."""
    with pytest.raises(Exception, match=r"\$1|parameter"):
        dal.executesql("SELECT $1", placeholders=[1])
    dal.rollback()


def test_chat_channels(dal: Any) -> None:
    assert [c.name for c in community_chat.get_chat_channels(dal, 1)] == ["general"]


def test_inventory_queries_with_every_filter_shape(dal: Any) -> None:
    assert community_inventory.list_all_checkouts(dal, 1, None) == []
    assert community_inventory.list_all_checkouts(dal, 1, "active") == []
    assert (
        community_inventory.get_audit_log(dal, 1, item_id=None, action=None, limit=10, offset=0)
        == []
    )
    assert (
        community_inventory.get_audit_log(dal, 1, item_id=5, action="add", limit=10, offset=0) == []
    )
    assert community_inventory.get_my_checkouts(dal, 1, 7) == []
    assert community_inventory.get_summary(dal, 1) is not None


def test_relay_fan_out_queries(dal: Any) -> None:
    dal.executesql(
        "INSERT INTO mirror_groups (community_id, channel_type, is_active) VALUES (1, 'chat', true)"
    )
    dal.executesql(
        "INSERT INTO mirror_group_members (mirror_group_id, community_server_id, "
        "community_server_channel_id, direction, is_active) VALUES (1, 1, 10, 'both', true)"
    )
    dal.commit()
    asyncio.run(
        relay_message(
            dal,
            source_member_channel_id=10,
            platform="hub",
            channel_type="chat",
            content={},
            author={},
        )
    )


def test_interaction_channel_policy_query(dal: Any) -> None:
    dal.executesql("INSERT INTO communities (name, tenant_id) VALUES ('c', 1)")
    dal.commit()
    assert can_create_channel(dal, 1, 7, False) is False


def test_floor_ban_erase_function_call(dal: Any) -> None:
    call = _postgres_call(dal)
    assert call("00000000-0000-0000-0000-000000000001", 1, "twitch", b"\x01") == (3, 2, 1)
