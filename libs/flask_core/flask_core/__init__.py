"""
Waddles Flask Core Library
============================

Shared utilities for all Waddles Flask/Quart modules.

Provides:
- AsyncDAL: Async wrapper for PyDAL database operations
- Auth utilities: Flask-Security-Too and OAuth integration
- Datamodels: Python 3.13 optimized dataclasses with slots
- Logging: Comprehensive AAA (Authentication, Authorization, Audit) logging
- API utilities: Standardized API responses and error handling
"""

__author__ = "Waddles Team"

from .platform_version import get_platform_version, platform_version_compatible

# Derived from flask_core/VERSION (see platform_version.py) rather than a
# second hardcoded literal -- avoids the exact staleness bug this module
# previously had (__version__ pinned at "2.0.0" long after the repo moved
# to release/v3.0.X).
__version__ = get_platform_version()

from .database import AsyncDAL, db_operation, init_database, install_db_resilience
from .bundle_runtime import (
    BundleContext,
    BundleRuntimeError,
    bundle_context,
    get_bundle_context,
    get_bundle_dal,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from .auth import (
    setup_auth,
    OAuthProvider,
    create_jwt_token,
    verify_jwt_token,
    verify_service_key,
    setup_default_roles,
    DEFAULT_TENANT_SLUG,
    TENANT_CLAIM_MIGRATION_CUTOFF,
    SCOPE_BUNDLES,
)
from .tenancy import (
    TenantContext,
    TenantIsolationError,
    tenant_middleware,
    tenant_scoped,
    resolve_tenant_context,
    get_tenant_context,
)
from .authz import (
    require_scope,
    has_required_scopes,
)
from .community_access import (
    CallerIdentityError,
    CommunityAccessError,
    DEFAULT_ADMIN_METHODS,
    bind_shared_read_tables as bind_community_read_tables,
    decode_caller_user_id,
    install_community_scoped_auth,
    require_admin as require_community_admin,
    require_member as require_community_member,
)
from .datamodels import (
    CommandRequest,
    CommandResult,
    IdentityPayload,
    Activity,
    EventPayload,
    ModuleResponse
)
from .logging_config import setup_aaa_logging, get_logger
from .feature_flags import feature_enabled
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
from .http_rate_limit import (
    DEFAULT_EXEMPT_PATHS,
    RATE_LIMITER_CONFIG_KEY,
    install_rate_limiting,
)
from .message_queue import MessageQueue, Message, create_message_queue
from .stream_pipeline import (
    StreamPipeline,
    StreamEvent,
    create_stream_pipeline,
    PlatformEvent,
    StageEnvelope,
    EnvelopeError,
    PROCESS_TARGET_APP_ID_KEY,
    Trace,
    Binding,
    ENVELOPE_SCHEMA_VERSION,
)
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
from .workload_identity import (
    IdentityProvider,
    SpiffeIdMatcher,
    SpiffeId,
    TrustDomain,
    MtlsConfig,
    WorkloadApiSource,
    RealWorkloadApiSource,
    IdentityError,
    Degraded,
    WorkloadApiUnavailable,
    PeerNotAuthorized,
    EmptyTrustBundle,
    NoDefaultSvid,
    is_production,
)
from .secrets import (
    require_secret_key,
    InsecureSecretError,
    KNOWN_PLACEHOLDER_SECRETS,
)
from .grpc_tls import (
    GrpcTlsConfigError,
    server_credentials as grpc_server_credentials,
    channel_credentials as grpc_channel_credentials,
    bind_secure_port as grpc_bind_secure_port,
    secure_channel as grpc_secure_channel,
    default_server_options as grpc_default_server_options,
    DEFAULT_MAX_MESSAGE_BYTES as GRPC_DEFAULT_MAX_MESSAGE_BYTES,
)
from .security_headers import (
    install_security_headers,
    SecurityHeadersConfig,
    DEFAULT_CSP,
    OVERLAY_CSP,
)

__all__ = [
    # Platform version / App Bundle compatibility
    "get_platform_version",
    "platform_version_compatible",
    # Database
    "AsyncDAL",
    "db_operation",
    "init_database",
    "install_db_resilience",
    # Bundle runtime (DAL + tenant/community context for stateful App Bundles)
    "BundleContext",
    "BundleRuntimeError",
    "bundle_context",
    "get_bundle_context",
    "get_bundle_dal",
    "reset_bundle_dal_for_tests",
    "set_bundle_dal",
    # Auth
    "setup_auth",
    "OAuthProvider",
    "create_jwt_token",
    "verify_jwt_token",
    "verify_service_key",
    "setup_default_roles",
    "DEFAULT_TENANT_SLUG",
    "TENANT_CLAIM_MIGRATION_CUTOFF",
    "SCOPE_BUNDLES",
    # Tenancy
    "TenantContext",
    "TenantIsolationError",
    "tenant_middleware",
    "tenant_scoped",
    "resolve_tenant_context",
    "get_tenant_context",
    # Scope Enforcement (HTTP layer)
    "require_scope",
    "has_required_scopes",
    # Community-scoped authorization (BOLA/IDOR fix for community_id)
    "CallerIdentityError",
    "CommunityAccessError",
    "DEFAULT_ADMIN_METHODS",
    "bind_community_read_tables",
    "decode_caller_user_id",
    "install_community_scoped_auth",
    "require_community_admin",
    "require_community_member",
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
    # Feature Flags
    "feature_enabled",
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
    "DEFAULT_EXEMPT_PATHS",
    "RATE_LIMITER_CONFIG_KEY",
    "install_rate_limiting",
    # Message Queue
    "MessageQueue",
    "Message",
    "create_message_queue",
    # Stream Pipeline
    "StreamPipeline",
    "StreamEvent",
    "create_stream_pipeline",
    # Frozen typed stage-to-stage pipeline contract
    "PlatformEvent",
    "StageEnvelope",
    "EnvelopeError",
    "PROCESS_TARGET_APP_ID_KEY",
    "Trace",
    "Binding",
    "ENVELOPE_SCHEMA_VERSION",
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
    # Workload identity (SPIFFE/SPIRE)
    "IdentityProvider",
    "SpiffeIdMatcher",
    "SpiffeId",
    "TrustDomain",
    "MtlsConfig",
    "WorkloadApiSource",
    "RealWorkloadApiSource",
    "IdentityError",
    "Degraded",
    "WorkloadApiUnavailable",
    "PeerNotAuthorized",
    "EmptyTrustBundle",
    "NoDefaultSvid",
    "is_production",
    # Fail-closed secret loading (C1)
    "require_secret_key",
    "InsecureSecretError",
    "KNOWN_PLACEHOLDER_SECRETS",
    # gRPC transport TLS (A02)
    "GrpcTlsConfigError",
    "grpc_server_credentials",
    "grpc_channel_credentials",
    "grpc_bind_secure_port",
    "grpc_secure_channel",
    "grpc_default_server_options",
    "GRPC_DEFAULT_MAX_MESSAGE_BYTES",
    # Security headers (A05 hardening)
    "install_security_headers",
    "SecurityHeadersConfig",
    "DEFAULT_CSP",
    "OVERLAY_CSP",
]
