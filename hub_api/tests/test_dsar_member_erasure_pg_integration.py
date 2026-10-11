"""Suppression-only erasure + ban-evasion suppression against a REAL, fully-migrated Postgres.

The sqlite suites (`test_data_privacy_erasure.py`, `test_ban_suppression.py`) prove the
transaction and the matching rules. They cannot prove what only Postgres does to the statements:

- the `suppression_records` CHECK constraints accept exactly what production code writes (a
  64-char lowercase hex code, the closed enum) and the UNIQUE index backs the upsert;
- `timestamptz` comparisons with Python aware datetimes decide expiry correctly;
- alembic 0045/0048's `trg_community_members_user_uuid` (a real trigger, real functions, real
  `ephemeral_pseudonyms` rows it mints) coexists with member-row DELETEs and the banned-role
  UPDATE that re-applies a restriction;
- pydal DELETE / SELECT against tables WITHOUT an `id` column (`bundle_reputation_scores`,
  `ephemeral_pseudonyms`) and against UUID-typed columns (`user_uuid IN (...)`);
- every column the pydal binders declare really exists in the production DDL (a drifted binder
  would otherwise only fail in production).

Here the production helpers (`_collect_erasure_scope`, `_delete_memberships`,
`_delete_reputation_and_identity`, `ban_suppression.*`) run through the real pydal `AsyncDAL`.
The reputation tables are created from the REAL legacy files (`config/postgres/migrations/
080|097_*.sql`), not a hand-written copy.

Skipped (never failed) where the `docker` CLI is unavailable, like every other real-Postgres
test in this repo.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from flask_core.database import AsyncDAL

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import ban_suppression as bs  # noqa: E402
from services import data_privacy_service as privacy  # noqa: E402
from services import suppression_keys as keys  # noqa: E402
from services.schema import bind_auth_tables  # noqa: E402

pg_only = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

MASTER_HEX = "ab" * 32
PUID = "d-1000000001"
LEGACY_SQL_DIR = Path(__file__).resolve().parents[2] / "config" / "postgres" / "migrations"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (0045/0048 trigger, 0050, 0059)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-suppression-pg") as db:
        raw = psycopg2.connect(db.dsn)
        raw.autocommit = True
        with raw.cursor() as cur:
            # `alembic/tests/pg_docker.py` bootstraps only the legacy columns migrations 0020+ FK
            # against. These are in the real baseline (`000_create_base_schema.sql`) and are what
            # erasure / re-application reads and writes.
            cur.execute(
                "ALTER TABLE community_members ADD COLUMN IF NOT EXISTS avatar_url TEXT, "
                "ADD COLUMN IF NOT EXISTS bio TEXT, ADD COLUMN IF NOT EXISTS social_links JSONB, "
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP, "
                "ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "ADD COLUMN IF NOT EXISTS reputation INTEGER DEFAULT 600, "
                "ADD COLUMN IF NOT EXISTS role VARCHAR(50) DEFAULT 'member'"
            )
            cur.execute(
                "ALTER TABLE communities ADD COLUMN IF NOT EXISTS member_count INTEGER DEFAULT 0, "
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"
            )
            cur.execute("ALTER TABLE hub_user_identities ADD COLUMN IF NOT EXISTS avatar_url TEXT")
            # The reputation tables come from the REAL legacy SQL (no hand-written copy).
            for name in ("080_add_reputation_tables.sql", "097_reputation_tenant_scope.sql"):
                cur.execute((LEGACY_SQL_DIR / name).read_text())
            cur.execute(
                "INSERT INTO app_catalog (app_id) VALUES ('rep-app') ON CONFLICT DO NOTHING"
            )
        raw.close()
        yield db


@pytest.fixture
def adal(pg_db: PgTestDatabase, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncDAL]:
    """A real pydal `AsyncDAL` on the migrated container (`migrate=False`: the schema is real)."""
    monkeypatch.setenv(keys.ENV_CURRENT, MASTER_HEX)
    monkeypatch.delenv(keys.ENV_PREVIOUS, raising=False)
    monkeypatch.delenv(bs.ENV_MAX_RETENTION_DAYS, raising=False)
    async_dal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        _field("slug"),
        _field("is_active", "boolean"),
        _field("logo_url"),
        _field("display_name"),
        _field("is_global", "boolean"),
        _field("config", "json"),
        migrate=False,
    )
    bind_auth_tables(dal, migrate=False)
    dal.executesql(
        "TRUNCATE suppression_records, bundle_reputation_adjustments, bundle_reputation_scores, "
        "reputation_events, reputation_tenant, community_members, ephemeral_pseudonyms, "
        "identity_resolution_events, hub_user_identities, hub_users, communities, tenants "
        "RESTART IDENTITY CASCADE"
    )
    dal.commit()
    yield async_dal
    dal.close()


def _field(name: str, kind: str = "string") -> Any:
    from pydal import Field

    return Field(name, kind)


def _q(dal: Any, sql: str, params: list[Any] | None = None) -> list[Any]:
    rows = dal.executesql(sql, placeholders=params or [])
    dal.commit()
    return list(rows or [])


def _one(dal: Any, sql: str, params: list[Any] | None = None) -> Any:
    rows = _q(dal, sql, params)
    return rows[0][0] if rows else None


def _tenant(dal: Any, slug: str) -> int:
    return int(_one(dal, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", [slug]))


def _community(dal: Any, tenant_id: int, member_count: int = 5) -> int:
    return int(
        _one(
            dal,
            "INSERT INTO communities (name, tenant_id, member_count) VALUES ('c', %s, %s) "
            "RETURNING id",
            [tenant_id, member_count],
        )
    )


def _hub_user(dal: Any, username: str) -> tuple[int, str]:
    rows = _q(
        dal,
        "INSERT INTO hub_users (username) VALUES (%s) RETURNING id, uuid::text",
        [username],
    )
    return int(rows[0][0]), str(rows[0][1])


def _identity(dal: Any, user_id: int, platform: str, puid: str) -> None:
    _q(
        dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (%s, %s, %s)",
        [user_id, platform, puid],
    )


def _member(
    dal: Any,
    community_id: int,
    *,
    user_id: int | None,
    platform: str | None = None,
    puid: str | None = None,
    role: str = "member",
    active: bool = True,
    reputation: int = 742,
) -> int:
    return int(
        _one(
            dal,
            "INSERT INTO community_members (community_id, user_id, platform, platform_user_id, "
            "role, is_active, reputation) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            [
                community_id,
                None if user_id is None else str(user_id),
                platform,
                puid,
                role,
                active,
                reputation,
            ],
        )
    )


def _scope_and_erase(dal: Any, user_id: int, now: datetime) -> dict[str, Any]:
    """The erasure core's new statements, exactly as `_sync_erase` composes them, one tx."""
    try:
        scope = privacy._collect_erasure_scope(dal, user_id=user_id)  # noqa: SLF001
        written = bs.record_suppressions(
            dal,
            scope.restrictions,
            keys=keys.PlatformKekKeySource(),
            now=now,
            retention_days=bs.max_retention_days() if scope.restrictions else None,
        )
        members = privacy._delete_memberships(dal, scope, now=now)  # noqa: SLF001
        rest = privacy._delete_reputation_and_identity(dal, scope, user_id=user_id)  # noqa: SLF001
        dal.commit()
        return {"scope": scope, "written": written, "members": members, **rest}
    except Exception:
        dal.rollback()
        raise


