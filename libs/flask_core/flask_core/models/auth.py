"""SQLAlchemy models for the legacy `auth_user` / `auth_role` tables.

Flask-Security-Too (and its sdist-only, unmaintained `flask-principal` dependency) was
dropped: only its `UserMixin`/`RoleMixin` were ever imported -- nothing here uses the
Security extension. The two small mixins below keep the same attribute surface for the
columns these models declare, so the Alembic metadata is unchanged.
"""
from typing import Any

from flask_core.models import db


class RoleMixin:
    """Role behaviour shared by `Role`: name-based equality and permission lookup."""

    name: str | None
    permissions: Any

    def __eq__(self, other: object) -> bool:
        """Roles compare equal to another role or to a bare role name."""
        return self.name == other or self.name == getattr(other, "name", None)

    def __ne__(self, other: object) -> bool:
        """Inverse of `__eq__`."""
        return not self.__eq__(other)

    def __hash__(self) -> int:
        """Hash by role name so roles stay usable in sets/dicts."""
        return hash(self.name)

    def get_permissions(self) -> set[str]:
        """Return the role's permission scopes as a set (empty when none are stored)."""
        return set(self.permissions or [])


class UserMixin:
    """User behaviour shared by `User`: session-identity flags and role checks."""

    active: bool | None
    fs_uniquifier: str | None
    roles: Any

    @property
    def is_active(self) -> bool:
        """An account is active unless its `active` column is explicitly false."""
        return self.active is not False

    @property
    def is_authenticated(self) -> bool:
        """A loaded user row is an authenticated principal."""
        return True

    @property
    def is_anonymous(self) -> bool:
        """A loaded user row is never anonymous."""
        return False

    def get_id(self) -> str:
        """Stable session identifier -- the per-user uniquifier."""
        return str(self.fs_uniquifier)

    def has_role(self, role: Any) -> bool:
        """True when the user holds `role` (a `Role` or a role name)."""
        name = getattr(role, "name", role)
        return any(r.name == name for r in self.roles)


roles_users = db.Table('auth_user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('auth_user.id')),
    db.Column('role_id', db.Integer, db.ForeignKey('auth_role.id'))
)


class Role(db.Model, RoleMixin):
    __tablename__ = 'auth_role'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))
    permissions = db.Column(db.JSON)


class User(db.Model, UserMixin):
    __tablename__ = 'auth_user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    username = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    active = db.Column(db.Boolean, default=True)
    fs_uniquifier = db.Column(db.String(64), unique=True, nullable=False)
    confirmed_at = db.Column(db.DateTime)
    roles = db.relationship(
        'Role', secondary=roles_users,
        backref=db.backref('users', lazy='dynamic')
    )
