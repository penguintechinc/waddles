"""SQLAlchemy models for Bar Citizen's guild-pairing + role-sync schema.

Mirrors `alembic/versions/0034_bar_citizen_guild_pairing.py` column-for-
column (see that migration's own docstring for the full design
rationale) -- these tables are hub-api-owned control-plane data, created
and migrated only by Alembic, never by `db.create_all()` in production.
This module exists for FK resolution + any ORM-level read access other
flask_core-based modules may need, same convention as `community.py`/
`hub_user.py`/`tenant.py`'s own stub models.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)

from flask_core.models import db


class TenantPlatformCredential(db.Model):
    """One row per `(tenant_id, platform)` -- a tenant's own platform app credentials.

    Platform-generic: `credentials_ciphertext` holds a single AES-256-GCM
    encrypted JSON blob (shape is platform-defined), never per-platform
    columns. Tenant 0 (global) never has a row here -- enforced at the
    DB layer by `trg_reject_global_tenant_credentials`, not by this model.
    """
    __tablename__ = 'tenant_platform_credentials'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'platform', name='tenant_platform_credentials_tenant_id_platform_key'),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    platform = Column(String(50), nullable=False)
    credentials_ciphertext = Column(Text, nullable=False)
    key_ref = Column(String(255), nullable=True)
    installed_by_user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GuildTenantPairing(db.Model):
    """N:M Discord guild <-> community pairing. Opt-in; admin-chosen sync direction."""
    __tablename__ = 'guild_tenant_pairings'
    __table_args__ = (
        UniqueConstraint('community_id', 'discord_guild_id', name='guild_tenant_pairings_community_id_discord_guild_id_key'),
        CheckConstraint(
            "direction IN ('discord_to_twitch', 'twitch_to_discord', 'bidirectional')",
            name='guild_tenant_pairings_direction_check',
        ),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('communities.id'), nullable=False)
    discord_guild_id = Column(String(255), nullable=False)
    direction = Column(String(20), nullable=False)
    sync_enabled = Column(Boolean, nullable=False, default=False)
    role_name_prefix = Column(String(50), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CommunityRoleSyncBinding(db.Model):
    """Maps one pairing's platform-role concept (sub tier / moderator) to a Discord role id."""
    __tablename__ = 'community_role_sync_bindings'
    __table_args__ = (
        CheckConstraint(
            "sync_scope IN ('subscriber_tier', 'moderator')",
            name='community_role_sync_bindings_sync_scope_check',
        ),
        CheckConstraint(
            "subscriber_tier IS NULL OR subscriber_tier IN (1, 2, 3)",
            name='community_role_sync_bindings_subscriber_tier_check',
        ),
        CheckConstraint(
            "(sync_scope = 'subscriber_tier' AND subscriber_tier IS NOT NULL) "
            "OR (sync_scope = 'moderator' AND subscriber_tier IS NULL)",
            name='chk_role_sync_binding_scope_tier',
        ),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    pairing_id = Column(Integer, ForeignKey('guild_tenant_pairings.id', ondelete='CASCADE'), nullable=False)
    sync_scope = Column(String(20), nullable=False)
    subscriber_tier = Column(SmallInteger, nullable=True)
    discord_role_id = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
