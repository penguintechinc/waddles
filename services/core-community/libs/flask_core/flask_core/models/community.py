"""Minimal SQLAlchemy model for communities table.

The communities table is primarily managed by the Node.js hub-api.
This stub exists so SQLAlchemy FK references from engagement/video
models resolve correctly during Alembic migrations.
"""
from flask_core.models import db


class Community(db.Model):
    """Community stub for FK resolution."""
    __tablename__ = 'communities'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
