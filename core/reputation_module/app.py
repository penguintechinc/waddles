"""
Reputation Module - Quart Application
FICO-style automated reputation tracking (300-850 range, default 600)
"""
import os
import sys
import asyncio
import json

import grpc
from quart import Quart, Blueprint, request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))
from flask_core import (  # noqa: E402
    setup_aaa_logging,
    init_database,
    async_endpoint,
    success_response,
    error_response,
    create_health_blueprint,
    bind_community_read_tables,
    install_community_scoped_auth,
    install_rate_limiting,
    install_security_headers,
)
from flask_core.grpc_tls import bind_secure_port, default_server_options  # noqa: E402
from flask_core.tenancy import get_tenant_context  # noqa: E402
from config import Config  # noqa: E402
from services.reputation_service import ReputationService  # noqa: E402
from services.weight_manager import WeightManager  # noqa: E402
from services.event_processor import EventProcessor  # noqa: E402
from services.policy_enforcer import PolicyEnforcer  # noqa: E402
from services.grpc_handler import ReputationServiceServicer  # noqa: E402
from proto import reputation_pb2_grpc  # noqa: E402

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# API Blueprints
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
internal_bp = Blueprint('internal', __name__, url_prefix='/api/v1/internal')
admin_bp = Blueprint('admin', __name__, url_prefix='/api/v1/admin')

# SECURITY (C6, A01 -- unauthenticated access): `api_bp` (community/tenant
# reputation reads) and `admin_bp` (community-scoped reputation writes)
# had ZERO authentication -- `internal_bp` already gates on
# `_verify_service_key()` below, but these two did not. Registered once
# per blueprint so a route added later can't ship without this check by
# omission -- see flask_core.community_access module docstring.
install_community_scoped_auth(api_bp)
install_community_scoped_auth(admin_bp)

# SECURITY (A04): every route on `app` (api_bp, admin_bp, internal_bp) had
# zero rate limiting -- shared global before_request hook, see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
weight_manager = None
reputation_service = None
event_processor = None
policy_enforcer = None
grpc_server = None


def _verify_service_key():
    """Verify X-Service-Key header for internal endpoints."""
    import secrets
    if not Config.SERVICE_API_KEY:
        logger.error("SERVICE_API_KEY not configured - rejecting request", action="security")
        return False  # No key configured, reject all requests for security
    key = request.headers.get('X-Service-Key', '')
    # Use constant-time comparison to prevent timing attacks
    return secrets.compare_digest(key, Config.SERVICE_API_KEY)


