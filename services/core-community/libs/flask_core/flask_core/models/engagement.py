"""
Engagement models for community polls and forms.
"""
import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from flask_core.models import db


class VisibilityLevel(enum.Enum):
    """Visibility levels for polls and forms."""
    PUBLIC = "public"
    REGISTERED = "registered"
    COMMUNITY = "community"
    ADMINS = "admins"


class FieldType(enum.Enum):
    """Form field types."""
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    NUMBER = "number"
    SELECT = "select"
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    DATE = "date"
    DATETIME = "datetime"


class CommunityPoll(db.Model):
    """Community poll model."""
    __tablename__ = 'community_polls'
    
    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('communities.id'), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    view_visibility = Column(SQLEnum(VisibilityLevel), nullable=False, default=VisibilityLevel.PUBLIC)
    submit_visibility = Column(SQLEnum(VisibilityLevel), nullable=False, default=VisibilityLevel.REGISTERED)
    allow_multiple_choices = Column(Boolean, default=False)
    max_choices = Column(Integer, default=1)
    expires_at = Column(DateTime)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    community = relationship("Community", backref="polls")
    options = relationship("PollOption", back_populates="poll", cascade="all, delete-orphan")
    votes = relationship("PollVote", back_populates="poll", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<CommunityPoll(id={self.id}, title='{self.title}', community_id={self.community_id})>"


class PollOption(db.Model):
    """Poll option model."""
    __tablename__ = 'poll_options'
    
    id = Column(Integer, primary_key=True)
    poll_id = Column(Integer, ForeignKey('community_polls.id'), nullable=False)
    option_text = Column(String(500), nullable=False)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    poll = relationship("CommunityPoll", back_populates="options")
    votes = relationship("PollVote", back_populates="option", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<PollOption(id={self.id}, poll_id={self.poll_id}, text='{self.option_text}')>"


class PollVote(db.Model):
    """Poll vote model."""
    __tablename__ = 'poll_votes'
    
    id = Column(Integer, primary_key=True)
    poll_id = Column(Integer, ForeignKey('community_polls.id'), nullable=False)
    option_id = Column(Integer, ForeignKey('poll_options.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    ip_hash = Column(String(64))  # SHA-256 hash for anonymous deduplication
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    poll = relationship("CommunityPoll", back_populates="votes")
    option = relationship("PollOption", back_populates="votes")
    user = relationship("HubUser")
    
    def __repr__(self):
        return f"<PollVote(id={self.id}, poll_id={self.poll_id}, option_id={self.option_id}, user_id={self.user_id})>"


class CommunityForm(db.Model):
    """Community form model."""
    __tablename__ = 'community_forms'
    
    id = Column(Integer, primary_key=True)
    community_id = Column(Integer, ForeignKey('communities.id'), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    view_visibility = Column(SQLEnum(VisibilityLevel), nullable=False, default=VisibilityLevel.PUBLIC)
    submit_visibility = Column(SQLEnum(VisibilityLevel), nullable=False, default=VisibilityLevel.REGISTERED)
    allow_anonymous = Column(Boolean, default=False)
    submit_once_per_user = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    community = relationship("Community", backref="forms")
    fields = relationship("FormField", back_populates="form", cascade="all, delete-orphan")
    submissions = relationship("FormSubmission", back_populates="form", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<CommunityForm(id={self.id}, title='{self.title}', community_id={self.community_id})>"


class FormField(db.Model):
    """Form field model."""
    __tablename__ = 'form_fields'
    
    id = Column(Integer, primary_key=True)
    form_id = Column(Integer, ForeignKey('community_forms.id'), nullable=False)
    field_type = Column(SQLEnum(FieldType), nullable=False)
    label = Column(String(255), nullable=False)
    placeholder = Column(String(255))
    is_required = Column(Boolean, default=False)
    options_json = Column(JSON)  # For select, multiselect, radio options
    validation_json = Column(JSON)  # Custom validation rules
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    form = relationship("CommunityForm", back_populates="fields")
    field_values = relationship("FormFieldValue", back_populates="field", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<FormField(id={self.id}, form_id={self.form_id}, label='{self.label}', type={self.field_type.value})>"


class FormSubmission(db.Model):
    """Form submission model."""
    __tablename__ = 'form_submissions'
    
    id = Column(Integer, primary_key=True)
    form_id = Column(Integer, ForeignKey('community_forms.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('hub_users.id'), nullable=True)
    ip_hash = Column(String(64))  # SHA-256 hash for anonymous deduplication
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    form = relationship("CommunityForm", back_populates="submissions")
    user = relationship("HubUser")
    field_values = relationship("FormFieldValue", back_populates="submission", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<FormSubmission(id={self.id}, form_id={self.form_id}, user_id={self.user_id})>"


class FormFieldValue(db.Model):
    """Form field value model."""
    __tablename__ = 'form_field_values'
    
    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey('form_submissions.id'), nullable=False)
    field_id = Column(Integer, ForeignKey('form_fields.id'), nullable=False)
    value_text = Column(Text)
    value_json = Column(JSON)  # For complex field types
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    submission = relationship("FormSubmission", back_populates="field_values")
    field = relationship("FormField", back_populates="field_values")
    
    def __repr__(self):
        return f"<FormFieldValue(id={self.id}, submission_id={self.submission_id}, field_id={self.field_id})>"
