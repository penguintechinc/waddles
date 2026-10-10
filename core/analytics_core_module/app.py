"""
Analytics Core Module - Main Application

Provides analytics and metrics for community health, engagement, and insights.
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs'))

from quart import Quart, Blueprint, request, jsonify
from flask_core import (
    setup_aaa_logging,
    init_database,
    success_response,
    error_response,
    create_health_blueprint,
    install_rate_limiting,
    install_security_headers,
)
from config import Config
from services.log_safety import INTERNAL_ERROR_MESSAGE, log_failure

# Create Quart app
app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Health blueprint
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): every route below had zero rate limiting. Shared global
# before_request hook (flask_core.http_rate_limit, extracted from hub_api's
# own gh security PR #255 fix) -- one call site covers every route present
# or future, not a per-route decorator that a new route could ship without.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

# API blueprints
api_bp = Blueprint('api', __name__, url_prefix='/api/v1/analytics')
internal_bp = Blueprint('internal', __name__, url_prefix='/api/v1/internal')

# Logger setup
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
analytics_service = None
metrics_service = None
polling_service = None
bot_score_service = None
user_stats_service = None
platform_stats_service = None


@app.before_request
async def validate_service_key():
    """
    Validate X-Service-Key header on all non-health endpoints.
    Health check (/healthz) is exempt so orchestrators can probe liveness.
    """
    if request.path.startswith('/health') or request.path == '/':
        return None
    service_key = request.headers.get('X-Service-Key', '')
    if not service_key or service_key != Config.SERVICE_API_KEY:
        logger.warning(
            "Unauthorized request: missing or invalid X-Service-Key",
            path=request.path,
            method=request.method,
        )
        return error_response("Unauthorized: invalid service key", 401)
    return None


@app.before_serving
async def startup():
    """Initialize services on startup."""
    global dal, analytics_service, metrics_service, polling_service
    global bot_score_service, user_stats_service, platform_stats_service

    logger.system("Starting analytics-core module", action="startup")

    try:
        # Initialize database
        dal = init_database(Config.DATABASE_URL)
        app.config['dal'] = dal
        logger.system("Database initialized", result="SUCCESS")

        # Import services
        from services.analytics_service import AnalyticsService
        from services.metrics_service import MetricsService
        from services.polling_service import PollingService
        from services.bot_score_service import BotScoreService
        from services.user_stats_service import UserStatsService
        from services.platform_stats_service import PlatformStatsService

        # Initialize services
        analytics_service = AnalyticsService(dal, logger)
        metrics_service = MetricsService(dal, logger)
        polling_service = PollingService(dal, logger)
        bot_score_service = BotScoreService(dal, logger)
        user_stats_service = UserStatsService(dal, logger)
        platform_stats_service = PlatformStatsService(dal, logger)

        # Wire services into blueprints
        from blueprints.user_bp import init_user_blueprint
        from blueprints.platform_bp import init_platform_blueprint
        init_user_blueprint(user_stats_service)
        init_platform_blueprint(platform_stats_service)

        logger.system("Analytics core module started", result="SUCCESS")

    except Exception as e:
        log_failure(logger, "Startup failed", e, action="startup", result="FAILED")
        raise


@app.after_serving
async def shutdown():
    """Cleanup on shutdown."""
    logger.system("Shutting down analytics-core module", action="shutdown")
    logger.system("Analytics core module shutdown complete", result="SUCCESS")


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


@api_bp.route('/<int:community_id>/basic', methods=['GET'])
async def get_basic_stats(community_id: int):
    """
    Get basic stats (Free tier).

    Returns:
    - Total chatters count
    - Stream time tracking
    - Messages per user
    """
    try:
        stats = await analytics_service.get_basic_stats(community_id)
        return success_response(stats)
    except Exception as e:
        log_failure(logger, "Failed to get basic stats", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/metrics', methods=['GET'])
async def get_metrics(community_id: int):
    """
    Get time-series metrics with period params.

    Query params:
    - metric_type: messages, viewers, engagement, growth
    - bucket_size: 1h, 1d, 1w, 1m
    - start_date: ISO date
    - end_date: ISO date
    """
    try:
        metric_type = request.args.get('metric_type', 'messages')
        bucket_size = request.args.get('bucket_size', '1d')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        metrics = await metrics_service.get_timeseries(
            community_id=community_id,
            metric_type=metric_type,
            bucket_size=bucket_size,
            start_date=start_date,
            end_date=end_date
        )
        return success_response(metrics)
    except Exception as e:
        log_failure(logger, "Failed to get metrics", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/poll', methods=['GET'])
async def poll_updates(community_id: int):
    """
    REST polling endpoint for real-time updates.

    Query params:
    - since: timestamp of last update (optional)
    """
    try:
        since = request.args.get('since')
        updates = await polling_service.get_updates(community_id, since)
        return success_response(updates)
    except Exception as e:
        log_failure(logger, "Failed to poll updates", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/config', methods=['GET'])
async def get_config(community_id: int):
    """Get analytics configuration for community."""
    try:
        config = await analytics_service.get_config(community_id)
        return success_response(config)
    except Exception as e:
        log_failure(logger, "Failed to get config", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/config', methods=['PUT'])
async def update_config(community_id: int):
    """Update analytics configuration for community."""
    try:
        data = await request.get_json()
        config = await analytics_service.update_config(community_id, data)
        return success_response(config)
    except Exception as e:
        log_failure(logger, "Failed to update config", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


# ============================================================================
# INTERNAL API ENDPOINTS (Service-to-Service)
# ============================================================================

@internal_bp.route('/events', methods=['POST'])
async def receive_events():
    """
    Receive activity events for aggregation.

    Expected payload:
    {
        "community_id": int,
        "events": [
            {
                "event_type": "message" | "viewer_join" | "viewer_leave",
                "platform": str,
                "platform_user_id": str,
                "timestamp": ISO datetime,
                "metadata": {}
            }
        ]
    }
    """
    try:
        data = await request.get_json()
        result = await analytics_service.process_events(data['events'])
        return success_response(result)
    except Exception as e:
        log_failure(logger, "Failed to process events", e)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@internal_bp.route('/aggregate', methods=['POST'])
async def trigger_aggregation():
    """
    Trigger aggregation job.

    Expected payload:
    {
        "community_id": int (optional - all if not provided),
        "force": bool (optional - force even if recent)
    }
    """
    try:
        data = await request.get_json()
        community_id = data.get('community_id')
        force = data.get('force', False)

        result = await analytics_service.run_aggregation(community_id, force)
        return success_response(result)
    except Exception as e:
        log_failure(logger, "Failed to trigger aggregation", e)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


# ============================================================================
# BOT SCORE ENDPOINTS
# ============================================================================

@api_bp.route('/<int:community_id>/bot-score', methods=['GET'])
async def get_bot_score(community_id: int):
    """
    Get bot detection score for community.

    Returns cached score or calculates if stale/missing.
    """
    try:
        score = await bot_score_service.get_score(community_id)
        return success_response(score)
    except Exception as e:
        log_failure(logger, "Failed to get bot score", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/bot-score/calculate', methods=['POST'])
async def calculate_bot_score(community_id: int):
    """
    Force recalculation of bot score for community.
    """
    try:
        score = await bot_score_service.calculate_score(community_id)
        return success_response(score)
    except Exception as e:
        log_failure(logger, "Failed to calculate bot score", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/suspected-bots', methods=['GET'])
async def get_suspected_bots(community_id: int):
    """
    Get list of suspected bots for community (Premium feature).

    Query params:
    - limit: max results (default 50)
    - min_confidence: minimum confidence score (default 50)
    """
    try:
        limit = int(request.args.get('limit', '50'))
        min_confidence = int(request.args.get('min_confidence', '50'))

        bots = await bot_score_service.get_suspected_bots(
            community_id, limit=limit, min_confidence=min_confidence
        )
        return success_response({'suspected_bots': bots})
    except Exception as e:
        log_failure(logger, "Failed to get suspected bots", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


@api_bp.route('/<int:community_id>/suspected-bots/<int:bot_id>/review', methods=['PUT'])
async def review_suspected_bot(community_id: int, bot_id: int):
    """
    Mark a suspected bot as reviewed (false positive or confirmed).

    Body:
    - is_false_positive: bool
    """
    try:
        data = await request.get_json()
        is_false_positive = data.get('is_false_positive', False)

        # Get reviewer ID from request context (set by auth middleware)
        reviewer_id = request.headers.get('X-User-ID')

        result = await bot_score_service.mark_bot_reviewed(
            community_id, bot_id, is_false_positive, reviewer_id
        )
        return success_response(result)
    except Exception as e:
        log_failure(logger, "Failed to review suspected bot", e, community_id=community_id)
        return error_response(INTERNAL_ERROR_MESSAGE, 500)


# Register blueprints
app.register_blueprint(api_bp)
app.register_blueprint(internal_bp)

# Import and register new analytics blueprints
from blueprints.user_bp import user_bp
from blueprints.platform_bp import platform_bp
app.register_blueprint(user_bp)
app.register_blueprint(platform_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    config.workers = 4

    logger.system(f"Starting analytics-core on port {Config.MODULE_PORT}")
    asyncio.run(hypercorn.asyncio.serve(app, config))
