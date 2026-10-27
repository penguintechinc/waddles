"""Round-trip + constraint tests for the three-layer connection model (0035) SQLAlchemy models.

Mirrors `alembic/versions/0035_connection_model_layers.py`'s shape -- see
that migration's own docstring for full design rationale. Exercises the
models directly (in-memory SQLite via `db.create_all()`), independent of
the real-Postgres migration round-trip test
(`alembic/tests/test_0035_connection_model_layers.py`), which proves the
two stay in lockstep (`test_0035_schema_drift.py` cross-checks column
sets for the two genuinely new tables).
"""
from __future__ import annotations

import pytest
from flask import Flask
from flask_core.models import (
    CommunityConnectionAccess,
    PlatformConnection,
    TenantPlatformApp,
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
        db.session.execute(db.text("INSERT INTO communities (id, name) VALUES (2, 'other-bc')"))
        db.session.commit()
        yield flask_app
        db.session.remove()
        db.drop_all()


class TestTenantPlatformAppRoundTrip:
    def test_insert_and_read_back(self, app: Flask) -> None:
        with app.app_context():
            row = TenantPlatformApp(
                tenant_id=1, platform="discord", credentials_ciphertext="app-blob-1"
            )
            db.session.add(row)
            db.session.commit()

            fetched = TenantPlatformApp.query.filter_by(tenant_id=1, platform="discord").one()
            assert fetched.credentials_ciphertext == "app-blob-1"
            assert fetched.key_ref is None

    def test_unique_tenant_platform_enforced(self, app: Flask) -> None:
        with app.app_context():
            db.session.add(
                TenantPlatformApp(tenant_id=1, platform="twitch", credentials_ciphertext="a")
            )
            db.session.commit()
            db.session.add(
                TenantPlatformApp(tenant_id=1, platform="twitch", credentials_ciphertext="b")
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()


class TestPlatformConnectionRoundTrip:
    def _make_connection(self, *, resource_id: str = "guild-1") -> PlatformConnection:
        conn = PlatformConnection(
            tenant_id=1,
            platform="discord",
            resource_type="discord_guild",
            resource_id=resource_id,
            access_token="encrypted-access-token",
        )
        db.session.add(conn)
        db.session.commit()
        return conn

    def test_insert_and_read_back_defaults(self, app: Flask) -> None:
        with app.app_context():
            conn = self._make_connection()
            fetched = PlatformConnection.query.filter_by(resource_id="guild-1").one()
            assert fetched.access_token == "encrypted-access-token"
            assert fetched.refresh_token is None
            assert fetched.status == "active"
            assert fetched.id == conn.id

    def test_unique_tenant_platform_resource_enforced(self, app: Flask) -> None:
        with app.app_context():
            self._make_connection(resource_id="guild-dup")
            db.session.add(
                PlatformConnection(
                    tenant_id=1,
                    platform="discord",
                    resource_type="discord_guild",
                    resource_id="guild-dup",
                    access_token="another-token",
                )
            )
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_different_resource_same_tenant_allowed(self, app: Flask) -> None:
        with app.app_context():
            self._make_connection(resource_id="guild-a")
            self._make_connection(resource_id="guild-b")
            rows = PlatformConnection.query.filter_by(tenant_id=1).all()
            assert {r.resource_id for r in rows} == {"guild-a", "guild-b"}


class TestCommunityConnectionAccessRoundTrip:
    def _make_connection(self) -> PlatformConnection:
        conn = PlatformConnection(
            tenant_id=1,
            platform="discord",
            resource_type="discord_guild",
            resource_id="guild-access",
            access_token="encrypted-access-token",
        )
        db.session.add(conn)
        db.session.commit()
        return conn

    def test_pending_default_status(self, app: Flask) -> None:
        with app.app_context():
            conn = self._make_connection()
            access = CommunityConnectionAccess(community_id=1, connection_id=conn.id)
            db.session.add(access)
            db.session.commit()

            fetched = CommunityConnectionAccess.query.filter_by(
                community_id=1, connection_id=conn.id
            ).one()
            assert fetched.status == "pending"
            assert fetched.approved_by_user_id is None

    def test_approval_transition(self, app: Flask) -> None:
        with app.app_context():
            conn = self._make_connection()
            access = CommunityConnectionAccess(community_id=1, connection_id=conn.id)
            db.session.add(access)
            db.session.commit()

            access.status = "approved"
            access.approved_by_user_id = None  # no hub_users seeded in this fixture
            db.session.commit()

            fetched = db.session.get(CommunityConnectionAccess, access.id)
            assert fetched.status == "approved"

    def test_unique_community_connection_enforced(self, app: Flask) -> None:
        with app.app_context():
            conn = self._make_connection()
            db.session.add(CommunityConnectionAccess(community_id=1, connection_id=conn.id))
            db.session.commit()
            db.session.add(CommunityConnectionAccess(community_id=1, connection_id=conn.id))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_two_communities_can_share_one_connection(self, app: Flask) -> None:
        """Reuse: a second community references the SAME connection instead of installing again."""
        with app.app_context():
            conn = self._make_connection()
            db.session.add(
                CommunityConnectionAccess(community_id=1, connection_id=conn.id, status="approved")
            )
            db.session.add(
                CommunityConnectionAccess(community_id=2, connection_id=conn.id, status="pending")
            )
            db.session.commit()

            rows = CommunityConnectionAccess.query.filter_by(connection_id=conn.id).all()
            assert {r.community_id for r in rows} == {1, 2}
            statuses = {r.community_id: r.status for r in rows}
            assert statuses[1] == "approved"
            assert statuses[2] == "pending"

    def test_cascade_is_fk_declared(self, app: Flask) -> None:
        """`ON DELETE CASCADE` is a Postgres-level FK action -- SQLite (this fixture)
        does not enforce FK actions by default, so real cascade behavior is proven
        against Postgres in
        `alembic/tests/test_0035_connection_model_layers.py::TestCommunityConnectionAccess
        ::test_connection_delete_cascades_to_access`. This test only checks the FK
        linkage itself round-trips correctly."""
        with app.app_context():
            conn = self._make_connection()
            access = CommunityConnectionAccess(community_id=1, connection_id=conn.id)
            db.session.add(access)
            db.session.commit()

            fetched = CommunityConnectionAccess.query.filter_by(community_id=1).one()
            assert fetched.connection_id == conn.id
