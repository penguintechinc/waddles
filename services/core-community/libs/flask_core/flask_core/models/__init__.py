"""SQLAlchemy models package for Waddles.

Defines the shared db instance and imports all model classes so that
Alembic's target_metadata sees every table.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Import all models AFTER db is defined to avoid circular imports.
# Each submodule does `from flask_core.models import db`.
from flask_core.models.auth import Role, User, roles_users  # noqa: E402
from flask_core.models.community import Community  # noqa: E402
from flask_core.models.hub_user import HubUser  # noqa: E402
from flask_core.models.engagement import (  # noqa: E402
    CommunityPoll, PollOption, PollVote,
    CommunityForm, FormField, FormSubmission, FormFieldValue,
)
from flask_core.models.video import (  # noqa: E402
    VideoStreamConfig, VideoStreamDestination, VideoStreamSession,
    CommunityCallRoom, CommunityCallParticipant, CallRaisedHand,
    CallAnnotation, VideoFeatureUsage,
)

__all__ = [
    'db',
    'Role', 'User', 'roles_users',
    'Community', 'HubUser',
    'CommunityPoll', 'PollOption', 'PollVote',
    'CommunityForm', 'FormField', 'FormSubmission', 'FormFieldValue',
    'VideoStreamConfig', 'VideoStreamDestination', 'VideoStreamSession',
    'CommunityCallRoom', 'CommunityCallParticipant', 'CallRaisedHand',
    'CallAnnotation', 'VideoFeatureUsage',
]
