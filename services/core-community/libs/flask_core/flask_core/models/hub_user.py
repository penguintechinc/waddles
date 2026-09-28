"""Minimal SQLAlchemy model for hub_users table.

The hub_users table is primarily managed by the Node.js hub-api.
This stub exists so SQLAlchemy FK references from engagement models
resolve correctly during Alembic migrations.
"""
from flask_core.models import db


class HubUser(db.Model):
    """Hub user stub for FK resolution."""
    __tablename__ = 'hub_users'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True)
    username = db.Column(db.String(255), unique=True)
