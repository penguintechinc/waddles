"""
Python 3.13 Optimized Dataclasses
==================================

Shared dataclasses used across all Waddles modules.
Optimized with slots=True for memory efficiency and performance.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum


class MessageType(str, Enum):
    """Message/event types"""
    CHAT_MESSAGE = "chatMessage"
    SLASH_COMMAND = "slashCommand"
    INTERACTION = "interaction"
    MODAL_SUBMIT = "modal_submit"
    BUTTON_CLICK = "button_click"
    SELECT_MENU = "select_menu"
    SUBSCRIPTION = "subscription"
    GIFT_SUBSCRIPTION = "gift_subscription"
    FOLLOW = "follow"
    DONATION = "donation"
    CHEER = "cheer"
    RAID = "raid"
    HOST = "host"
    SUBGIFT = "subgift"
    RESUB = "resub"
    REACTION = "reaction"
    MEMBER_JOIN = "member_join"
    MEMBER_LEAVE = "member_leave"
    VOICE_JOIN = "voice_join"
    VOICE_LEAVE = "voice_leave"
    VOICE_TIME = "voice_time"
    BOOST = "boost"
    BAN = "ban"
    KICK = "kick"
    TIMEOUT = "timeout"
    WARN = "warn"
    FILE_SHARE = "file_share"
    APP_MENTION = "app_mention"
    CHANNEL_JOIN = "channel_join"
    STREAM_ONLINE = "stream_online"
    STREAM_OFFLINE = "stream_offline"


class Platform(str, Enum):
    """Platform types"""
    TWITCH = "twitch"
    DISCORD = "discord"
    SLACK = "slack"
    UNKNOWN = "unknown"


class ResponseAction(str, Enum):
    """Module response action types"""
    CHAT = "chat"
    MEDIA = "media"
    TICKER = "ticker"
    GENERAL = "general"
    FORM = "form"


class ReputationTier(str, Enum):
    """FICO-style reputation tier classifications"""
    EXCEPTIONAL = "exceptional"  # 800-850
    VERY_GOOD = "very_good"      # 740-799
    GOOD = "good"                # 670-739
    FAIR = "fair"                # 580-669
    POOR = "poor"                # 300-579


class ReputationEventType(str, Enum):
    """Types of events that affect reputation"""
    CHAT_MESSAGE = "chat_message"
    COMMAND_USAGE = "command_usage"
    GIVEAWAY_ENTRY = "giveaway_entry"
    FOLLOW = "follow"
    SUBSCRIPTION = "subscription"
    SUBSCRIPTION_TIER2 = "subscription_tier2"
    SUBSCRIPTION_TIER3 = "subscription_tier3"
    GIFT_SUBSCRIPTION = "gift_subscription"
    DONATION = "donation"
    CHEER = "cheer"
    RAID = "raid"
    BOOST = "boost"
    WARN = "warn"
    TIMEOUT = "timeout"
    KICK = "kick"
    BAN = "ban"
    MANUAL_SET = "manual_set"
    AUTO_BAN = "auto_ban"


@dataclass(slots=True, frozen=True)
class CommandRequest:
    """
    Command request from platform to router.
    Immutable and memory-efficient with slots.
    """
    entity_id: str
    user_id: str
    message: str
    message_type: MessageType
    platform: Platform
    session_id: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    channel_id: Optional[str] = None
    server_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandResult:
    """
    Result of command execution.
    Mutable to allow updating during processing.
    """
    execution_id: str
    command_id: Optional[int]
    success: bool
    response_action: Optional[ResponseAction] = None
    response_data: Dict[str, Any] = field(default_factory=dict)
    processing_time_ms: int = 0
    error_message: Optional[str] = None
    session_id: Optional[str] = None
    retry_count: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True, frozen=True)
class IdentityPayload:
    """
    User identity information for context API.
    """
    platform: Platform
    platform_user_id: str
    platform_username: str
    entity_id: str
    waddlebot_user_id: Optional[str] = None
    display_name: Optional[str] = None
    is_verified: bool = False
    reputation_score: int = 0
    roles: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class Activity:
    """
    User activity for reputation tracking.
    """
    entity_id: str
    user_id: str
    username: str
    activity_type: MessageType
    platform: Platform
    amount: int  # Points value
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class EventPayload:
    """
    Generic event payload for platform events.
    """
    event_id: str
    event_type: MessageType
    platform: Platform
    entity_id: str
    user_id: str
    username: str
    display_name: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    processed: bool = False
    error_message: Optional[str] = None


@dataclass(slots=True)
class ModuleResponse:
    """
    Response from interaction module back to router.
    """
    execution_id: str
    session_id: str
    module_name: str
    success: bool
    response_action: ResponseAction
    response_data: Dict[str, Any] = field(default_factory=dict)
    media_type: Optional[str] = None
    media_url: Optional[str] = None
    ticker_text: Optional[str] = None
    ticker_duration: int = 10
    chat_message: Optional[str] = None
    error_message: Optional[str] = None
    processing_time_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True, frozen=True)
class OAuthToken:
    """
    OAuth token storage.
    """
    platform: Platform
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[datetime] = None
    scope: List[str] = field(default_factory=list)


@dataclass(slots=True)
class BrowserSourceMessage:
    """
    Message to browser source via WebSocket.
    """
    source_type: str  # 'ticker', 'media', 'general'
    action: str  # 'display', 'update', 'clear'
    content: Dict[str, Any]
    duration: int = 10
    priority: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True, frozen=True)
class APIKeyInfo:
    """
    API key information.
    """
    key_id: str
    name: str
    user_id: str
    key_hash: str
    permissions: List[str]
    is_active: bool
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


@dataclass(slots=True)
class RateLimitInfo:
    """
    Rate limit tracking information.
    """
    key: str  # Unique identifier for rate limit
    window_start: datetime
    request_count: int
    limit: int
    window_seconds: int

    def is_exceeded(self) -> bool:
        """Check if rate limit is exceeded"""
        return self.request_count >= self.limit

    def reset_if_expired(self, now: Optional[datetime] = None) -> None:
        """Reset counter if window expired"""
        now = now or datetime.utcnow()
        window_end = self.window_start.timestamp() + self.window_seconds
        if now.timestamp() >= window_end:
            self.window_start = now
            self.request_count = 0


@dataclass(slots=True, frozen=True)
class CoordinationClaim:
    """
    Coordination system claim for horizontal scaling.
    """
    entity_id: str
    platform: Platform
    server_id: Optional[str]
    channel_id: Optional[str]
    claimed_by: str  # Container ID
    claimed_at: datetime
    claim_expires: datetime
    heartbeat_interval: int = 300  # 5 minutes
    is_live: bool = False
    viewer_count: int = 0
    priority: int = 0


@dataclass(slots=True)
class HealthCheckResult:
    """
    Health check result for module monitoring.
    """
    module_name: str
    status: str  # 'healthy', 'degraded', 'unhealthy'
    version: str
    uptime_seconds: int
    database_connected: bool
    redis_connected: bool
    timestamp: datetime = field(default_factory=datetime.utcnow)
    details: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass(slots=True)
class MetricsSnapshot:
    """
    Performance metrics snapshot.
    """
    module_name: str
    requests_total: int
    requests_success: int
    requests_failed: int
    avg_response_time_ms: float
    p95_response_time_ms: float
    p99_response_time_ms: float
    active_connections: int
    cache_hit_rate: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True, frozen=True)
class ReputationInfo:
    """
    FICO-style reputation information for a user.
    Score range: 300-850, default 600.
    """
    score: int
    tier: ReputationTier
    tier_label: str
    total_events: int = 0
    last_event_at: Optional[datetime] = None

    @staticmethod
    def get_tier(score: int) -> tuple:
        """Get tier from score."""
        if score >= 800:
            return ReputationTier.EXCEPTIONAL, "Exceptional"
        elif score >= 740:
            return ReputationTier.VERY_GOOD, "Very Good"
        elif score >= 670:
            return ReputationTier.GOOD, "Good"
        elif score >= 580:
            return ReputationTier.FAIR, "Fair"
        else:
            return ReputationTier.POOR, "Poor"


@dataclass(slots=True)
class ReputationEvent:
    """
    A single reputation adjustment event.
    """
    event_id: int
    community_id: int
    user_id: int
    event_type: ReputationEventType
    score_change: float
    score_before: int
    score_after: int
    platform: Platform
    platform_user_id: str
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(slots=True)
class ReputationAdjustment:
    """
    Result of a reputation adjustment operation.
    """
    success: bool
    score_before: int
    score_after: int
    score_change: float
    event_id: Optional[int] = None
    error: Optional[str] = None


@dataclass(slots=True, frozen=True)
class ReputationWeights:
    """
    Weight configuration for reputation scoring.
    Premium communities can customize; non-premium use defaults.
    """
    community_id: int
    is_premium: bool = False
    # Activity weights
    chat_message: float = 0.01
    command_usage: float = -0.1
    giveaway_entry: float = -1.0
    follow: float = 1.0
    subscription: float = 5.0
    subscription_tier2: float = 10.0
    subscription_tier3: float = 20.0
    gift_subscription: float = 3.0
    donation_per_dollar: float = 1.0
    cheer_per_100bits: float = 1.0
    raid: float = 2.0
    boost: float = 5.0
    # Moderation weights
    warn: float = -25.0
    timeout: float = -50.0
    kick: float = -75.0
    ban: float = -200.0
    # Policy settings
    auto_ban_enabled: bool = False
    auto_ban_threshold: int = 450
    starting_score: int = 600
    min_score: int = 300
    max_score: int = 850


# Type aliases for Python 3.13
type CommandHandler = callable[[CommandRequest], CommandResult]
type AsyncCommandHandler = callable[[CommandRequest], CommandResult]
type EventHandler = callable[[EventPayload], None]
type AsyncEventHandler = callable[[EventPayload], None]