@app.before_serving
async def startup():
    global dal, weight_manager, reputation_service, event_processor, policy_enforcer, grpc_server
    logger.system("Starting reputation_module", action="startup")

    dal = init_database(Config.DATABASE_URL)
    # SECURITY (C6): `async_dal`/`dal` under the two config keys
    # `install_community_scoped_auth`/`community_access` expect -- matches
    # `core/svc_streaming/app.py`'s established convention.
    app.config['async_dal'] = dal
    app.config['dal'] = dal.dal
    bind_community_read_tables(app.config['dal'], migrate=Config.DB_MIGRATE)

    # Initialize services
    weight_manager = WeightManager(dal, logger)
    reputation_service = ReputationService(dal, weight_manager, logger)
    event_processor = EventProcessor(
        reputation_service, weight_manager, None, logger
    )
    policy_enforcer = PolicyEnforcer(weight_manager, dal, logger)

    # Wire up policy enforcer to event processor
    event_processor.policy_enforcer = policy_enforcer

    # Initialize gRPC server
    grpc_server = grpc.aio.server(options=default_server_options())
    servicer = ReputationServiceServicer(reputation_service, event_processor)
    reputation_pb2_grpc.add_ReputationServiceServicer_to_server(servicer, grpc_server)

    grpc_server_address = f"0.0.0.0:{Config.GRPC_PORT}"

    # gRPC TLS mode decision (upfront, not fallback on failure)
    grpc_tls_insecure_flag = os.getenv('GRPC_TLS_INSECURE_DEV', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    dev_tiers = {'development', 'alpha', 'local', 'test'}
    use_insecure = grpc_tls_insecure_flag and Config.DEPLOYMENT_TIER in dev_tiers

    if use_insecure:
        # Deliberate insecure mode for dev tiers only
        logger.warning(
            "gRPC server starting in INSECURE mode",
            action="grpc_startup_mode",
            tier=Config.DEPLOYMENT_TIER,
            insecure_flag_set=grpc_tls_insecure_flag,
            note="Only valid for development/alpha/local/test"
        )
        grpc_server.add_insecure_port(grpc_server_address)
        await grpc_server.start()
        logger.system(
            "gRPC server started (insecure)",
            action="grpc_startup",
            port=Config.GRPC_PORT,
            address=grpc_server_address
        )
    elif grpc_tls_insecure_flag and Config.DEPLOYMENT_TIER not in dev_tiers:
        # Refuse insecure mode in production tiers even if flag is set
        logger.error(
            "GRPC_TLS_INSECURE_DEV set in non-dev tier (refusing startup)",
            action="grpc_security_violation",
            tier=Config.DEPLOYMENT_TIER
        )
        sys.exit(1)
    else:
        # TLS required: fail closed if setup fails
        try:
            bind_secure_port(grpc_server, grpc_server_address)
            await grpc_server.start()
            logger.system(
                "gRPC server started (TLS)",
                action="grpc_startup",
                port=Config.GRPC_PORT,
                address=grpc_server_address
            )
        except Exception as e:
            logger.error(
                f"gRPC TLS setup failed (fail closed): {e}",
                action="grpc_startup_error",
                tier=Config.DEPLOYMENT_TIER
            )
            sys.exit(1)

    logger.system("reputation_module started", result="SUCCESS")


@app.after_serving
async def shutdown():
    global grpc_server
    logger.system("Shutting down reputation_module", action="shutdown")

    # Shutdown gRPC server
    if grpc_server:
        await grpc_server.stop(grace=5)
        logger.system("gRPC server stopped", action="grpc_shutdown")

    if policy_enforcer:
        await policy_enforcer.close()
    logger.system("reputation_module shutdown complete", result="SUCCESS")


# =============================================================================
# Public API Endpoints (Authenticated)
# =============================================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "features": {
            "tenant_reputation": True,
            "custom_weights": "premium",
            "auto_ban": True,
            "giveaway_protection": True
        }
    })


@api_bp.route('/reputation/<int:community_id>/user/<int:user_id>')
@async_endpoint
async def get_user_reputation(community_id: int, user_id: int):
    """Get reputation for a user in a specific community."""
    info = await reputation_service.get_reputation(community_id, user_id)
    if not info:
        return error_response("User not found in community", 404)

    return success_response({
        "community_id": community_id,
        "user_id": user_id,
        "score": info.score,
        "tier": info.tier,
        "tier_label": info.tier_label,
        "total_events": info.total_events,
        "last_event_at": info.last_event_at
    })


@api_bp.route('/reputation/<int:community_id>/user/<int:user_id>/history')
@async_endpoint
async def get_user_history(community_id: int, user_id: int):
    """Get reputation event history for a user."""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)

    events = await reputation_service.get_history(
        community_id, user_id, limit=min(limit, 100), offset=offset
    )

    return success_response({
        "community_id": community_id,
        "user_id": user_id,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "score_change": e.score_change,
                "score_before": e.score_before,
                "score_after": e.score_after,
                "reason": e.reason,
                "created_at": e.created_at,
                "metadata": e.metadata
            }
            for e in events
        ],
        "limit": limit,
        "offset": offset
    })


@api_bp.route('/reputation/<int:community_id>/leaderboard')
@async_endpoint
async def get_leaderboard(community_id: int):
    """Get reputation leaderboard for a community."""
    limit = request.args.get('limit', 25, type=int)
    offset = request.args.get('offset', 0, type=int)

    leaderboard = await reputation_service.get_leaderboard(
        community_id, limit=min(limit, 100), offset=offset
    )

    return success_response({
        "community_id": community_id,
        "leaderboard": leaderboard,
        "limit": limit,
        "offset": offset
    })