def _suppression(dal: Any) -> list[tuple[Any, ...]]:
    return _q(
        dal,
        "SELECT tenant_id, platform, key_ver, hmac, reason_enum, expires_at "
        "FROM suppression_records ORDER BY id",
    )


@pg_only
class TestBinderMatchesTheRealSchema:
    """A binder column that does not exist in the DDL would only fail in production.

    Tables this change owns or fully binds are checked in full. `community_members` and
    `hub_user_identities` are shared legacy tables (the test bootstrap carries only some of their
    columns), so only the columns suppression/erasure actually reads or writes are pinned.
    """

    OWNED = (
        "suppression_records",
        "reputation_events",
        "reputation_tenant",
        "bundle_reputation_scores",
        "bundle_reputation_adjustments",
        "ephemeral_pseudonyms",
    )
    USED = {
        "community_members": (
            "community_id",
            "user_id",
            "platform",
            "platform_user_id",
            "role",
            "is_active",
            "user_uuid",
            "joined_at",
            "updated_at",
            "reputation",
        ),
        "hub_user_identities": ("hub_user_id", "platform", "platform_user_id"),
    }

    @staticmethod
    def _real(adal: AsyncDAL, table: str) -> set[str]:
        return {
            r[0]
            for r in _q(
                adal.dal,
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                [table],
            )
        }

    @pytest.mark.parametrize("table", OWNED)
    def test_every_bound_field_of_an_owned_table_is_a_real_column(
        self, adal: AsyncDAL, table: str
    ) -> None:
        real = self._real(adal, table)
        bound = {f for f in adal.dal[table].fields if f != "id"}
        print(f"{table}: bound={len(bound)} real={len(real)} missing={sorted(bound - real)}")
        assert (
            bound <= real
        ), f"{table}: binder declares columns the DB lacks: {sorted(bound - real)}"

    @pytest.mark.parametrize("table", sorted(USED))
    def test_every_column_suppression_uses_on_a_shared_table_is_real_and_bound(
        self, adal: AsyncDAL, table: str
    ) -> None:
        real = self._real(adal, table)
        bound = set(adal.dal[table].fields)
        used = set(self.USED[table])
        assert used <= real, f"{table}: DDL lacks {sorted(used - real)}"
        assert used <= bound, f"{table}: binder lacks {sorted(used - bound)}"


