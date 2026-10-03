"""SQLAlchemy models package for Waddles.

Defines the shared db instance and imports all model classes so that
Alembic's target_metadata sees every table.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Import all models AFTER db is defined to avoid circular imports.
# Each submodule does `from flask_core.models import db`.
from flask_core.models.auth import Role, User, roles_users
from flask_core.models.community import Community
from flask_core.models.engagement import (
    CommunityForm,
    CommunityPoll,
    FormField,
    FormFieldValue,
    FormSubmission,
    PollOption,
    PollVote,
)
from flask_core.models.guild_pairing import (
    CommunityRoleSyncBinding,
    GuildTenantPairing,
    TenantPlatformCredential,
)
from flask_core.models.hub_user import HubUser
from flask_core.models.tenant import Tenant
from flask_core.models.video import (
    CallAnnotation,
    CallRaisedHand,
    CommunityCallParticipant,
    CommunityCallRoom,
    VideoFeatureUsage,
    VideoStreamConfig,
    VideoStreamDestination,
    VideoStreamSession,
)

__all__ = [
    'CallAnnotation',
    'CallRaisedHand',
    'Community',
    'CommunityCallParticipant',
    'CommunityCallRoom',
    'CommunityForm',
    'CommunityPoll',
    'CommunityRoleSyncBinding',
    'FormField',
    'FormFieldValue',
    'FormSubmission',
    'GuildTenantPairing',
    'HubUser',
    'PollOption',
    'PollVote',
    'Role',
    'Tenant',
    'TenantPlatformCredential',
    'User',
    'VideoFeatureUsage',
    'VideoStreamConfig',
    'VideoStreamDestination',
    'VideoStreamSession',
    'db',
    'roles_users',
]