@api_bp.route('/reputation/tenant/<int:user_id>')
@async_endpoint
async def get_tenant_reputation(user_id: int):
    """Get tenant-wide (cross-community, single-tenant) reputation for a user.

    `tenant_id` is read from `request.tenant_context` -- published by
    `install_community_scoped_auth` from the caller's bearer JWT `tenant`
    claim, never from a path/query parameter (security.md Tenant Isolation:
    tenant is never client-supplied).
    """
    ctx = get_tenant_context(request)
    if ctx is None:
        return error_response("Tenant context missing", 403)

    info = await reputation_service.get_tenant_reputation(ctx.tenant_id, user_id)
    if not info:
        return error_response("User not found", 404)

    return success_response({
        "user_id": user_id,
        "score": info.score,
        "tier": info.tier,
        "tier_label": info.tier_label,
        "total_events": info.total_events,
        "last_event_at": info.last_event_at
    })


@api_bp.route('/reputation/tenant/leaderboard')
@async_endpoint
async def get_tenant_leaderboard():
    """Get the tenant-wide reputation leaderboard for the caller's own tenant."""
    ctx = get_tenant_context(request)
    if ctx is None:
        return error_response("Tenant context missing", 403)

    limit = request.args.get('limit', 25, type=int)
    offset = request.args.get('offset', 0, type=int)

    leaderboard = await reputation_service.get_tenant_leaderboard(
        ctx.tenant_id, limit=min(limit, 100), offset=offset
    )

    return success_response({
        "leaderboard": leaderboard,
        "limit": limit,
        "offset": offset
    })


@api_bp.route('/reputation/tiers')
@async_endpoint
async def get_tiers():
    """Get reputation tier definitions."""
    return success_response({
        "tiers": Config.REPUTATION_TIERS,
        "min_score": Config.REPUTATION_MIN,
        "max_score": Config.REPUTATION_MAX,
        "default_score": Config.REPUTATION_DEFAULT
    })


@api_bp.route('/reputation/weights/<int:community_id>')
@async_endpoint
async def get_weights(community_id: int):
    """Get weight configuration for a community."""
    weights = await weight_manager.get_weights(community_id)

    return success_response({
        "community_id": community_id,
        "is_premium": weights.is_premium,
        "weights": {
            "chat_message": weights.chat_message,
            "command_usage": weights.command_usage,
            "giveaway_entry": weights.giveaway_entry,
            "follow": weights.follow,
            "subscription": weights.subscription,
            "subscription_tier2": weights.subscription_tier2,
            "subscription_tier3": weights.subscription_tier3,
            "gift_subscription": weights.gift_subscription,
            "donation_per_dollar": weights.donation_per_dollar,
            "cheer_per_100bits": weights.cheer_per_100bits,
            "raid": weights.raid,
            "boost": weights.boost,
            "warn": weights.warn,
            "timeout": weights.timeout,
            "kick": weights.kick,
            "ban": weights.ban
        },
        "policy": {
            "auto_ban_enabled": weights.auto_ban_enabled,
            "auto_ban_threshold": weights.auto_ban_threshold,
            "starting_score": weights.starting_score,
            "min_score": weights.min_score,
            "max_score": weights.max_score
        },
        "can_customize": weights.is_premium
    })


# =============================================================================
# Internal API Endpoints (Service-to-Service)
# =============================================================================

@internal_bp.route('/events', methods=['POST'])
@async_endpoint
async def receive_events():
    """
    Receive events from the router for reputation processing.

    Expected payload:
    {
        "events": [
            {
                "community_id": int,
                "user_id": int (optional),
                "platform": str,
                "platform_user_id": str,
                "event_type": str,
                "metadata": dict (optional)
            },
            ...
        ]
    }
    OR single event:
    {
        "community_id": int,
        ...
    }
    """
    if not _verify_service_key():
        return error_response("Unauthorized", 401)

    data = await request.get_json()
    if not data:
        return error_response("Invalid JSON", 400)

    # Handle single event or batch
    if 'events' in data:
        events = data['events']
    else:
        events = [data]

    result = await event_processor.process_batch(events)

    return success_response({
        "total": result.total,
        "processed": result.processed,
        "skipped": result.skipped,
        "failed": result.failed
    })