@pg_only
class TestBenignErasureOnPostgres:
    def test_everything_incl_reputation_is_deleted_and_nothing_is_kept(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        community = _community(dal, tenant)
        user_id, user_uuid = _hub_user(dal, "benign")
        _identity(dal, user_id, "discord", PUID)
        _member(dal, community, user_id=user_id)
        # A platform-keyed row (no hub link) in ANOTHER community: one hub user may hold only one
        # uuid-bearing membership per community (alembic 0048), and it resolves via the identity.
        orphan = _member(dal, _community(dal, tenant), user_id=None, platform="discord", puid=PUID)
        assert _one(
            dal, "SELECT user_uuid IS NOT NULL FROM community_members WHERE id=%s", [orphan]
        )
        pseudonyms_before = _one(dal, "SELECT count(*) FROM ephemeral_pseudonyms")
        _q(
            dal,
            "INSERT INTO reputation_events (community_id, hub_user_id, platform, platform_user_id,"
            " event_type, score_before, score_after)"
            " VALUES (%s, %s, 'discord', %s, 'chat', 600, 601)",
            [community, user_id, PUID],
        )
        _q(
            dal,
            "INSERT INTO reputation_tenant (tenant_id, hub_user_id, score) VALUES (%s, %s, 777)",
            [tenant, user_id],
        )
        _q(
            dal,
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
            "VALUES (%s, %s, %s::uuid, 5)",
            [tenant, community, user_uuid],
        )
        _q(
            dal,
            "INSERT INTO bundle_reputation_adjustments (app_id, tenant_id, community_id, "
            "target_user_uuid, scope, delta, reason_code) VALUES ('rep-app', %s, %s, %s, "
            "'community', 5, 'chat')",
            [tenant, community, user_uuid],
        )

        result = _scope_and_erase(dal, user_id, datetime.now(UTC))

        print(
            f"pg benign erasure: {({k: v for k, v in result.items() if k != 'scope'})} "
            f"pseudonyms_before={pseudonyms_before}"
        )
        assert result["written"] == 0
        for table, column in (
            ("community_members", f"user_id = '{user_id}'"),
            ("hub_user_identities", f"hub_user_id = {user_id}"),
            ("reputation_events", f"hub_user_id = {user_id}"),
            ("reputation_tenant", f"hub_user_id = {user_id}"),
            ("bundle_reputation_scores", f"user_uuid = '{user_uuid}'"),
            ("bundle_reputation_adjustments", f"target_user_uuid = '{user_uuid}'"),
        ):
            assert _one(dal, f"SELECT count(*) FROM {table} WHERE {column}") == 0, table  # noqa: S608
        assert _one(dal, "SELECT count(*) FROM community_members WHERE id=%s", [orphan]) == 0
        assert (
            _one(dal, "SELECT count(*) FROM ephemeral_pseudonyms WHERE platform_user_id=%s", [PUID])
            == 0
        )
        assert _suppression(dal) == []
        assert _one(dal, "SELECT member_count FROM communities WHERE id=%s", [community]) == 4

    def test_a_pseudonym_minted_before_the_account_was_linked_is_deleted_with_its_scores(
        self, adal: AsyncDAL
    ) -> None:
        """Erasure reaches a pseudonym the 0045 trigger minted before the account was linked.

        The trigger mints `ephemeral_pseudonyms` (the raw handle store) for an unlinked platform
        account; once the person links it, erasure must delete the mapping AND the bundle
        reputation keyed on the pseudonym uuid.
        """
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        community = _community(dal, tenant)
        orphan = _member(dal, community, user_id=None, platform="discord", puid=PUID)
        pseudonym = _one(
            dal,
            "SELECT pseudonym::text FROM ephemeral_pseudonyms WHERE platform_user_id=%s",
            [PUID],
        )
        assert pseudonym is not None  # the trigger really minted one
        _q(
            dal,
            "UPDATE ephemeral_pseudonyms SET handle='Raw Handle' WHERE pseudonym=%s::uuid",
            [pseudonym],
        )
        _q(
            dal,
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
            "VALUES (%s, %s, %s::uuid, 5)",
            [tenant, community, pseudonym],
        )
        subject, _ = _hub_user(dal, "late-linker")
        _identity(dal, subject, "discord", PUID)

        result = _scope_and_erase(dal, subject, datetime.now(UTC))

        assert result["ephemeral_pseudonyms"] == 1
        assert (
            _one(dal, "SELECT count(*) FROM ephemeral_pseudonyms WHERE platform_user_id=%s", [PUID])
            == 0
        )
        assert (
            _one(
                dal,
                "SELECT count(*) FROM bundle_reputation_scores WHERE user_uuid=%s::uuid",
                [pseudonym],
            )
            == 0
        )
        assert _one(dal, "SELECT count(*) FROM community_members WHERE id=%s", [orphan]) == 0

    def test_another_users_rows_are_untouched(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        community = _community(dal, tenant)
        subject, _ = _hub_user(dal, "subject")
        bystander, bystander_uuid = _hub_user(dal, "bystander")
        _identity(dal, subject, "discord", PUID)
        _identity(dal, bystander, "discord", "d-other")
        theirs = _member(dal, community, user_id=bystander, platform="discord", puid="d-other")
        _q(
            dal,
            "INSERT INTO bundle_reputation_scores (tenant_id, community_id, user_uuid, balance) "
            "VALUES (%s, %s, %s::uuid, 9)",
            [tenant, community, bystander_uuid],
        )

        _scope_and_erase(dal, subject, datetime.now(UTC))

        assert _one(dal, "SELECT count(*) FROM community_members WHERE id=%s", [theirs]) == 1
        assert (
            _one(dal, "SELECT count(*) FROM hub_user_identities WHERE hub_user_id=%s", [bystander])
            == 1
        )
        assert _one(dal, "SELECT count(*) FROM bundle_reputation_scores") == 1


@pg_only
class TestBannedErasureOnPostgres:
    def _banned(self, dal: Any) -> tuple[int, int, int]:
        tenant = _tenant(dal, "acme")
        community = _community(dal, tenant)
        user_id, _ = _hub_user(dal, "banned")
        _identity(dal, user_id, "discord", PUID)
        _member(dal, community, user_id=user_id, role="banned", active=False, reputation=310)
        return tenant, community, user_id

    def test_exactly_one_valid_row_is_written_and_everything_else_deleted(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        tenant, community, user_id = self._banned(dal)
        now = datetime.now(UTC)

        result = _scope_and_erase(dal, user_id, now)

        [(t, platform, key_ver, code, reason, expires)] = _suppression(dal)
        expected = keys.current_code(keys.PlatformKekKeySource(), tenant, "discord", PUID)
        assert (t, platform, key_ver, code, reason, expires) == (
            tenant,
            "discord",
            expected.key_ver,
            expected.hmac,
            "ban",
            None,
        )  # the real CHECK constraints accepted it
        assert result["written"] == 1 and result["members"] == 1
        assert (
            _one(dal, "SELECT count(*) FROM community_members WHERE community_id=%s", [community])
            == 0
        )
        assert (
            _one(dal, "SELECT count(*) FROM hub_user_identities WHERE hub_user_id=%s", [user_id])
            == 0
        )
        raw = repr(_q(dal, "SELECT * FROM suppression_records"))
        assert PUID not in raw

    def test_the_upsert_extends_one_row_against_the_real_unique_index(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        tenant, _, _ = self._banned(dal)
        now = datetime.now(UTC)
        dated = bs.ActiveRestriction(
            tenant, "discord", PUID, bs.RestrictionReason.AUTO_BAN, now + timedelta(days=2)
        )
        forever = bs.ActiveRestriction(tenant, "discord", PUID, bs.RestrictionReason.BAN, None)
        source = keys.PlatformKekKeySource()

        bs.record_suppressions(dal, [dated], keys=source, now=now)
        bs.record_suppressions(dal, [forever], keys=source, now=now)
        dal.commit()

        [(_, _, _, _, reason, expires)] = _suppression(dal)
        assert (reason, expires) == ("ban", None)

    def test_the_retention_ceiling_lands_as_a_real_timestamptz(
        self, adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bs.ENV_MAX_RETENTION_DAYS, "30")
        dal = adal.dal
        _, _, user_id = self._banned(dal)
        now = datetime.now(UTC)

        _scope_and_erase(dal, user_id, now)

        [(*_, expires)] = _suppression(dal)
        assert abs(expires - (now + timedelta(days=30))) < timedelta(seconds=2)


@pg_only
class TestReturnOnPostgres:
    async def test_a_returning_banned_user_is_recaught_and_the_ban_reapplied(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        old_community = _community(dal, tenant)
        old, _ = _hub_user(dal, "old")
        _identity(dal, old, "discord", PUID)
        _member(dal, old_community, user_id=old, role="banned", active=False)
        _scope_and_erase(dal, old, datetime.now(UTC))
        target = _community(dal, tenant)
        returning, _ = _hub_user(dal, "returning")
        _identity(dal, returning, "discord", PUID)

        match = await bs.check_and_ban_membership(
            adal, dal, community_id=target, hub_user_id=returning
        )

        assert match is not None and match.reason is bs.RestrictionReason.BAN
        row = _q(
            dal,
            "SELECT role, is_active, user_uuid IS NOT NULL FROM community_members "
            "WHERE community_id=%s AND user_id=%s",
            [target, str(returning)],
        )
        assert row == [("banned", False, True)]  # the 0048 trigger resolved the uuid on insert

    async def test_a_benign_return_gets_a_fresh_start(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        old, _ = _hub_user(dal, "benign-old")
        _identity(dal, old, "discord", PUID)
        _member(dal, _community(dal, tenant), user_id=old)
        _scope_and_erase(dal, old, datetime.now(UTC))
        target = _community(dal, tenant)
        returning, _ = _hub_user(dal, "benign-back")
        _identity(dal, returning, "discord", PUID)

        assert (
            await bs.check_and_ban_membership(adal, dal, community_id=target, hub_user_id=returning)
            is None
        )
        assert (
            _one(dal, "SELECT count(*) FROM community_members WHERE community_id=%s", [target]) == 0
        )

    async def test_another_tenant_never_matches(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        acme, beta = _tenant(dal, "acme"), _tenant(dal, "beta")
        old, _ = _hub_user(dal, "old2")
        _identity(dal, old, "discord", PUID)
        _member(dal, _community(dal, acme), user_id=old, role="banned", active=False)
        _scope_and_erase(dal, old, datetime.now(UTC))
        returning, _ = _hub_user(dal, "cross")
        _identity(dal, returning, "discord", PUID)

        assert (
            await bs.check_and_ban_membership(
                adal, dal, community_id=_community(dal, beta), hub_user_id=returning
            )
            is None
        )
        assert (
            await bs.check_and_ban_membership(
                adal, dal, community_id=_community(dal, acme), hub_user_id=returning
            )
            is not None
        )

    async def test_expiry_purge_and_lift_use_real_timestamptz(self, adal: AsyncDAL) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        source = keys.PlatformKekKeySource()
        now = datetime.now(UTC)
        bs.record_suppressions(
            dal,
            [
                bs.ActiveRestriction(
                    tenant,
                    "discord",
                    "expired",
                    bs.RestrictionReason.BAN,
                    now - timedelta(minutes=1),
                ),
                bs.ActiveRestriction(
                    tenant, "discord", "live", bs.RestrictionReason.BAN, now + timedelta(days=1)
                ),
                bs.ActiveRestriction(tenant, "discord", "forever", bs.RestrictionReason.BAN, None),
            ],
            keys=source,
            now=now,
        )
        dal.commit()

        def hit(puid: str) -> bool:
            found = bs.find_suppression(
                dal,
                tenant_id=tenant,
                platform="discord",
                platform_user_id=puid,
                keys=source,
                now=datetime.now(UTC),
            )
            dal.commit()
            return found is not None

        assert (hit("expired"), hit("live"), hit("forever")) == (False, True, True)
        assert await bs.purge_expired_async(adal, dal) == 1
        assert len(_suppression(dal)) == 2
        assert (
            await bs.lift_suppression_async(
                adal,
                dal,
                tenant_id=tenant,
                platform="discord",
                platform_user_id="live",
                keys=source,
            )
            == 1
        )
        assert (hit("live"), hit("forever")) == (False, True)

    async def test_reapply_after_identity_link_flips_a_real_membership(
        self, adal: AsyncDAL
    ) -> None:
        dal = adal.dal
        tenant = _tenant(dal, "acme")
        old, _ = _hub_user(dal, "old3")
        _identity(dal, old, "discord", PUID)
        _member(dal, _community(dal, tenant), user_id=old, role="banned", active=False)
        _scope_and_erase(dal, old, datetime.now(UTC))
        joiner, _ = _hub_user(dal, "joiner")
        community = _community(dal, tenant)
        member = _member(dal, community, user_id=joiner)
        _identity(dal, joiner, "discord", PUID)  # linked AFTER joining

        banned = await bs.reapply_for_hub_user_async(adal, dal, hub_user_id=joiner)

        assert banned == 1
        assert _q(dal, "SELECT role, is_active FROM community_members WHERE id=%s", [member]) == [
            ("banned", False)
        ]
