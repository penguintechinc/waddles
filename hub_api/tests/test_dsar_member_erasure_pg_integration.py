"""Art. 17 `community_members` PII erasure against a REAL, fully-migrated Postgres.

The sqlite suite (`test_data_privacy_erasure.py`) proves the transaction and the matching rules.
It cannot prove what only Postgres does to the statement: alembic 0048's
`trg_community_members_user_uuid` fires `BEFORE UPDATE OF user_id, platform, platform_user_id`
on exactly the column erasure NULLs, and `social_links` is a JSONB column (a pydal `None` must
land as SQL NULL, not the JSON literal `null`). Here the production helper
(`data_privacy_service._erase_community_member_pii`) runs through the real pydal `DAL` against
the real trigger, function and `identity_resolution_events` table.

Skipped (never failed) where the `docker` CLI is unavailable, like every other real-Postgres
test in this repo.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydal import DAL, Field

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import data_privacy_service as privacy  # noqa: E402
from services.schema import bind_auth_tables  # noqa: E402

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

PII_COLUMNS = ("display_name", "avatar_url", "bio", "social_links", "platform_user_id")


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes the 0045/0048 trigger)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-dsar-member-erasure") as db:
        yield db


@pytest.fixture
def dal(pg_db: PgTestDatabase) -> Iterator[Any]:
    """A real pydal `DAL` on the migrated container (`migrate=False`: the schema is real)."""
    connection = DAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    connection.define_table(
        "tenants",
        Field("slug", "string", unique=True),
        Field("display_name", "string"),
        Field("logo_url", "string"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )
    # `alembic/tests/pg_docker.py` bootstraps only the legacy columns migrations 0020+ FK
    # against. These four are in the real baseline (`config/postgres/migrations/
    # 000_create_base_schema.sql`, `community_members`) and are what erasure writes.
    connection.executesql(
        "ALTER TABLE community_members ADD COLUMN IF NOT EXISTS avatar_url TEXT, "
        "ADD COLUMN IF NOT EXISTS bio TEXT, ADD COLUMN IF NOT EXISTS social_links JSONB, "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP, "
        "ADD COLUMN IF NOT EXISTS reputation INTEGER DEFAULT 600, "
        "ADD COLUMN IF NOT EXISTS role VARCHAR(50) DEFAULT 'member'"
    )
    connection.executesql("ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
    connection.commit()
    connection.executesql(
        "TRUNCATE community_members, ephemeral_pseudonyms, identity_resolution_events, "
        "hub_user_identities, hub_users, communities, tenants RESTART IDENTITY CASCADE"
    )
    connection.commit()
    # Binding the production table definitions is what proves the erasure columns exist on
    # `dal.community_members` (the helper raises if one is unbound).
    bind_auth_tables(connection, migrate=False)
    yield connection
    connection.close()


def _one(dal: Any, sql: str, params: list[Any] | None = None) -> Any:
    rows = dal.executesql(sql, placeholders=params or [])
    dal.commit()
    return rows[0][0] if rows else None


def _seed_community(dal: Any, slug: str = "acme") -> int:
    tenant_id = int(_one(dal, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", [slug]))
    return int(
        _one(
            dal,
            "INSERT INTO communities (name, tenant_id) VALUES ('c', %s) RETURNING id",
            [tenant_id],
        )
    )


def _hub_user(dal: Any, username: str) -> tuple[int, str]:
    rows = dal.executesql(
        "INSERT INTO hub_users (username) VALUES (%s) RETURNING id, uuid::text",
        placeholders=[username],
    )
    dal.commit()
    return int(rows[0][0]), str(rows[0][1])


def _member(
    dal: Any,
    community_id: int,
    *,
    user_id: str | None,
    platform: str,
    puid: str,
) -> int:
    return int(
        _one(
            dal,
            "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
            "display_name, avatar_url, bio, social_links, reputation, role) "
            "VALUES (%s, %s, %s, %s, 'Zed', 'https://cdn.example/z.png', 'zed bio', "
            "'{\"twitter\": \"@zed\"}'::jsonb, 742, 'moderator') RETURNING id",
            [community_id, user_id, platform, puid],
        )
    )


def _row(dal: Any, member_id: int) -> dict[str, Any]:
    cols = (
        *PII_COLUMNS,
        "user_id",
        "reputation",
        "role",
        "user_uuid::text",
        "user_uuid_unavailable_reason",
        "(social_links IS NULL)",
        "updated_at",
    )
    names = (
        *PII_COLUMNS,
        "user_id",
        "reputation",
        "role",
        "user_uuid",
        "unavailable_reason",
        "social_links_is_sql_null",
        "updated_at",
    )
    rows = dal.executesql(
        f"SELECT {', '.join(cols)} FROM community_members WHERE id = %s",  # noqa: S608 - fixed literals
        placeholders=[member_id],
    )
    dal.commit()
    return dict(zip(names, rows[0], strict=True))


def _erase(dal: Any, user_id: int) -> int:
    count = privacy._erase_community_member_pii(dal, user_id=user_id, now=datetime.now(UTC))
    dal.commit()
    return count


@pg_only
def test_real_update_nulls_every_pii_column_and_keeps_the_row_attributable(dal: Any) -> None:
    community_id = _seed_community(dal)
    user_id, hub_uuid = _hub_user(dal, "subject")
    member_id = _member(dal, community_id, user_id=str(user_id), platform="discord", puid="d-1")
    before = _row(dal, member_id)
    assert before["user_uuid"] == hub_uuid  # the 0048 trigger derived it from the hub link
    assert before["social_links_is_sql_null"] is False

    count = _erase(dal, user_id)

    after = _row(dal, member_id)
    print(f"pg check: members_anonymized={count} pii_columns={len(PII_COLUMNS)}")
    assert count == 1
    assert {column: after[column] for column in PII_COLUMNS} == dict.fromkeys(PII_COLUMNS)
    assert after["social_links_is_sql_null"] is True  # SQL NULL, not the JSON literal `null`
    assert (after["user_id"], after["reputation"], after["role"]) == (
        str(user_id),
        742,
        "moderator",
    )
    assert after["updated_at"] is not None
    # The trigger fired on the platform_user_id UPDATE and re-derived the SAME uuid through the
    # retained hub link: the row stays attributable to the (anonymized) hub user, no error raised.
    assert (after["user_uuid"], after["unavailable_reason"]) == (hub_uuid, None)


@pg_only
def test_platform_keyed_row_is_nulled_and_the_trigger_degrades_it_without_failing(
    dal: Any,
) -> None:
    """No hub link on the row + platform id nulled: 0048 marks it `unresolvable`, never raises."""
    community_id = _seed_community(dal)
    user_id, _ = _hub_user(dal, "linked")
    dal.executesql(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'twitch', 't-9')",
        placeholders=[user_id],
    )
    dal.commit()
    orphan = _member(dal, community_id, user_id=None, platform="twitch", puid="t-9")
    assert _row(dal, orphan)["user_uuid"] is not None  # resolved via the identity link

    count = _erase(dal, user_id)

    after = _row(dal, orphan)
    assert count == 1
    assert {column: after[column] for column in PII_COLUMNS} == dict.fromkeys(PII_COLUMNS)
    assert (after["user_uuid"], after["unavailable_reason"]) == (None, "unresolvable")
    events = _one(
        dal,
        "SELECT count(*) FROM identity_resolution_events WHERE event = 'unresolvable' "
        "AND member_id = %s",
        [orphan],
    )
    assert events == 1  # the degradation is recorded, not silent


@pg_only
def test_other_users_and_other_hub_links_are_untouched_on_real_postgres(dal: Any) -> None:
    community_id = _seed_community(dal)
    subject, _ = _hub_user(dal, "subject2")
    bystander, _ = _hub_user(dal, "bystander2")
    dal.executesql(
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, 'discord', 'd-shared')",
        placeholders=[subject],
    )
    dal.commit()
    theirs = _member(dal, community_id, user_id=str(bystander), platform="discord", puid="d-b")
    claimed_by_other = _member(
        dal, community_id, user_id=str(bystander), platform="discord", puid="d-shared"
    )
    before = (_row(dal, theirs), _row(dal, claimed_by_other))

    count = _erase(dal, subject)

    assert count == 0
    assert (_row(dal, theirs), _row(dal, claimed_by_other)) == before