@internal_bp.route('/check/<int:community_id>/<int:user_id>')
@async_endpoint
async def quick_check(community_id: int, user_id: int):
    """Quick score check for internal services."""
    if not _verify_service_key():
        return error_response("Unauthorized", 401)

    info = await reputation_service.get_reputation(community_id, user_id)
    if not info:
        # Return default if not found
        return success_response({
            "score": Config.REPUTATION_DEFAULT,
            "tier": "fair",
            "found": False
        })

    return success_response({
        "score": info.score,
        "tier": info.tier,
        "found": True
    })


@internal_bp.route('/moderation', methods=['POST'])
@async_endpoint
async def process_moderation():
    """
    Process moderation action for reputation impact.

    Expected payload:
    {
        "community_id": int,
        "moderator_id": int,
        "target_user_id": int,
        "action": str (warn, timeout, kick, ban),
        "platform": str,
        "platform_user_id": str,
        "reason": str (optional),
        "duration": int (optional, for timeouts)
    }
    """
    if not _verify_service_key():
        return error_response("Unauthorized", 401)

    data = await request.get_json()
    if not data:
        return error_response("Invalid JSON", 400)

    required = ['community_id', 'action', 'platform', 'platform_user_id']
    for field in required:
        if field not in data:
            return error_response(f"Missing required field: {field}", 400)

    result = await event_processor.process_moderation_action(
        community_id=data['community_id'],
        moderator_id=data.get('moderator_id', 0),
        target_user_id=data.get('target_user_id'),
        action=data['action'],
        platform=data['platform'],
        platform_user_id=data['platform_user_id'],
        reason=data.get('reason'),
        duration=data.get('duration')
    )

    return success_response({
        "success": result.success,
        "score_before": result.score_before,
        "score_after": result.score_after,
        "score_change": result.score_change,
        "error": result.error
    })


# =============================================================================
# Admin API Endpoints (Community Admin)
# =============================================================================

@admin_bp.route('/<int:community_id>/reputation/<int:user_id>', methods=['PUT'])
@async_endpoint
async def set_user_reputation(community_id: int, user_id: int):
    """
    Manually set reputation score (admin action).

    Expected payload:
    {
        "score": int,
        "reason": str,
        "admin_id": int
    }
    """
    data = await request.get_json()
    if not data:
        return error_response("Invalid JSON", 400)

    if 'score' not in data:
        return error_response("Missing required field: score", 400)

    score = data['score']
    reason = data.get('reason', 'Manual adjustment by admin')
    admin_id = data.get('admin_id', 0)

    # Validate score range
    if score < Config.REPUTATION_MIN or score > Config.REPUTATION_MAX:
        return error_response(
            f"Score must be between {Config.REPUTATION_MIN} and {Config.REPUTATION_MAX}",
            400
        )

    result = await reputation_service.set_reputation(
        community_id, user_id, score, reason, admin_id
    )

    if not result.success:
        return error_response(result.error or "Failed to set reputation", 400)

    return success_response({
        "success": True,
        "score_before": result.score_before,
        "score_after": result.score_after,
        "score_change": result.score_change
    })


@admin_bp.route('/<int:community_id>/reputation/config', methods=['GET'])
@async_endpoint
async def get_reputation_config(community_id: int):
    """Get reputation configuration for a community."""
    weights = await weight_manager.get_weights(community_id)

    return success_response({
        "community_id": community_id,
        "is_premium": weights.is_premium,
        "weights": {
            "chat_message": weights.chat_message,
            "command_usage": weights.command_usage,
            "giveaway_entry": weights.giveaway_entry,
            "follow": weights.follow,
            "subscription": weights.subscription,
            "subscription_tier2": weights.subscription_tier2,
            "subscription_tier3": weights.subscription_tier3,
            "gift_subscription": weights.gift_subscription,
            "donation_per_dollar": weights.donation_per_dollar,
            "cheer_per_100bits": weights.cheer_per_100bits,
            "raid": weights.raid,
            "boost": weights.boost,
            "warn": weights.warn,
            "timeout": weights.timeout,
            "kick": weights.kick,
            "ban": weights.ban
        },
        "policy": {
            "auto_ban_enabled": weights.auto_ban_enabled,
            "auto_ban_threshold": weights.auto_ban_threshold,
            "starting_score": weights.starting_score,
            "min_score": weights.min_score,
            "max_score": weights.max_score
        },
        "can_customize": weights.is_premium
    })


