"""Security Core Module - Main Application.

Provides spam detection, content filtering, warnings, and cross-platform moderation.
"""
import asyncio
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs')
)

from flask_core import (
    bind_community_read_tables,
    create_health_blueprint,
    error_response,
    init_database,
    install_community_scoped_auth,
    install_rate_limiting,
    install_security_headers,
    setup_aaa_logging,
    success_response,
    verify_service_key,
)
from quart import Blueprint, Quart, request

from config import Config

# Create Quart app
app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Health blueprint
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# API blueprints
api_bp = Blueprint('api', __name__, url_prefix='/api/v1/security')
internal_bp = Blueprint('internal', __name__, url_prefix='/api/v1/internal')

# SECURITY (C6, A01 -- BOLA/unauthenticated access): every api_bp route is
# tenant + community-membership scoped, not just tenant-scoped -- a caller
# holding any valid tenant JWT could otherwise read/write ANY community's
# warnings, blocked words, and moderation log by supplying an arbitrary
# `community_id` in the URL. Registered once for the whole blueprint (not
# per-route) so a route added later can't ship without this check by
# omission -- see flask_core.community_access module docstring. Mutating
# verbs (POST/PUT/PATCH/DELETE) require community-admin; GET requires only
# active membership -- flask_core's DEFAULT_ADMIN_METHODS.
install_community_scoped_auth(api_bp)

# SECURITY (A04): every route on `app` (api_bp, internal_bp) had zero rate
# limiting -- shared global before_request hook, see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)


@internal_bp.before_request
async def _require_internal_service_key():
    """Gate every internal_bp (service-to-service) route on X-Service-Key.

    SECURITY (C6): these three routes (`check`, `warn`, `sync-action`) had
    ZERO authentication -- any caller reaching this service's network
    address could inject fabricated moderation events. `verify_service_key`
    is the same constant-time, fail-closed-if-unconfigured helper already
    used elsewhere in this codebase for internal service-to-service calls
    (flask_core.auth.verify_service_key) -- not a per-user JWT, since these
    endpoints carry no caller identity, only a `community_id` in the body.
    """
    provided = request.headers.get('X-Service-Key', '')
    if not verify_service_key(provided, Config.SERVICE_API_KEY):
        return error_response("Unauthorized: invalid service key", 401)
    return None


# Logger setup
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
security_service = None
spam_detector = None
content_filter = None
warning_manager = None


@app.before_serving
async def startup():
    """Initialize services on startup."""
    global dal, security_service, spam_detector, content_filter, warning_manager

    logger.system("Starting security-core module", action="startup")

    try:
        # Initialize database
        dal = init_database(Config.DATABASE_URL)
        # SECURITY (C6): `async_dal` (this AsyncDAL wrapper, for
        # `.select_async()`/etc) and `dal` (the real pydal DAL it proxies
        # attribute access to, for `dal(query)` calls and `define_table`)
        # are stored under the SAME two config keys `flask_core.
        # install_community_scoped_auth`/`community_access` expect --
        # matches `core/svc_streaming/app.py`'s established convention.
        app.config['async_dal'] = dal
        app.config['dal'] = dal.dal
        # Read-only tenants/communities/community_members subset this
        # service's community-scoped authz needs -- owned by hub-api's own
        # migrations, never created here (migrate=False).
        bind_community_read_tables(app.config['dal'], migrate=Config.DB_MIGRATE)
        logger.system("Database initialized", result="SUCCESS")

        # Import services
        from services.content_filter import ContentFilter
        from services.security_service import SecurityService
        from services.spam_detector import SpamDetector
        from services.warning_manager import WarningManager

        # Initialize services
        security_service = SecurityService(dal, logger)
        spam_detector = SpamDetector(dal, logger)
        content_filter = ContentFilter(dal, logger)
        warning_manager = WarningManager(dal, logger)

        logger.system("Security core module started", result="SUCCESS")

    except Exception as e:
        logger.error(f"Startup failed: {e}", action="startup", result="FAILED")
        raise


