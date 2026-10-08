"""Minimal SQLAlchemy model for the tenants table.

The tenants table is owned by Alembic migrations (`058_tenants_and_claims.sql`,
`hub_api/services/schema.py::bind_tenant_tables`), never created/migrated
by this process. This stub exists so SQLAlchemy FK references from
guild-pairing models resolve correctly during Alembic autogenerate /
`create_all()`-based tests, matching `community.py`/`hub_user.py`'s own
stub convention.
"""
from flask_core.models import db


class Tenant(db.Model):
    """Tenant stub for FK resolution."""
    __tablename__ = 'tenants'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True)
    is_global = db.Column(db.Boolean, default=False)
