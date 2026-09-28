"""SQLAlchemy models for video proxy and community calls."""
from datetime import datetime
from enum import Enum
from flask_core.models import db


class CallParticipantRole(str, Enum):
    """Participant role in community call."""
    HOST = "host"
    MODERATOR = "moderator"
    SPEAKER = "speaker"
    VIEWER = "viewer"


class VideoStreamConfig(db.Model):
    """Video stream configuration for community."""
    __tablename__ = 'video_stream_configs'
    
    id = db.Column(db.Integer, primary_key=True)
    stream_key = db.Column(db.String(255), unique=True, nullable=False, index=True)
    community_id = db.Column(db.Integer, nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    max_destinations = db.Column(db.Integer, default=5, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    destinations = db.relationship('VideoStreamDestination', back_populates='config', cascade='all, delete-orphan')
    sessions = db.relationship('VideoStreamSession', back_populates='config', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<VideoStreamConfig id={self.id} community={self.community_id} active={self.is_active}>'


class VideoStreamDestination(db.Model):
    """Video stream destination configuration."""
    __tablename__ = 'video_stream_destinations'
    
    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('video_stream_configs.id'), nullable=False)
    platform = db.Column(db.String(50), nullable=False)
    rtmp_url = db.Column(db.String(500), nullable=False)
    stream_key_encrypted = db.Column(db.Text, nullable=False)
    resolution = db.Column(db.String(20), default='1080p')
    force_cut = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    config = db.relationship('VideoStreamConfig', back_populates='destinations')
    
    def __repr__(self):
        return f'<VideoStreamDestination id={self.id} platform={self.platform} active={self.is_active}>'


class VideoStreamSession(db.Model):
    """Video stream session tracking."""
    __tablename__ = 'video_stream_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('video_stream_configs.id'), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    ended_at = db.Column(db.DateTime(timezone=True))
    viewer_count_peak = db.Column(db.Integer, default=0)
    status = db.Column(db.String(50), default='active', nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    config = db.relationship('VideoStreamConfig', back_populates='sessions')
    
    def __repr__(self):
        return f'<VideoStreamSession id={self.id} status={self.status} started={self.started_at}>'


class CommunityCallRoom(db.Model):
    """Community call room configuration."""
    __tablename__ = 'community_call_rooms'
    
    id = db.Column(db.Integer, primary_key=True)
    livekit_room_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    room_name = db.Column(db.String(255), nullable=False)
    community_id = db.Column(db.Integer, nullable=False, index=True)
    is_locked = db.Column(db.Boolean, default=False, nullable=False)
    recording_enabled = db.Column(db.Boolean, default=False, nullable=False)
    max_participants = db.Column(db.Integer, default=50, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    closed_at = db.Column(db.DateTime(timezone=True))
    
    # Relationships
    participants = db.relationship('CommunityCallParticipant', back_populates='room', cascade='all, delete-orphan')
    annotations = db.relationship('CallAnnotation', back_populates='room', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<CommunityCallRoom id={self.id} name={self.room_name} locked={self.is_locked}>'


class CommunityCallParticipant(db.Model):
    """Participant in community call."""
    __tablename__ = 'community_call_participants'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('community_call_rooms.id'), nullable=False)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    role = db.Column(db.Enum(CallParticipantRole), default=CallParticipantRole.VIEWER, nullable=False)
    joined_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    left_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    room = db.relationship('CommunityCallRoom', back_populates='participants')
    raised_hands = db.relationship('CallRaisedHand', back_populates='participant', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<CommunityCallParticipant id={self.id} user={self.user_id} role={self.role}>'


class CallRaisedHand(db.Model):
    """Raised hand in community call."""
    __tablename__ = 'call_raised_hands'
    
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('community_call_participants.id'), nullable=False)
    raised_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    acknowledged_at = db.Column(db.DateTime(timezone=True))
    acknowledged_by = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    participant = db.relationship('CommunityCallParticipant', back_populates='raised_hands')
    
    def __repr__(self):
        return f'<CallRaisedHand id={self.id} participant={self.participant_id} acknowledged={bool(self.acknowledged_at)}>'


class CallAnnotation(db.Model):
    """Annotation in community call."""
    __tablename__ = 'call_annotations'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('community_call_rooms.id'), nullable=False)
    user_id = db.Column(db.Integer, nullable=False)
    annotation_data = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    room = db.relationship('CommunityCallRoom', back_populates='annotations')
    
    def __repr__(self):
        return f'<CallAnnotation id={self.id} room={self.room_id} user={self.user_id}>'


class VideoFeatureUsage(db.Model):
    """Video feature usage tracking."""
    __tablename__ = 'video_feature_usage'
    
    id = db.Column(db.Integer, primary_key=True)
    community_id = db.Column(db.Integer, nullable=False, index=True)
    feature_type = db.Column(db.String(50), nullable=False)
    usage_count = db.Column(db.Integer, default=0, nullable=False)
    period_start = db.Column(db.DateTime(timezone=True), nullable=False)
    period_end = db.Column(db.DateTime(timezone=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    def __repr__(self):
        return f'<VideoFeatureUsage id={self.id} community={self.community_id} type={self.feature_type} count={self.usage_count}>'