@app.after_serving
async def shutdown():
    """Cleanup on shutdown."""
    logger.system("Shutting down security-core module", action="shutdown")
    logger.system("Security core module shutdown complete", result="SUCCESS")


# ============================================================================
# PUBLIC API ENDPOINTS
# ============================================================================

@api_bp.route('/status', methods=['GET'])
async def get_status():
    """Get module status."""
    return success_response({
        'module': Config.MODULE_NAME,
        'version': Config.MODULE_VERSION,
        'status': 'healthy'
    })


@api_bp.route('/<int:community_id>/config', methods=['GET'])
async def get_config(community_id: int):
    """Get security configuration for community."""
    try:
        config = await security_service.get_config(community_id)
        return success_response(config)
    except Exception as e:
        logger.error(f"Failed to get config: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/config', methods=['PUT'])
async def update_config(community_id: int):
    """Update security configuration for community."""
    try:
        data = await request.get_json()
        config = await security_service.update_config(community_id, data)
        return success_response(config)
    except Exception as e:
        logger.error(f"Failed to update config: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/warnings', methods=['GET'])
async def get_warnings(community_id: int):
    """List all warnings for community.

    Query params:
    - status: active, expired, all (default: active)
    - page: page number (default: 1)
    - limit: results per page (default: 25)
    """
    try:
        status = request.args.get('status', 'active')
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 25))

        warnings = await warning_manager.get_warnings(community_id, status, page, limit)
        return success_response(warnings)
    except Exception as e:
        logger.error(f"Failed to get warnings: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/warnings', methods=['POST'])
async def issue_manual_warning(community_id: int):
    """Issue manual warning.

    Expected payload:
    {
        "platform": str,
        "platform_user_id": str,
        "warning_reason": str,
        "issued_by": int (hub_user_id)
    }
    """
    try:
        data = await request.get_json()
        warning = await warning_manager.issue_manual_warning(
            community_id=community_id,
            platform=data['platform'],
            platform_user_id=data['platform_user_id'],
            warning_reason=data['warning_reason'],
            issued_by=data.get('issued_by')
        )
        return success_response(warning)
    except Exception as e:
        logger.error(f"Failed to issue warning: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/warnings/<int:warning_id>', methods=['DELETE'])
async def revoke_warning(community_id: int, warning_id: int):
    """Revoke warning.

    Expected payload:
    {
        "revoked_by": int (hub_user_id),
        "revoke_reason": str
    }
    """
    try:
        data = await request.get_json()
        result = await warning_manager.revoke_warning(
            warning_id=warning_id,
            revoked_by=data.get('revoked_by'),
            revoke_reason=data.get('revoke_reason')
        )
        return success_response(result)
    except Exception as e:
        logger.error(f"Failed to revoke warning: {e}", warning_id=warning_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/filter-matches', methods=['GET'])
async def get_filter_matches(community_id: int):
    """View filter match log.

    Query params:
    - page: page number (default: 1)
    - limit: results per page (default: 50)
    """
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))

        matches = await content_filter.get_filter_matches(community_id, page, limit)
        return success_response(matches)
    except Exception as e:
        logger.error(f"Failed to get filter matches: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/blocked-words', methods=['POST'])
async def add_blocked_words(community_id: int):
    """Add blocked words.

    Expected payload:
    {
        "words": ["word1", "word2", ...]
    }
    """
    try:
        data = await request.get_json()
        result = await content_filter.add_blocked_words(community_id, data['words'])
        return success_response(result)
    except Exception as e:
        logger.error(f"Failed to add blocked words: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/blocked-words', methods=['DELETE'])
async def remove_blocked_words(community_id: int):
    """Remove blocked words.

    Expected payload:
    {
        "words": ["word1", "word2", ...]
    }
    """
    try:
        data = await request.get_json()
        result = await content_filter.remove_blocked_words(community_id, data['words'])
        return success_response(result)
    except Exception as e:
        logger.error(f"Failed to remove blocked words: {e}", community_id=community_id)
        return error_response(str(e), 500)


@api_bp.route('/<int:community_id>/moderation-log', methods=['GET'])
async def get_moderation_log(community_id: int):
    """View moderation actions log.

    Query params:
    - page: page number (default: 1)
    - limit: results per page (default: 50)
    """
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))

        actions = await security_service.get_moderation_log(community_id, page, limit)
        return success_response(actions)
    except Exception as e:
        logger.error(f"Failed to get moderation log: {e}", community_id=community_id)
        return error_response(str(e), 500)


# ============================================================================
# INTERNAL API ENDPOINTS (Service-to-Service)
# ============================================================================

@internal_bp.route('/check', methods=['POST'])
async def check_message():
    """Check message against filters (real-time).

    Expected payload:
    {
        "community_id": int,
        "platform": str,
        "platform_user_id": str,
        "message": str,
        "metadata": {}
    }

    Returns:
    {
        "allowed": bool,
        "blocked_reason": str (if blocked),
        "action_taken": str
    }
    """
    try:
        data = await request.get_json()

        # Check spam
        is_spam = await spam_detector.check_spam(
            community_id=data['community_id'],
            platform=data['platform'],
            platform_user_id=data['platform_user_id']
        )

        # Check content filter
        is_filtered, matched_pattern = await content_filter.check_message(
            community_id=data['community_id'],
            message=data['message']
        )

        # Determine action
        if is_spam:
            return success_response({
                'allowed': False,
                'blocked_reason': 'spam_detected',
                'action_taken': 'warn'
            })

        if is_filtered:
            return success_response({
                'allowed': False,
                'blocked_reason': 'content_filtered',
                'matched_pattern': matched_pattern,
                'action_taken': 'delete'
            })

        return success_response({
            'allowed': True
        })

    except Exception as e:
        logger.error(f"Failed to check message: {e}")
        return error_response(str(e), 500)


@internal_bp.route('/warn', methods=['POST'])
async def issue_automated_warning():
    """Issue automated warning.

    Expected payload:
    {
        "community_id": int,
        "platform": str,
        "platform_user_id": str,
        "warning_type": str,
        "warning_reason": str,
        "trigger_message": str
    }
    """
    try:
        data = await request.get_json()
        warning = await warning_manager.issue_automated_warning(
            community_id=data['community_id'],
            platform=data['platform'],
            platform_user_id=data['platform_user_id'],
            warning_type=data['warning_type'],
            warning_reason=data['warning_reason'],
            trigger_message=data.get('trigger_message')
        )
        return success_response(warning)
    except Exception as e:
        logger.error(f"Failed to issue automated warning: {e}")
        return error_response(str(e), 500)


@internal_bp.route('/sync-action', methods=['POST'])
async def sync_moderation_action():
    """Sync moderation action across platforms.

    Expected payload:
    {
        "community_id": int,
        "platform": str,
        "platform_user_id": str,
        "action_type": str,
        "action_reason": str,
        "moderator_id": int,
        "sync_to_platforms": []
    }
    """
    try:
        data = await request.get_json()
        result = await security_service.sync_moderation_action(
            community_id=data['community_id'],
            platform=data['platform'],
            platform_user_id=data['platform_user_id'],
            action_type=data['action_type'],
            action_reason=data.get('action_reason'),
            moderator_id=data.get('moderator_id'),
            sync_to_platforms=data.get('sync_to_platforms', [])
        )
        return success_response(result)
    except Exception as e:
        logger.error(f"Failed to sync moderation action: {e}")
        return error_response(str(e), 500)


# Register blueprints
app.register_blueprint(api_bp)
app.register_blueprint(internal_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    config.workers = 4

    logger.system(f"Starting security-core on port {Config.MODULE_PORT}")
    asyncio.run(hypercorn.asyncio.serve(app, config))
