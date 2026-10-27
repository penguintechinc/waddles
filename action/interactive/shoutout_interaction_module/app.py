"""
Shoutout Interaction Module

Generates platform-aware shoutouts with Twitch API integration.
Includes video shoutouts (!vso) with cross-platform fallback.
"""
import asyncio
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from quart import Blueprint, Quart, request  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint,
    auth_required,
    create_health_blueprint,
    init_database,
    setup_aaa_logging,
    success_response,
    error_response,
)
from flask_core.authz import require_scope  # noqa: E402
from flask_core.feature_flags import feature_enabled  # noqa: E402
from flask_core.tenancy import (  # noqa: E402
    DEFAULT_TENANT_SLUG,
    get_tenant_context,
    tenant_middleware,
)
from config import Config  # noqa: E402
from services.twitch_service import TwitchService  # noqa: E402
from services.shoutout_service import ShoutoutService  # noqa: E402
from services.video_service import VideoService  # noqa: E402
from services.identity_service import IdentityService  # noqa: E402
from services.video_shoutout_service import VideoShoutoutService  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
db_pool = None
twitch_service = None
shoutout_service = None
video_service = None
identity_service = None
video_shoutout_service = None


@app.before_serving
async def startup():
    global dal, db_pool, twitch_service, shoutout_service
    global video_service, identity_service, video_shoutout_service
    logger.system("Starting shoutout_interaction_module", action="startup")

    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    # Create asyncpg pool for video shoutout service
    import asyncpg
    db_pool = await asyncpg.create_pool(Config.DATABASE_URL)

    # Initialize Twitch service
    twitch_service = TwitchService(
        client_id=Config.TWITCH_CLIENT_ID,
        client_secret=Config.TWITCH_CLIENT_SECRET
    )

    # Initialize shoutout service
    shoutout_service = ShoutoutService(dal)

    # Initialize video services
    video_service = VideoService(
        twitch_client_id=Config.TWITCH_CLIENT_ID,
        twitch_client_secret=Config.TWITCH_CLIENT_SECRET,
        youtube_api_key=Config.YOUTUBE_API_KEY
    )
    identity_service = IdentityService(Config.IDENTITY_URL)
    video_shoutout_service = VideoShoutoutService(
        db_pool=db_pool,
        video_service=video_service,
        identity_service=identity_service
    )

    logger.system("shoutout_interaction_module started", result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    """Module status endpoint"""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION
    })