@admin_bp.route('/<int:community_id>/reputation/config', methods=['PUT'])
@async_endpoint
async def update_reputation_config(community_id: int):
    """
    Update reputation configuration (PREMIUM only for weights).

    Expected payload:
    {
        "admin_id": int,
        "weights": { ... },  // Only applied if premium
        "policy": {
            "auto_ban_enabled": bool,
            "auto_ban_threshold": int
        }
    }
    """
    data = await request.get_json()
    if not data:
        return error_response("Invalid JSON", 400)

    admin_id = data.get('admin_id', 0)
    weights_data = data.get('weights', {})
    policy_data = data.get('policy', {})

    # Combine weights and policy for update
    update_data = {**weights_data, **policy_data}

    if not update_data:
        return success_response({"message": "No changes to apply"})

    # Check if premium for weight changes
    current_weights = await weight_manager.get_weights(community_id)

    if weights_data and not current_weights.is_premium:
        return error_response(
            "Weight customization requires premium subscription",
            403
        )

    success = await weight_manager.update_weights(community_id, update_data, admin_id)

    if not success:
        return error_response("Failed to update configuration", 400)

    return success_response({
        "success": True,
        "message": "Configuration updated"
    })


@admin_bp.route('/<int:community_id>/reputation/at-risk')
@async_endpoint
async def get_at_risk_users(community_id: int):
    """Get users who are close to auto-ban threshold."""
    buffer = request.args.get('buffer', 50, type=int)

    users = await policy_enforcer.get_at_risk_users(
        community_id, threshold_buffer=min(buffer, 100)
    )

    return success_response({
        "community_id": community_id,
        "at_risk_users": users,
        "count": len(users)
    })


@admin_bp.route('/<int:community_id>/reputation/auto-ban', methods=['POST'])
@async_endpoint
async def toggle_auto_ban(community_id: int):
    """
    Enable or disable auto-ban.

    Expected payload:
    {
        "enabled": bool,
        "threshold": int (optional, default 450),
        "admin_id": int
    }
    """
    data = await request.get_json()
    if not data:
        return error_response("Invalid JSON", 400)

    enabled = data.get('enabled', False)
    threshold = data.get('threshold', 450)
    admin_id = data.get('admin_id', 0)

    if enabled:
        success = await policy_enforcer.enable_auto_ban(
            community_id, admin_id, threshold
        )
    else:
        success = await policy_enforcer.disable_auto_ban(community_id, admin_id)

    if not success:
        return error_response("Failed to update auto-ban setting", 400)

    return success_response({
        "success": True,
        "auto_ban_enabled": enabled,
        "threshold": threshold if enabled else None
    })


@admin_bp.route('/<int:community_id>/reputation/defaults')
@async_endpoint
async def get_default_weights(community_id: int):
    """Get default weight values for reference."""
    defaults = weight_manager.get_default_weights()

    return success_response({
        "defaults": {
            "chat_message": defaults.chat_message,
            "command_usage": defaults.command_usage,
            "giveaway_entry": defaults.giveaway_entry,
            "follow": defaults.follow,
            "subscription": defaults.subscription,
            "subscription_tier2": defaults.subscription_tier2,
            "subscription_tier3": defaults.subscription_tier3,
            "gift_subscription": defaults.gift_subscription,
            "donation_per_dollar": defaults.donation_per_dollar,
            "cheer_per_100bits": defaults.cheer_per_100bits,
            "raid": defaults.raid,
            "boost": defaults.boost,
            "warn": defaults.warn,
            "timeout": defaults.timeout,
            "kick": defaults.kick,
            "ban": defaults.ban
        },
        "policy_defaults": {
            "auto_ban_enabled": False,
            "auto_ban_threshold": Config.REPUTATION_AUTO_BAN_THRESHOLD,
            "starting_score": Config.REPUTATION_DEFAULT,
            "min_score": Config.REPUTATION_MIN,
            "max_score": Config.REPUTATION_MAX
        }
    })


# Register blueprints
app.register_blueprint(api_bp)
app.register_blueprint(internal_bp)
app.register_blueprint(admin_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
