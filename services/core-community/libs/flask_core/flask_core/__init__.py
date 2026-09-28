"""
Waddles Flask Core Library
============================

Shared utilities for all Waddles Flask/Quart modules.

Provides:
- AsyncDAL: Async wrapper for penguin-dal database operations
- Auth utilities: Flask-Security-Too and OAuth integration
- Datamodels: Python 3.13 optimized dataclasses with slots
- Logging: Comprehensive AAA (Authentication, Authorization, Audit) logging
- API utilities: Standardized API responses and error handling
"""

__version__ = "2.0.0"
__author__ = "Waddles Team"

from .database import AsyncDAL, init_database
from .auth import setup_auth, OAuthProvider, create_jwt_token, verify_jwt_token, verify_service_key
from .datamodels import (
    CommandRequest,
    CommandResult,
    IdentityPayload,
    Activity,
    EventPayload,
    ModuleResponse
)
from .logging_config import setup_aaa_logging, get_logger
from .api_utils import (
    success_response,
    error_response,
    paginate_response,
    async_endpoint,
    auth_required,
    create_health_blueprint,
    record_request_metrics
)
from .cache import CacheManager, create_cache_manager
from .rate_limiter import RateLimiter, RateLimitExceeded, create_rate_limiter
from .message_queue import MessageQueue, Message, create_message_queue
from .stream_pipeline import StreamPipeline, StreamEvent, create_stream_pipeline
from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitBreakerManager,
    CircuitState,
    retry_with_backoff,
    with_retry
)
from .sharding import ConsistentHashRing, ChannelShardManager
from .read_replica import (
    ReadReplicaManager,
    ReadReplicaRouter,
    ReplicaConfig,
    ReplicaMetrics,
    ReplicaStatus,
    create_read_replica_manager
)
from .tracing import (
    TracingManager,
    create_tracing_manager,
    init_tracing,
    get_tracing_manager
)
from .correlation import (
    CorrelationIDManager,
    CorrelationIDFilter,
    CorrelationIDFormatter,
    create_correlation_manager,
    setup_correlation_logging,
    init_correlation,
    get_correlation_manager,
    get_correlation_id,
    get_request_id
)
from .custom_metrics import (
    MetricsManager,
    create_metrics_manager,
    init_metrics,
    get_metrics_manager
)
from .validation import (
    validate_json,
    validate_query,
    validate_form,
    validate_data,
    PaginationParams,
    CommunityIdRequired,
    UsernameRequired,
    DateRange,
    PlatformRequired,
    validate_email,
    validate_url,
    validate_username_format,
    validate_positive_integer,
    validate_non_negative_integer,
    BaseModel,
    Field,
    validator,
    ValidationError
)
from .sanitization import (
    sanitize_html,
    sanitize_input,
    sanitize_sql_like,
    strip_whitespace,
    sanitize_filename,
    sanitize_url,
    sanitize_json_string,
    truncate_text,
    sanitized_html_validator,
    sanitized_filename_validator,
    sanitized_url_validator,
    ALLOWED_TAGS,
    ALLOWED_ATTRIBUTES,
    ALLOWED_PROTOCOLS
)

__all__ = [
    # Database
    "AsyncDAL",
    "init_database",
    # Auth
    "setup_auth",
    "OAuthProvider",
    "create_jwt_token",
    "verify_jwt_token",
    "verify_service_key",
    # Datamodels
    "CommandRequest",
    "CommandResult",
    "IdentityPayload",
    "Activity",
    "EventPayload",
    "ModuleResponse",
    # Logging
    "setup_aaa_logging",
    "get_logger",
    # API Utils
    "success_response",
    "error_response",
    "paginate_response",
    "async_endpoint",
    "auth_required",
    # Health & Metrics
    "create_health_blueprint",
    "record_request_metrics",
    # Cache
    "CacheManager",
    "create_cache_manager",
    # Rate Limiting
    "RateLimiter",
    "RateLimitExceeded",
    "create_rate_limiter",
    # Message Queue
    "MessageQueue",
    "Message",
    "create_message_queue",
    # Stream Pipeline
    "StreamPipeline",
    "StreamEvent",
    "create_stream_pipeline",
    # Circuit Breaker & Resilience
    "CircuitBreaker",
    "CircuitBreakerError",
    "CircuitBreakerManager",
    "CircuitState",
    "retry_with_backoff",
    "with_retry",
    # Sharding
    "ConsistentHashRing",
    "ChannelShardManager",
    # Read Replicas
    "ReadReplicaManager",
    "ReadReplicaRouter",
    "ReplicaConfig",
    "ReplicaMetrics",
    "ReplicaStatus",
    "create_read_replica_manager",
    # Tracing & Observability
    "TracingManager",
    "create_tracing_manager",
    "init_tracing",
    "get_tracing_manager",
    # Correlation IDs
    "CorrelationIDManager",
    "CorrelationIDFilter",
    "CorrelationIDFormatter",
    "create_correlation_manager",
    "setup_correlation_logging",
    "init_correlation",
    "get_correlation_manager",
    "get_correlation_id",
    "get_request_id",
    # Custom Metrics
    "MetricsManager",
    "create_metrics_manager",
    "init_metrics",
    "get_metrics_manager",
    # Validation
    "validate_json",
    "validate_query",
    "validate_form",
    "validate_data",
    "PaginationParams",
    "CommunityIdRequired",
    "UsernameRequired",
    "DateRange",
    "PlatformRequired",
    "validate_email",
    "validate_url",
    "validate_username_format",
    "validate_positive_integer",
    "validate_non_negative_integer",
    "BaseModel",
    "Field",
    "validator",
    "ValidationError",
    # Sanitization
    "sanitize_html",
    "sanitize_input",
    "sanitize_sql_like",
    "strip_whitespace",
    "sanitize_filename",
    "sanitize_url",
    "sanitize_json_string",
    "truncate_text",
    "sanitized_html_validator",
    "sanitized_filename_validator",
    "sanitized_url_validator",
    "ALLOWED_TAGS",
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_PROTOCOLS",
]
