"""Round-trip + constraint tests for the Bar Citizen guild-pairing SQLAlchemy models.

Mirrors `alembic/versions/0034_bar_citizen_guild_pairing.py`'s shape --
see that migration's own docstring for the full design rationale. These
tests exercise the models directly (in-memory SQLite via `db.create_all()`),
independent of the real-Postgres migration round-trip test
(`alembic/tests/test_0034_bar_citizen_guild_pairing.py`), which proves the
two stay in lockstep (`test_0034_schema_drift.py` cross-checks column sets
between the migration and these models directly).
"""
from __future__ import annotations

import pytest
from flask import Flask
from flask_core.models import (
    CommunityRoleSyncBinding,
    GuildTenantPairing,
    TenantPlatformCredential,
    db,
)
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def app():
    """A fresh Flask app + in-memory SQLite schema per test."""
    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    db.init_app(flask_app)
    with flask_app.app_context():
        db.create_all()
        db.session.execute(db.text("INSERT INTO tenants (id, slug, is_global) VALUES (1, 'acme', 0)"))
        db.session.execute(db.text("INSERT INTO communities (id, name) VALUES (1, 'acme-bc')"))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


class TestTenantPlatformCredentialRoundTrip:
    def test_insert_and_read_back(self, app: Flask) -> None:
        with app.app_context():
            row = TenantPlatformCredential(
                tenant_id=1, platform="discord", credentials_ciphertext="blob-1"
            )
            db.session.add(row)
            db.session.commit()

            fetched = TenantPlatformCredential.query.filter_by(tenant_id=1, platform="discord").one()
            assert fetched.credentials_ciphertext == "blob-1"
            assert fetched.key_ref is None

    def test_unique_tenant_platform_enforced(self, app: Flask) -> None:
        with app.app_context():
            db.session.add(
                TenantPlatformCredential(tenant_id=1, platform="twitch", credentials_ciphertext="a")
            )
            db.session.commit()
            db.session.add(
                TenantPlatformCredential(tenant_id=1, platform="twitch", credentials_ciphertext="b")
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


class TestGuildTenantPairingRoundTrip:
    def test_insert_and_read_back(self, app: Flask) -> None:
        with app.app_context():
            pairing = GuildTenantPairing(
                community_id=1,
                discord_guild_id="guild-1",
                direction="twitch_to_discord",
                role_name_prefix="[BC]",
            )
            db.session.add(pairing)
            db.session.commit()

            fetched = GuildTenantPairing.query.filter_by(discord_guild_id="guild-1").one()
            assert fetched.sync_enabled is False
            assert fetched.direction == "twitch_to_discord"

    def test_direction_check_constraint_rejects_invalid_value(self, app: Flask) -> None:
        with app.app_context():
            db.session.add(
                GuildTenantPairing(
                    community_id=1,
                    discord_guild_id="guild-bad",
                    direction="sideways",
                    role_name_prefix="[BC]",
                )
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_unique_community_guild_pairing_enforced(self, app: Flask) -> None:
        with app.app_context():
            db.session.add(
                GuildTenantPairing(
                    community_id=1,
                    discord_guild_id="guild-dup",
                    direction="bidirectional",
                    role_name_prefix="[BC]",
                )
            )
            db.session.commit()
            db.session.add(
                GuildTenantPairing(
                    community_id=1,
                    discord_guild_id="guild-dup",
                    direction="discord_to_twitch",
                    role_name_prefix="[BC2]",
                )
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_n_to_m_same_guild_multiple_communities_allowed(self, app: Flask) -> None:
        """A guild may be paired with more than one community (N:M, owner requirement)."""
        with app.app_context():
            db.session.execute(db.text("INSERT INTO communities (id, name) VALUES (2, 'other-bc')"))
            db.session.commit()
            db.session.add(
                GuildTenantPairing(
                    community_id=1,
                    discord_guild_id="shared-guild",
                    direction="twitch_to_discord",
                    role_name_prefix="[BC1]",
                )
            )
            db.session.add(
                GuildTenantPairing(
                    community_id=2,
                    discord_guild_id="shared-guild",
                    direction="bidirectional",
                    role_name_prefix="[BC2]",
                )
            )
            db.session.commit()

            rows = GuildTenantPairing.query.filter_by(discord_guild_id="shared-guild").all()
            assert {r.community_id for r in rows} == {1, 2}


class TestCommunityRoleSyncBindingRoundTrip:
    def _make_pairing(self) -> GuildTenantPairing:
        pairing = GuildTenantPairing(
            community_id=1,
            discord_guild_id="guild-sync",
            direction="twitch_to_discord",
            role_name_prefix="[BC]",
        )
        db.session.add(pairing)
        db.session.commit()
        return pairing

    def test_subscriber_tier_binding_round_trip(self, app: Flask) -> None:
        with app.app_context():
            pairing = self._make_pairing()
            binding = CommunityRoleSyncBinding(
                pairing_id=pairing.id,
                sync_scope="subscriber_tier",
                subscriber_tier=2,
                discord_role_id="role-t2",
            )
            db.session.add(binding)
            db.session.commit()

            fetched = CommunityRoleSyncBinding.query.filter_by(pairing_id=pairing.id).one()
            assert fetched.subscriber_tier == 2
            assert fetched.discord_role_id == "role-t2"

    def test_moderator_binding_round_trip(self, app: Flask) -> None:
        with app.app_context():
            pairing = self._make_pairing()
            binding = CommunityRoleSyncBinding(
                pairing_id=pairing.id,
                sync_scope="moderator",
                subscriber_tier=None,
                discord_role_id="role-mod",
            )
            db.session.add(binding)
            db.session.commit()

            fetched = CommunityRoleSyncBinding.query.filter_by(pairing_id=pairing.id).one()
            assert fetched.subscriber_tier is None
            assert fetched.sync_scope == "moderator"

    def test_tier_without_scope_mismatch_rejected(self, app: Flask) -> None:
        """A subscriber_tier value with sync_scope='moderator' violates the scope/tier check."""
        with app.app_context():
            pairing = self._make_pairing()
            db.session.add(
                CommunityRoleSyncBinding(
                    pairing_id=pairing.id,
                    sync_scope="moderator",
                    subscriber_tier=1,
                    discord_role_id="role-bad",
                )
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_subscriber_tier_requires_tier_value(self, app: Flask) -> None:
        with app.app_context():
            pairing = self._make_pairing()
            db.session.add(
                CommunityRoleSyncBinding(
                    pairing_id=pairing.id,
                    sync_scope="subscriber_tier",
                    subscriber_tier=None,
                    discord_role_id="role-bad",
                )
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_binding_references_its_pairing(self, app: Flask) -> None:
        """`ON DELETE CASCADE` is a Postgres-level FK action -- SQLite (this
        fixture) does not enforce FK actions by default, so the real cascade
        behavior is proven against Postgres in
        `alembic/tests/test_0034_bar_citizen_guild_pairing.py` instead. This
        test only checks the FK linkage itself round-trips correctly."""
        with app.app_context():
            pairing = self._make_pairing()
            binding = CommunityRoleSyncBinding(
                pairing_id=pairing.id,
                sync_scope="moderator",
                subscriber_tier=None,
                discord_role_id="role-cascade",
            )
            db.session.add(binding)
            db.session.commit()

            fetched = CommunityRoleSyncBinding.query.filter_by(discord_role_id="role-cascade").one()
            assert fetched.pairing_id == pairing.id
