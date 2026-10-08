"""Real-Postgres regression tests for 0034_bar_citizen_guild_pairing.

This is the serialization anchor for the Bar Citizen workstream -- these
tests prove the migration actually lands on a real Postgres 17 container
(`pg_docker.migrated_postgres`, which replays migrations 0020 through
head) and round-trips upgrade -> downgrade -> upgrade idempotently, plus
exercise the constraints that encode this migration's design decisions
(N:M guild<->tenant pairing, opt-in direction, tenant-0 credential
rejection, subscriber-tier/moderator binding exclusivity).
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


def _connect(db: PgTestDatabase) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    return conn


def _seed_tenant(cur: psycopg2.extensions.cursor, *, slug: str, is_global: bool) -> int:
    cur.execute(
        "INSERT INTO tenants (slug, is_global) VALUES (%s, %s) RETURNING id", (slug, is_global)
    )
    return int(cur.fetchone()[0])


def _seed_community(cur: psycopg2.extensions.cursor, *, name: str) -> int:
    cur.execute("INSERT INTO communities (name) VALUES (%s) RETURNING id", (name,))
    return int(cur.fetchone()[0])


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every non-round-trip test below."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("0034-bar-citizen-guild-pairing") as db:
        yield db


@requires_docker
class TestTenantPlatformCredentials:
    def test_insert_and_unique_tenant_platform(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-bc-1", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "ciphertext-blob-1"),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "ciphertext-blob-2"),
                )
        finally:
            conn.close()

    def test_global_tenant_rejected_by_trigger(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="global-tenant", is_global=True)
            with (
                pytest.raises(psycopg2.errors.RaiseException),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "should-fail"),
                )
        finally:
            conn.close()

    def test_platform_generic_two_platforms_same_tenant(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                tenant_id = _seed_tenant(cur, slug="acme-bc-2", is_global=False)
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "discord", "discord-blob"),
                )
                cur.execute(
                    "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                    "credentials_ciphertext) VALUES (%s, %s, %s)",
                    (tenant_id, "twitch", "twitch-blob"),
                )
                cur.execute(
                    "SELECT platform FROM tenant_platform_credentials "
                    "WHERE tenant_id = %s ORDER BY platform",
                    (tenant_id,),
                )
                assert [r[0] for r in cur.fetchall()] == ["discord", "twitch"]
        finally:
            conn.close()


@requires_docker
class TestGuildTenantPairingsNtoM:
    def test_same_guild_paired_with_multiple_communities(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                c1 = _seed_community(cur, name="bc-sea")
                c2 = _seed_community(cur, name="bc-eu")
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s)",
                    (c1, "shared-guild-1", "twitch_to_discord", "[SEA]"),
                )
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s)",
                    (c2, "shared-guild-1", "bidirectional", "[EU]"),
                )
                cur.execute(
                    "SELECT community_id FROM guild_tenant_pairings "
                    "WHERE discord_guild_id = %s ORDER BY community_id",
                    ("shared-guild-1",),
                )
                assert [r[0] for r in cur.fetchall()] == sorted([c1, c2])
        finally:
            conn.close()

    def test_duplicate_community_guild_pairing_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                community_id = _seed_community(cur, name="bc-dup")
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s)",
                    (community_id, "guild-dup", "twitch_to_discord", "[DUP]"),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s)",
                    (community_id, "guild-dup", "bidirectional", "[DUP2]"),
                )
        finally:
            conn.close()

    def test_invalid_direction_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                community_id = _seed_community(cur, name="bc-baddir")
            with (
                pytest.raises(psycopg2.errors.CheckViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s)",
                    (community_id, "guild-baddir", "sideways", "[X]"),
                )
        finally:
            conn.close()

    def test_sync_enabled_defaults_false_opt_in(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                community_id = _seed_community(cur, name="bc-optin")
                cur.execute(
                    "INSERT INTO guild_tenant_pairings "
                    "(community_id, discord_guild_id, direction, role_name_prefix) "
                    "VALUES (%s, %s, %s, %s) RETURNING sync_enabled",
                    (community_id, "guild-optin", "twitch_to_discord", "[OI]"),
                )
                assert cur.fetchone()[0] is False
        finally:
            conn.close()


@requires_docker
class TestCommunityRoleSyncBindings:
    def _make_pairing(self, cur: psycopg2.extensions.cursor, *, guild_id: str) -> int:
        community_id = _seed_community(cur, name=f"bc-{guild_id}")
        cur.execute(
            "INSERT INTO guild_tenant_pairings "
            "(community_id, discord_guild_id, direction, role_name_prefix) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (community_id, guild_id, "twitch_to_discord", "[RS]"),
        )
        return int(cur.fetchone()[0])

    def test_subscriber_tier_and_moderator_coexist(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                pairing_id = self._make_pairing(cur, guild_id="guild-coexist")
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'subscriber_tier', 2, 'role-t2')",
                    (pairing_id,),
                )
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'moderator', NULL, 'role-mod')",
                    (pairing_id,),
                )
                cur.execute(
                    "SELECT sync_scope FROM community_role_sync_bindings "
                    "WHERE pairing_id = %s ORDER BY sync_scope",
                    (pairing_id,),
                )
                assert [r[0] for r in cur.fetchall()] == ["moderator", "subscriber_tier"]
        finally:
            conn.close()

    def test_duplicate_tier_binding_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                pairing_id = self._make_pairing(cur, guild_id="guild-duptier")
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'subscriber_tier', 1, 'role-t1-a')",
                    (pairing_id,),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'subscriber_tier', 1, 'role-t1-b')",
                    (pairing_id,),
                )
        finally:
            conn.close()

    def test_duplicate_moderator_binding_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                pairing_id = self._make_pairing(cur, guild_id="guild-dupmod")
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'moderator', NULL, 'role-mod-a')",
                    (pairing_id,),
                )
            with (
                pytest.raises(psycopg2.errors.UniqueViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'moderator', NULL, 'role-mod-b')",
                    (pairing_id,),
                )
        finally:
            conn.close()

    def test_scope_tier_mismatch_rejected(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                pairing_id = self._make_pairing(cur, guild_id="guild-mismatch")
            with (
                pytest.raises(psycopg2.errors.CheckViolation),
                conn.cursor() as cur,
            ):
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'moderator', 1, 'role-bad')",
                    (pairing_id,),
                )
        finally:
            conn.close()

    def test_cascade_delete_on_pairing_removal(self, pg_db: PgTestDatabase) -> None:
        conn = _connect(pg_db)
        try:
            with conn.cursor() as cur:
                pairing_id = self._make_pairing(cur, guild_id="guild-cascade")
                cur.execute(
                    "INSERT INTO community_role_sync_bindings "
                    "(pairing_id, sync_scope, subscriber_tier, discord_role_id) "
                    "VALUES (%s, 'moderator', NULL, 'role-cascade')",
                    (pairing_id,),
                )
                cur.execute("DELETE FROM guild_tenant_pairings WHERE id = %s", (pairing_id,))
                cur.execute(
                    "SELECT COUNT(*) FROM community_role_sync_bindings WHERE pairing_id = %s",
                    (pairing_id,),
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()


@requires_docker
class TestDowngradeThenUpgrade:
    def test_downgrade_then_upgrade_round_trip(self) -> None:
        with migrated_postgres("0034-bar-citizen-roundtrip") as db:
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    tenant_id = _seed_tenant(cur, slug="roundtrip-tenant", is_global=False)
                    community_id = _seed_community(cur, name="roundtrip-community")
                    cur.execute(
                        "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                        "credentials_ciphertext) VALUES (%s, %s, %s)",
                        (tenant_id, "discord", "pre-downgrade-blob"),
                    )
                    cur.execute(
                        "INSERT INTO guild_tenant_pairings "
                        "(community_id, discord_guild_id, direction, role_name_prefix) "
                        "VALUES (%s, %s, %s, %s)",
                        (community_id, "roundtrip-guild", "twitch_to_discord", "[RT]"),
                    )
            finally:
                conn.close()

            alembic_cli("downgrade", "0033_artifact_digest_not_unique", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT to_regclass('community_role_sync_bindings'), "
                        "to_regclass('guild_tenant_pairings'), "
                        "to_regclass('tenant_platform_credentials')"
                    )
                    assert cur.fetchone() == (None, None, None), "downgrade left a table behind"
            finally:
                conn.close()

            alembic_cli("upgrade", "head", dsn=db.dsn)

            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    # Tables are gone after downgrade -- re-upgrade must create them
                    # fresh (the pre-downgrade rows are intentionally not expected
                    # to survive; this proves the migration is re-appliable, not
                    # that it preserves data across a downgrade).
                    cur.execute("SELECT COUNT(*) FROM tenant_platform_credentials")
                    assert cur.fetchone()[0] == 0
                    cur.execute("SELECT COUNT(*) FROM guild_tenant_pairings")
                    assert cur.fetchone()[0] == 0
                    cur.execute("SELECT COUNT(*) FROM community_role_sync_bindings")
                    assert cur.fetchone()[0] == 0

                    # Trigger must be re-created and still enforce tenant-0 rejection.
                    tenant_id = _seed_tenant(cur, slug="post-reupgrade-global", is_global=True)
                    with pytest.raises(psycopg2.errors.RaiseException):
                        cur.execute(
                            "INSERT INTO tenant_platform_credentials (tenant_id, platform, "
                            "credentials_ciphertext) VALUES (%s, %s, %s)",
                            (tenant_id, "discord", "should-fail-again"),
                        )
            finally:
                conn.close()
