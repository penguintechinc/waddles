"""
Presence Module — cross-platform user presence/status syncing.

Collects status from connected platforms, stores in Redis, and fans out
canonical status updates to all push-capable platforms.
"""
import asyncio
import os
import sys

# Insert libs/ directory two levels up from this file's parent
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'),
)

from quart import Blueprint, Quart, request  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint,
    create_health_blueprint,
    error_response,
    setup_aaa_logging,
    success_response,
)
from config import Config  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

presence_service = None
settings_store = None


@app.before_serving
async def startup():
    global presence_service, settings_store
    logger.system("Starting presence_module", action="startup")

    Config.validate()

    import redis.asyncio as aioredis
    from presence import PresenceStateStore, PresenceSyncEngine
    from services.presence_service import PresenceService

    redis_client = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
    state_store = PresenceStateStore(redis_client)
    sync_engine = PresenceSyncEngine(state_store=state_store)

    presence_service = PresenceService(
        state_store=state_store,
        sync_engine=sync_engine,
        redis_client=redis_client,
    )
    app.config['presence_service'] = presence_service

    logger.system("presence_module started", result="SUCCESS")


# ──────────────────────────────────────────────────────────────────────────────
# Status endpoint
# ──────────────────────────────────────────────────────────────────────────────

@api_bp.route('/status')
@async_endpoint
async def status():
    """Module status endpoint"""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
    })


# ──────────────────────────────────────────────────────────────────────────────
# Presence endpoints
# ──────────────────────────────────────────────────────────────────────────────

@api_bp.route('/presence/update', methods=['POST'])
@async_endpoint
async def update_presence():
    """Process an incoming presence update from a platform.

    Request JSON:
    {
        "user_id": "waddlebot-user-uuid",
        "source_platform": "slack",
        "canonical_status": "online",
        "platform_status": "active",
        "metadata": {}
    }

    Returns sync result including which platforms received the fan-out.
    """
    try:
        data = await request.get_json()
        if not data:
            return error_response("Request body is required", status_code=400)

        user_id = data.get('user_id')
        source_platform = data.get('source_platform')
        canonical_status = data.get('canonical_status')
        platform_status = data.get('platform_status', canonical_status)
        metadata = data.get('metadata', {})

        if not user_id:
            return error_response("user_id is required", status_code=400)
        if not source_platform:
            return error_response("source_platform is required", status_code=400)
        if not canonical_status:
            return error_response("canonical_status is required", status_code=400)

        logger.audit(
            action="presence_update",
            user=user_id,
            community="",
            result="STARTED",
            platform=source_platform,
            status=canonical_status,
        )

        result = await presence_service.process_presence_update(
            user_id=user_id,
            source_platform=source_platform,
            canonical_status=canonical_status,
            platform_status=platform_status,
            metadata=metadata,
        )

        logger.audit(
            action="presence_update",
            user=user_id,
            community="",
            result="SUCCESS",
            platform=source_platform,
            status=canonical_status,
        )
        return success_response(result)

    except ValueError as exc:
        return error_response(str(exc), status_code=422)
    except Exception as exc:
        logger.error("Failed to process presence update: %s", exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>', methods=['GET'])
@async_endpoint
async def get_presence(user_id: str):
    """Get the aggregated (most-recent-wins) presence for a user.

    Query params:
    - platform: Optional. If provided, return raw record for that platform only.
    - all_platforms: Optional boolean. If "true", return per-platform breakdown.
    """
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        platform = request.args.get('platform')
        all_platforms = request.args.get('all_platforms', '').lower() == 'true'

        presence_data = await presence_service.get_user_presence(
            user_id=user_id,
            platform=platform,
            all_platforms=all_platforms,
        )

        if presence_data is None:
            return error_response(
                f"No presence data found for user {user_id}",
                status_code=404,
            )

        return success_response(presence_data)

    except Exception as exc:
        logger.error("Failed to get presence for user %s: %s", user_id, exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>/settings', methods=['GET'])
@async_endpoint
async def get_user_settings(user_id: str):
    """Get presence sync settings for a user.

    Returns which platforms are enabled, sync preferences, etc.
    """
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        settings = await presence_service.get_user_settings(user_id)
        return success_response(settings)

    except Exception as exc:
        logger.error(
            "Failed to get settings for user %s: %s", user_id, exc
        )
        return error_response(str(exc), status_code=500)


@api_bp.route('/presence/<user_id>/settings', methods=['PUT'])
@async_endpoint
async def update_user_settings(user_id: str):
    """Update presence sync settings for a user.

    Request JSON:
    {
        "sync_enabled": true,
        "enabled_platforms": ["slack", "teams"],
        "sync_direction": "bidirectional"
    }
    """
    try:
        if not user_id:
            return error_response("user_id is required", status_code=400)

        data = await request.get_json()
        if not data:
            return error_response("Request body is required", status_code=400)

        updated = await presence_service.update_user_settings(user_id, data)

        logger.audit(
            action="update_presence_settings",
            user=user_id,
            community="",
            result="SUCCESS",
        )
        return success_response(updated)

    except ValueError as exc:
        return error_response(str(exc), status_code=422)
    except Exception as exc:
        logger.error(
            "Failed to update settings for user %s: %s", user_id, exc
        )
        return error_response(str(exc), status_code=500)


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    hconfig = HyperConfig()
    hconfig.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hconfig))
