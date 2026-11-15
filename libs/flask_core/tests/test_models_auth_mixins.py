"""`flask_core.models.auth` carries its own User/Role mixins (flask-security-too was dropped)."""

from __future__ import annotations

import sys

from flask_core.models import Role, User, db, roles_users


def test_flask_security_is_not_imported_or_required() -> None:
    """The models import cleanly without flask_security / flask_principal in the process."""
    assert "flask_security" not in sys.modules
    assert "flask_principal" not in sys.modules


def test_metadata_still_declares_the_legacy_auth_tables() -> None:
    """Dropping the mixin base classes must not change the Alembic-visible schema."""
    assert User.__table__.name == "auth_user"
    assert Role.__table__.name == "auth_role"
    assert roles_users.name == "auth_user_roles"
    assert {c.name for c in User.__table__.columns} == {
        "id",
        "email",
        "username",
        "password",
        "active",
        "fs_uniquifier",
        "confirmed_at",
    }
    assert {c.name for c in Role.__table__.columns} == {"id", "name", "description", "permissions"}
    assert {"auth_user", "auth_role", "auth_user_roles"} <= set(db.metadata.tables)


def test_user_mixin_session_identity_and_role_checks() -> None:
    """Active flag, authenticated/anonymous flags, session id and role membership behave."""
    admin = Role(name="admin", permissions=["*:admin"])
    user = User(email="u@example.test", active=True, fs_uniquifier="uniq-1", roles=[admin])
    assert user.is_active is True
    assert user.is_authenticated is True
    assert user.is_anonymous is False
    assert user.get_id() == "uniq-1"
    assert user.has_role("admin") is True
    assert user.has_role(admin) is True
    assert user.has_role("viewer") is False
    assert User(active=False, fs_uniquifier="x").is_active is False
    assert User(active=None, fs_uniquifier="x").is_active is True  # column default is TRUE


def test_role_mixin_equality_hash_and_permissions() -> None:
    """Roles compare by name (to roles or bare names), hash by name, expose permissions."""
    a = Role(name="admin", permissions=["a:read", "a:write"])
    assert a == Role(name="admin")
    assert a == "admin"
    assert a != Role(name="viewer")
    assert a != "viewer"
    assert hash(a) == hash(Role(name="admin"))
    assert a.get_permissions() == {"a:read", "a:write"}
    assert Role(name="empty").get_permissions() == set()