@api_bp.route('/shoutout', methods=['POST'])
# regression: tenant-isolation audit 2026-08-30 -- tenant_middleware was
# missing here, so get_tenant_context(request) always returned None and
# every request silently ran as DEFAULT_TENANT_SLUG with no auth. Must be
# the outermost decorator (security.md: tenant before scope/handler logic)
# so an invalid/missing JWT 401s before async_endpoint or the handler body
# ever run.
@tenant_middleware
# HTTP-layer scope enforcement -- bot_module/features.py's "bot.shoutout"
# Feature contract declares requires_scopes == {"bot.command:write"}; this
# wires that declaration up to an actual 403 rather than leaving it
# unenforced.
@require_scope("bot.command:write")
@async_endpoint
async def create_shoutout():
    """
    Generate shoutout for a Twitch user.

    Request JSON:
    {
        "username": "target_username",
        "community_id": 123,
        "platform": "twitch"
    }

    Returns shoutout message and metadata.
    """
    # v3 Feature gate (bot.shoutout / waddles.bot.shoutout, free tier). The
    # other 3 Bot Features (bot.commands, bot.connectors, bot.interactions)
    # follow this identical one-line guard at their own handler entry points.
    tenant_ctx = get_tenant_context(request)
    tenant_slug = tenant_ctx.tenant_slug if tenant_ctx is not None else DEFAULT_TENANT_SLUG
    if not await feature_enabled("waddles.bot.shoutout", tenant=tenant_slug):
        return error_response("shoutout is not available", status_code=404)

    try:
        data = await request.get_json()

        username = data.get('username')
        community_id = data.get('community_id')
        platform = data.get('platform', 'twitch')

        if not username:
            return error_response("username is required", status_code=400)

        if not community_id:
            return error_response("community_id is required", status_code=400)

        logger.audit(
            action="create_shoutout",
            user="system",
            community=community_id,
            result="STARTED",
            target_user=username
        )

        # Get Twitch data
        twitch_data = await twitch_service.get_full_shoutout_data(username)

        if not twitch_data:
            return error_response(
                f"User '{username}' not found on Twitch",
                status_code=404
            )

        # Generate shoutout
        shoutout = await shoutout_service.generate_shoutout(
            twitch_data,
            community_id,
            platform
        )

        logger.audit(
            action="create_shoutout",
            user="system",
            community=community_id,
            result="SUCCESS",
            target_user=username
        )

        return success_response(shoutout)

    except Exception as e:
        logger.error(f"Failed to create shoutout: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/history/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_history(community_id: int):
    """
    Get shoutout history for community.

    Query params:
    - limit: Number of results (default 50)
    """
    try:
        limit = int(request.args.get('limit', 50))
        history = await shoutout_service.get_shoutout_history(
            community_id,
            limit
        )

        return success_response({
            'history': history,
            'count': len(history)
        })

    except Exception as e:
        logger.error(f"Failed to get history: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/stats/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_stats(community_id: int):
    """Get shoutout statistics for community"""
    try:
        stats = await shoutout_service.get_stats(community_id)
        return success_response(stats)

    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/template', methods=['POST'])
@auth_required
@async_endpoint
async def save_template():
    """
    Save custom shoutout template.

    Request JSON:
    {
        "community_id": 123,
        "platform": "twitch",
        "is_live": true,
        "template": "Check out {display_name} at twitch.tv/{login}!"
    }

    Template variables:
    - {display_name} - User display name
    - {login} - Username
    - {game_name} - Current/last game
    - {title} - Stream title
    - {viewer_count} - Viewer count (live only)
    - {description} - User description
    """
    try:
        data = await request.get_json()

        community_id = data.get('community_id')
        platform = data.get('platform', 'twitch')
        is_live = data.get('is_live', True)
        template = data.get('template')

        if not all([community_id, template]):
            return error_response(
                "community_id and template are required",
                status_code=400
            )

        success = await shoutout_service.save_custom_template(
            community_id,
            platform,
            is_live,
            template
        )

        if success:
            logger.audit(
                action="save_template",
                user="system",
                community=community_id,
                result="SUCCESS"
            )
            return success_response({"message": "Template saved"})
        else:
            return error_response("Failed to save template", status_code=500)

    except Exception as e:
        logger.error(f"Failed to save template: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/twitch/user/<username>', methods=['GET'])
@auth_required
@async_endpoint
async def get_twitch_user(username: str):
    """
    Get Twitch user information.

    For testing/debugging Twitch API integration.
    """
    try:
        data = await twitch_service.get_full_shoutout_data(username)

        if not data:
            return error_response("User not found", status_code=404)

        return success_response(data)

    except Exception as e:
        logger.error(f"Failed to get Twitch user: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/circuit-breaker/metrics', methods=['GET'])
@auth_required
@async_endpoint
async def circuit_breaker_metrics():
    """Get circuit breaker metrics for Twitch API"""
    try:
        metrics = twitch_service.get_circuit_breaker_metrics()
        return success_response(metrics)

    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        return error_response(str(e), status_code=500)


# =====================================================
# VIDEO SHOUTOUT ENDPOINTS (!vso / /vso)
# =====================================================

@api_bp.route('/video-shoutout', methods=['POST'])
@async_endpoint
async def execute_video_shoutout():
    """
    Execute a video shoutout (!vso command).

    Request JSON:
    {
        "community_id": 123,
        "target_username": "streamer_name",
        "target_platform": "twitch",
        "triggered_by_user_id": "123456",
        "triggered_by_username": "mod_name",
        "user_roles": ["mod", "vip"]
    }

    Returns video info for overlay display.
    """
    try:
        data = await request.get_json()

        community_id = data.get('community_id')
        target_username = data.get('target_username')
        target_platform = data.get('target_platform', 'twitch')
        triggered_by_user_id = data.get('triggered_by_user_id')
        triggered_by_username = data.get('triggered_by_username')
        user_roles = data.get('user_roles', [])

        if not community_id or not target_username:
            return error_response(
                "community_id and target_username required",
                status_code=400
            )

        logger.audit(
            action="video_shoutout",
            user="system",
            community=community_id,
            result="STARTED",
            target_user=target_username,
            triggered_by=triggered_by_username
        )

        result = await video_shoutout_service.execute_video_shoutout(
            community_id=community_id,
            target_username=target_username,
            target_platform=target_platform,
            trigger_type='manual',
            triggered_by_user_id=triggered_by_user_id,
            triggered_by_username=triggered_by_username,
            user_roles=user_roles
        )

        if not result.success:
            logger.audit(
                action="video_shoutout",
                user="system",
                community=community_id,
                result="FAILED",
                target_user=target_username,
                error=result.error
            )
            return error_response(result.error, status_code=400)

        logger.audit(
            action="video_shoutout",
            user="system",
            community=community_id,
            result="SUCCESS",
            target_user=target_username
        )

        # Build response with video and channel info
        response_data = {
            'success': True,
            'video': asdict(result.video) if result.video else None,
            'channel': asdict(result.channel) if result.channel else None,
            'game_name': result.game_name,
            'is_live': result.is_live
        }

        return success_response(response_data)

    except Exception as e:
        logger.error(f"Video shoutout failed: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/auto-check', methods=['POST'])
@async_endpoint
async def check_auto_shoutout():
    """
    Check if user is eligible for auto-shoutout.

    Request JSON:
    {
        "community_id": 123,
        "platform": "twitch",
        "user_id": "123456",
        "user_roles": ["vip"],
        "trigger_type": "first_message"
    }

    Returns eligibility status.
    """
    try:
        data = await request.get_json()

        community_id = data.get('community_id')
        platform = data.get('platform')
        user_id = data.get('user_id')
        user_roles = data.get('user_roles', [])
        trigger_type = data.get('trigger_type', 'first_message')

        if not all([community_id, platform, user_id]):
            return error_response(
                "community_id, platform, and user_id required",
                status_code=400
            )

        eligible = await video_shoutout_service.check_auto_shoutout_eligible(
            community_id=community_id,
            platform=platform,
            user_id=user_id,
            user_roles=user_roles,
            trigger_type=trigger_type
        )

        return success_response({'eligible': eligible})

    except Exception as e:
        logger.error(f"Auto-shoutout check failed: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/config/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_vso_config(community_id: int):
    """Get video shoutout configuration for a community"""
    try:
        # Check community eligibility
        eligible = await video_shoutout_service.check_community_eligible(
            community_id
        )
        if not eligible:
            return error_response(
                "Shoutouts not available for this community type",
                status_code=403
            )

        config = await video_shoutout_service.get_config(community_id)
        return success_response(asdict(config) if config else {})

    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/config/<int:community_id>', methods=['PUT'])
@auth_required
@async_endpoint
async def update_vso_config(community_id: int):
    """Update video shoutout configuration"""
    try:
        data = await request.get_json()

        success = await video_shoutout_service.update_config(community_id, data)

        if success:
            logger.audit(
                action="update_vso_config",
                user="system",
                community=community_id,
                result="SUCCESS"
            )
            return success_response({'message': 'Configuration updated'})
        else:
            return error_response(
                "Shoutouts not available for this community type",
                status_code=403
            )

    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/creators/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_creators(community_id: int):
    """Get auto-shoutout creator list"""
    try:
        creators = await video_shoutout_service.get_creators(community_id)
        return success_response({'creators': creators})

    except Exception as e:
        logger.error(f"Failed to get creators: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/creators/<int:community_id>', methods=['POST'])
@auth_required
@async_endpoint
async def add_creator(community_id: int):
    """
    Add creator to auto-shoutout list.

    Request JSON:
    {
        "platform": "twitch",
        "user_id": "123456",
        "username": "streamer_name",
        "custom_trigger": "default"
    }
    """
    try:
        data = await request.get_json()

        platform = data.get('platform')
        user_id = data.get('user_id')
        username = data.get('username')
        custom_trigger = data.get('custom_trigger', 'default')

        if not all([platform, user_id, username]):
            return error_response(
                "platform, user_id, and username required",
                status_code=400
            )

        success = await video_shoutout_service.add_creator(
            community_id=community_id,
            platform=platform,
            user_id=user_id,
            username=username,
            custom_trigger=custom_trigger
        )

        if success:
            logger.audit(
                action="add_creator",
                user="system",
                community=community_id,
                result="SUCCESS",
                creator=username
            )
            return success_response({'message': 'Creator added'})
        else:
            return error_response("Failed to add creator", status_code=500)

    except Exception as e:
        logger.error(f"Failed to add creator: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/creators/<int:community_id>/<platform>/<user_id>',
              methods=['DELETE'])
@auth_required
@async_endpoint
async def remove_creator(community_id: int, platform: str, user_id: str):
    """Remove creator from auto-shoutout list"""
    try:
        success = await video_shoutout_service.remove_creator(
            community_id=community_id,
            platform=platform,
            user_id=user_id
        )

        if success:
            logger.audit(
                action="remove_creator",
                user="system",
                community=community_id,
                result="SUCCESS",
                platform=platform,
                user_id=user_id
            )
            return success_response({'message': 'Creator removed'})
        else:
            return error_response("Creator not found", status_code=404)

    except Exception as e:
        logger.error(f"Failed to remove creator: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/history/<int:community_id>', methods=['GET'])
@auth_required
@async_endpoint
async def get_vso_history(community_id: int):
    """Get video shoutout history for community"""
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        history = await video_shoutout_service.get_history(
            community_id=community_id,
            limit=limit,
            offset=offset
        )

        return success_response({
            'history': history,
            'count': len(history)
        })

    except Exception as e:
        logger.error(f"Failed to get VSO history: {e}")
        return error_response(str(e), status_code=500)


@api_bp.route('/video-shoutout/video/<platform>/<username>', methods=['GET'])
@auth_required
@async_endpoint
async def get_video_content(platform: str, username: str):
    """
    Get video content for a user.

    For testing/preview of video shoutout content.
    """
    try:
        result = await video_service.get_video_for_shoutout(
            platform=platform,
            username=username
        )

        if not result:
            return error_response("No video content found", status_code=404)

        response_data = {
            'video': asdict(result['video']) if result.get('video') else None,
            'channel': asdict(result['channel']) if result.get('channel') else None,
            'game_name': result.get('game_name'),
            'is_live': result.get('is_live', False)
        }

        return success_response(response_data)

    except Exception as e:
        logger.error(f"Failed to get video content: {e}")
        return error_response(str(e), status_code=500)


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
