"""SQLAlchemy models for Bar Citizen's guild-pairing + role-sync schema,
plus the three-layer platform-connection model (0035).

Mirrors `alembic/versions/0034_bar_citizen_guild_pairing.py` and
`alembic/versions/0035_connection_model_layers.py` column-for-column (see
each migration's own docstring for full design rationale) -- these
tables are hub-api-owned control-plane data, created and migrated only
by Alembic, never by `db.create_all()` in production. This module exists
for FK resolution + any ORM-level read access other flask_core-based
modules may need, same convention as `community.py`/`hub_user.py`/
`tenant.py`'s own stub models.
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
    """**Compatibility model (0035) -- mapped to a VIEW, not the base table.**

    `tenant_platform_credentials` was renamed to `tenant_platform_apps`
    by 0035; this name now resolves to a compatibility VIEW (with
    `INSTEAD OF` triggers forwarding writes to `tenant_platform_apps`)
    kept for the still-open, stacked PR #563 chain. Left unmodified
    (including this class name/tablename) so `test_0034_schema_drift.py`
    -- which statically compares 0034's own migration SQL against this
    model, unrelated to 0035 -- stays green. New code should use
    `TenantPlatformApp` instead; this class is removed once the #563
    chain is rebased off the new name (see 0035's migration docstring).
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


class TenantPlatformApp(db.Model):
    """Layer 1 (0035): one row per `(tenant_id, platform)` -- a tenant's own platform app credentials.

    Platform-generic: `credentials_ciphertext` holds a single AES-256-GCM
    encrypted JSON blob (client_id/secret + bot_token; shape is
    platform-defined), never per-platform columns. Tenant 0 (global)
    never has a row here -- enforced at the DB layer by
    `trg_reject_global_tenant_app_credentials`, not by this model. This
    is the renamed `tenant_platform_credentials` (see
    `TenantPlatformCredential` for the compatibility view of the old
    name). Tokens for a specific Discord guild/Twitch channel install
    live in `PlatformConnection`, never here.
    """
    __tablename__ = 'tenant_platform_apps'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'platform', name='tenant_platform_apps_tenant_id_platform_key'),
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


class PlatformConnection(db.Model):
    """Layer 2 (0035): the actual bot install/authorization into one Discord guild or Twitch channel.

    `access_token`/`refresh_token` (AES-256-GCM encrypted, same wire
    format as `TenantPlatformApp.credentials_ciphertext`) live ONLY
    here -- never in layer 1 or layer 3. Installed once per resource
    under a tenant's app (`UNIQUE (tenant_id, platform, resource_id)`).
    No FK to `TenantPlatformApp`: tenant 0 legitimately has connections
    via SaaS credentials with no app row at all.
    """
    __tablename__ = 'platform_connections'
    __table_args__ = (
        UniqueConstraint(
            'tenant_id', 'platform', 'resource_id',
            name='platform_connections_tenant_id_platform_resource_id_key',
        ),
        CheckConstraint(
            "resource_type IN ('discord_guild', 'twitch_channel')",
            name='platform_connections_resource_type_check',
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'expired')",
            name='platform_connections_status_check',
        ),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey('tenants.id'), nullable=False)
    platform = Column(String(50), nullable=False)
    resource_type = Column(String(20), nullable=False)
    resource_id = Column(String(255), nullable=False)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default='active')
    installed_by_user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CommunityConnectionAccess(db.Model):
    """Layer 3 (0035): which communities may leverage a `PlatformConnection`, and its approval state.

    NO tokens/credentials here -- grant only. First connect to a
    resource creates an already-approved row; a second community
    reusing the same connection creates a `pending` row requiring
    server/guild-admin approval before `approved`. `ON DELETE CASCADE`
    on `connection_id`: revoking/removing a connection removes every
    community's access to it.
    """
    __tablename__ = 'community_connection_access'
    __table_args__ = (
        UniqueConstraint(
            'community_id', 'connection_id',
            name='community_connection_access_community_id_connection_id_key',
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'revoked')",
            name='community_connection_access_status_check',
        ),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('communities.id'), nullable=False)
    connection_id = Column(
        Integer, ForeignKey('platform_connections.id', ondelete='CASCADE'), nullable=False
    )
    status = Column(String(20), nullable=False, default='pending')
    requested_by_user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
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
