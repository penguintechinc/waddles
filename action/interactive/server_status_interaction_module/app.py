"""
Server Status Interaction Module

Monitors game server status with polling, event tracking,
and multi-provider support (Steam, Riot, custom URLs).
"""
import asyncio
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'),
)

from quart import Blueprint, Quart, request  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint,
    create_health_blueprint,
    error_response,
    init_database,
    setup_aaa_logging,
    success_response,
)
from config import Config  # noqa: E402
from services.provider_service import ProviderService  # noqa: E402
from services.status_service import StatusService  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('server_status_api', __name__, url_prefix='/api/v1/server-status')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
provider_service = None
status_service = None


@app.before_serving
async def startup():
    global dal, provider_service, status_service
    logger.system(
        "Starting server_status_interaction_module", action="startup"
    )

    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    provider_service = ProviderService(Config)
    status_service = StatusService(dal, Config, provider_service)

    logger.system(
        "server_status_interaction_module started", result="SUCCESS"
    )


# =====================================================
# STATUS ENDPOINTS
# =====================================================


@api_bp.route('/<int:community_id>')
@async_endpoint
async def get_current_status(community_id: int):
    """Get all known statuses for a community."""
    try:
        statuses = await status_service.get_current_status(community_id)
        return success_response({
            'statuses': statuses,
            'count': len(statuses),
        })
    except Exception as exc:
        logger.error("Failed to get current status: %s", exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/<int:community_id>/<game_name>')
@async_endpoint
async def check_game_status(community_id: int, game_name: str):
    """Live-check status for a single game."""
    try:
        result = await status_service.check_status(community_id, game_name)
        if result.get('error'):
            return error_response(result['message'], status_code=404)
        return success_response(result)
    except Exception as exc:
        logger.error("Failed to check game status: %s", exc)
        return error_response(str(exc), status_code=500)


@api_bp.route('/<int:community_id>/check', methods=['POST'])
@async_endpoint
async def force_check(community_id: int):
    """Force a live poll for all games in a community."""
    try:
        statuses = await status_service.get_current_status(community_id)
        results = []
        for entry in statuses:
            if entry.get('is_active'):
                result = await status_service.check_status(
                    community_id, entry['game_name']
                )
                results.append(result)
        return success_response({
            'results': results,
            'count': len(results),
        })
    except Exception as exc:
        logger.error("Failed to force check: %s", exc)
        return error_response(str(exc), status_code=500)


# =====================================================
# CONFIG ENDPOINTS
# =====================================================


@api_bp.route('/<int:community_id>/configs', methods=['POST'])
@async_endpoint
async def add_config(community_id: int):
    """Add or update a game server status config.

    Request JSON:
    {
        "game_name": "cs2",
        "status_api_type": "steam",
        "status_url": null,
        "alert_on_outage": true,
        "poll_interval_minutes": 5
    }
    """
    try:
        data = await request.get_json()

        game_name = data.get('game_name')
        status_api_type = data.get('status_api_type')

        if not game_name:
            return error_response(
                "game_name is required", status_code=400
            )
        if not status_api_type:
            return error_response(
                "status_api_type is required", status_code=400
            )

        config = await status_service.add_config(
            community_id=community_id,
            game_name=game_name,
            status_api_type=status_api_type,
            status_url=data.get('status_url'),
            alert_on_outage=data.get('alert_on_outage', True),
            poll_interval_minutes=data.get('poll_interval_minutes'),
        )

        logger.audit(
            action="add_config",
            user="system",
            community=community_id,
            result="SUCCESS",
            game=game_name,
        )

        return success_response(config)
    except Exception as exc:
        logger.error("Failed to add config: %s", exc)
        return error_response(str(exc), status_code=500)


@api_bp.route(
    '/<int:community_id>/configs/<game_name>', methods=['DELETE']
)
@async_endpoint
async def remove_config(community_id: int, game_name: str):
    """Remove (deactivate) a game server status config."""
    try:
        removed = await status_service.remove_config(
            community_id, game_name
        )
        if not removed:
            return error_response(
                "Config not found or already inactive",
                status_code=404,
            )

        logger.audit(
            action="remove_config",
            user="system",
            community=community_id,
            result="SUCCESS",
            game=game_name,
        )

        return success_response({'message': 'Config removed'})
    except Exception as exc:
        logger.error("Failed to remove config: %s", exc)
        return error_response(str(exc), status_code=500)


# =====================================================
# POLLING / CRON ENDPOINT
# =====================================================


@api_bp.route('/poll', methods=['POST'])
@async_endpoint
async def poll_all():
    """Cron endpoint: poll ALL active configs across all communities."""
    try:
        summary = await status_service.poll_all()
        logger.system(
            "Poll complete",
            polled=summary['polled'],
            changes=summary['changes'],
        )
        return success_response(summary)
    except Exception as exc:
        logger.error("Poll failed: %s", exc)
        return error_response(str(exc), status_code=500)


# =====================================================
# EVENT HISTORY
# =====================================================


@api_bp.route('/<int:community_id>/events')
@async_endpoint
async def get_events(community_id: int):
    """Get recent status events for a community.

    Query params:
    - limit: Number of events (default 20)
    """
    try:
        limit = int(request.args.get('limit', 20))
        events = await status_service.get_recent_events(
            community_id, limit
        )
        return success_response({
            'events': events,
            'count': len(events),
        })
    except Exception as exc:
        logger.error("Failed to get events: %s", exc)
        return error_response(str(exc), status_code=500)


# =====================================================
# MODULE STATUS
# =====================================================


@api_bp.route('/status')
@async_endpoint
async def status():
    """Module status endpoint."""
    return success_response({
        'status': 'operational',
        'module': Config.MODULE_NAME,
        'version': Config.MODULE_VERSION,
    })


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    hconfig = HyperConfig()
    hconfig.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hconfig))
