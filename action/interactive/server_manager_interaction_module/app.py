"""
Server Manager Interaction Module

Replaces and extends server_status_interaction_module with RCON
game server management and reputation-based access control.
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
from services.encryption_service import EncryptionService  # noqa: E402
from services.rcon_service import RconService  # noqa: E402
from services.enforcement_service import EnforcementService  # noqa: E402

app = Quart(__name__)

health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# Backward-compat blueprint (preserves all server_status endpoints)
status_bp = Blueprint('status_api', __name__, url_prefix='/api/v1/server-status')
# New server-manager blueprint
manager_bp = Blueprint('manager_api', __name__, url_prefix='/api/v1/server-manager')

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
provider_service = None
status_service = None
encryption_service = None
rcon_service = None
enforcement_service = None


@app.before_serving
async def startup():
    global dal, provider_service, status_service, encryption_service
    global rcon_service, enforcement_service

    logger.system("Starting server_manager_interaction_module", action="startup")

    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    provider_service = ProviderService(Config)
    status_service = StatusService(dal, Config, provider_service)
    encryption_service = EncryptionService(Config)
    rcon_service = RconService(Config, encryption_service)
    enforcement_service = EnforcementService(dal, Config, rcon_service, encryption_service)

    logger.system("server_manager_interaction_module started", result="SUCCESS")


# =====================================================
# BACKWARD-COMPAT: server_status endpoints
# =====================================================

@status_bp.route('/<int:community_id>')
@async_endpoint
async def get_current_status(community_id: int):
    try:
        statuses = await status_service.get_current_status(community_id)
        return success_response({'statuses': statuses, 'count': len(statuses)})
    except Exception as exc:
        logger.error("Failed to get current status: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/<int:community_id>/<game_name>')
@async_endpoint
async def check_game_status(community_id: int, game_name: str):
    try:
        result = await status_service.check_status(community_id, game_name)
        if result.get('error'):
            return error_response(result['message'], status_code=404)
        return success_response(result)
    except Exception as exc:
        logger.error("Failed to check game status: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/<int:community_id>/check', methods=['POST'])
@async_endpoint
async def force_check(community_id: int):
    try:
        statuses = await status_service.get_current_status(community_id)
        results = []
        for entry in statuses:
            if entry.get('is_active'):
                result = await status_service.check_status(community_id, entry['game_name'])
                results.append(result)
        return success_response({'results': results, 'count': len(results)})
    except Exception as exc:
        logger.error("Failed to force check: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/<int:community_id>/configs', methods=['POST'])
@async_endpoint
async def add_config(community_id: int):
    try:
        data = await request.get_json()
        game_name = data.get('game_name')
        status_api_type = data.get('status_api_type')
        if not game_name:
            return error_response("game_name is required", status_code=400)
        if not status_api_type:
            return error_response("status_api_type is required", status_code=400)
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
            game=game_name
        )
        return success_response(config)
    except Exception as exc:
        logger.error("Failed to add config: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/<int:community_id>/configs/<game_name>', methods=['DELETE'])
@async_endpoint
async def remove_config(community_id: int, game_name: str):
    try:
        removed = await status_service.remove_config(community_id, game_name)
        if not removed:
            return error_response("Config not found or already inactive", status_code=404)
        logger.audit(
            action="remove_config",
            user="system",
            community=community_id,
            result="SUCCESS",
            game=game_name
        )
        return success_response({'message': 'Config removed'})
    except Exception as exc:
        logger.error("Failed to remove config: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/poll', methods=['POST'])
@async_endpoint
async def poll_all():
    try:
        summary = await status_service.poll_all()
        logger.system("Poll complete", polled=summary['polled'], changes=summary['changes'])
        return success_response(summary)
    except Exception as exc:
        logger.error("Poll failed: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/<int:community_id>/events')
@async_endpoint
async def get_events(community_id: int):
    try:
        limit = int(request.args.get('limit', 20))
        events = await status_service.get_recent_events(community_id, limit)
        return success_response({'events': events, 'count': len(events)})
    except Exception as exc:
        logger.error("Failed to get events: %s", exc)
        return error_response(str(exc), status_code=500)


@status_bp.route('/status')
@async_endpoint
async def status():
    return success_response({
        'status': 'operational',
        'module': Config.MODULE_NAME,
        'version': Config.MODULE_VERSION,
    })


# =====================================================
# NEW: server-manager endpoints
# =====================================================

def _get_server_config(server_id: int, community_id: int):
    """Fetch a server config row, returning dict or None."""
    rows = dal.executesql(
        "SELECT id, community_id, game_name, server_type, host, game_port, "
        "rcon_port, credential_enc, credential_iv, game_type, visibility, "
        "display_name, status_api_type, status_url, is_active, metadata "
        "FROM server_status_configs "
        "WHERE id = $1 AND community_id = $2 AND deleted_at IS NULL",
        placeholders=[server_id, community_id],
    )
    if not rows:
        return None
    r = rows[0]
    return {
        'id': r[0], 'community_id': r[1], 'game_name': r[2],
        'server_type': r[3], 'host': r[4], 'game_port': r[5],
        'rcon_port': r[6], 'credential_enc': r[7], 'credential_iv': r[8],
        'game_type': r[9], 'visibility': r[10], 'display_name': r[11],
        'status_api_type': r[12], 'status_url': r[13],
        'is_active': r[14], 'metadata': r[15],
    }


def _decrypt_credential(config_row: dict) -> str:
    """Decrypt the credential from a server config row."""
    enc = config_row.get('credential_enc')
    iv = config_row.get('credential_iv')
    if not enc or not iv:
        raise ValueError("No credentials stored for this server")
    return encryption_service.decrypt(bytes(enc), bytes(iv))


def _log_command(server_config_id: int, user_id: int, command: str, response: str, success: bool):
    """Insert a row into rcon_command_log."""
    dal.executesql(
        "INSERT INTO rcon_command_log (server_config_id, user_id, command, response_summary, success) "
        "VALUES ($1, $2, $3, $4, $5)",
        placeholders=[server_config_id, user_id, command, (response or '')[:500], success],
    )
    dal.commit()


@manager_bp.route('/<int:community_id>/connect-test', methods=['POST'])
@async_endpoint
async def connect_test(community_id: int):
    """Test connection to a server (called from hub backend)."""
    try:
        data = await request.get_json()
        server_type = data.get('server_type', 'rcon')
        host = data.get('host')
        port = int(data.get('port', 0))
        password = data.get('password', '')

        if not host or not port:
            return error_response("host and port are required", status_code=400)

        if server_type == 'rcon':
            result = await rcon_service.test_connection(host, port, password)
        else:
            return error_response(f"Unknown server_type: {server_type}", status_code=400)

        return success_response(result)
    except Exception as exc:
        logger.error("Connect test failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/command', methods=['POST'])
@async_endpoint
async def execute_command(community_id: int):
    """Execute a command on a server."""
    try:
        data = await request.get_json()
        server_id = data.get('server_id')
        command = data.get('command')
        user_id = data.get('user_id')

        if not server_id or not command:
            return error_response("server_id and command are required", status_code=400)

        config = _get_server_config(int(server_id), community_id)
        if not config:
            return error_response("Server not found", status_code=404)

        password = _decrypt_credential(config)
        server_type = config['server_type']

        if server_type == 'rcon':
            result = await rcon_service.execute(config['host'], config['rcon_port'], password, command)
        else:
            return error_response(f"Command execution not supported for {server_type}", status_code=400)

        _log_command(int(server_id), user_id, command, result.get('response', result.get('error', '')), result.get('success', False))
        return success_response(result)
    except Exception as exc:
        logger.error("Command execution failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/status')
@async_endpoint
async def server_status(community_id: int, server_id: int):
    """Get live server status."""
    try:
        config = _get_server_config(server_id, community_id)
        if not config:
            return error_response("Server not found", status_code=404)

        password = _decrypt_credential(config)
        server_type = config['server_type']

        if server_type == 'rcon':
            result = await rcon_service.get_status(config['host'], config['rcon_port'], password)
        else:
            result = {'success': False, 'error': 'Status not supported for this server type'}

        return success_response(result)
    except Exception as exc:
        logger.error("Server status failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/players')
@async_endpoint
async def server_players(community_id: int, server_id: int):
    """Get live player/user list."""
    try:
        config = _get_server_config(server_id, community_id)
        if not config:
            return error_response("Server not found", status_code=404)

        password = _decrypt_credential(config)
        server_type = config['server_type']

        if server_type == 'rcon':
            result = await rcon_service.get_players(config['host'], config['rcon_port'], password)
        else:
            result = {'success': False, 'error': 'Players not supported for this server type'}

        return success_response(result)
    except Exception as exc:
        logger.error("Server players failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/kick', methods=['POST'])
@async_endpoint
async def kick_player(community_id: int, server_id: int):
    """Kick a player from a server."""
    try:
        data = await request.get_json()
        player = data.get('player')
        reason = data.get('reason', '')
        user_id = data.get('user_id')

        if not player:
            return error_response("player is required", status_code=400)

        config = _get_server_config(server_id, community_id)
        if not config:
            return error_response("Server not found", status_code=404)

        password = _decrypt_credential(config)
        server_type = config['server_type']

        if server_type == 'rcon':
            result = await rcon_service.kick_player(config['host'], config['rcon_port'], password, player, reason)
        else:
            return error_response(f"Kick not supported for {server_type}", status_code=400)

        _log_command(server_id, user_id, f'kick {player}', str(result), result.get('success', False))
        return success_response(result)
    except Exception as exc:
        logger.error("Kick failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/ban', methods=['POST'])
@async_endpoint
async def ban_player(community_id: int, server_id: int):
    """Ban a player from a server."""
    try:
        data = await request.get_json()
        player = data.get('player')
        reason = data.get('reason', '')
        duration = int(data.get('duration', 0))
        user_id = data.get('user_id')

        if not player:
            return error_response("player is required", status_code=400)

        config = _get_server_config(server_id, community_id)
        if not config:
            return error_response("Server not found", status_code=404)

        password = _decrypt_credential(config)
        server_type = config['server_type']

        if server_type == 'rcon':
            result = await rcon_service.ban_player(config['host'], config['rcon_port'], password, player, reason, duration)
        else:
            return error_response(f"Ban not supported for {server_type}", status_code=400)

        _log_command(server_id, user_id, f'ban {player}', str(result), result.get('success', False))
        return success_response(result)
    except Exception as exc:
        logger.error("Ban failed: %s", exc)
        return error_response(str(exc), status_code=500)


# -- Enforcement endpoints --

@manager_bp.route('/<int:community_id>/servers/<int:server_id>/policy')
@async_endpoint
async def get_access_policy(community_id: int, server_id: int):
    try:
        policy = await enforcement_service.get_policy(server_id)
        if not policy:
            return success_response({'policy': None})
        return success_response({'policy': policy})
    except Exception as exc:
        logger.error("Get policy failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/policy', methods=['PUT'])
@async_endpoint
async def update_access_policy(community_id: int, server_id: int):
    try:
        data = await request.get_json()
        policy = await enforcement_service.upsert_policy(server_id, community_id, data)
        return success_response({'policy': policy})
    except Exception as exc:
        logger.error("Update policy failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/enforce', methods=['POST'])
@async_endpoint
async def trigger_enforcement(community_id: int, server_id: int):
    try:
        result = await enforcement_service.enforce_server(server_id)
        return success_response(result)
    except Exception as exc:
        logger.error("Enforcement failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/<int:community_id>/servers/<int:server_id>/access-log')
@async_endpoint
async def get_access_log(community_id: int, server_id: int):
    try:
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        log = await enforcement_service.get_access_log(server_id, limit, offset)
        return success_response({'log': log, 'count': len(log)})
    except Exception as exc:
        logger.error("Get access log failed: %s", exc)
        return error_response(str(exc), status_code=500)


@manager_bp.route('/enforce-all', methods=['POST'])
@async_endpoint
async def enforce_all():
    try:
        result = await enforcement_service.enforce_all()
        return success_response(result)
    except Exception as exc:
        logger.error("Enforce all failed: %s", exc)
        return error_response(str(exc), status_code=500)


app.register_blueprint(status_bp)
app.register_blueprint(manager_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    hconfig = HyperConfig()
    hconfig.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hconfig))
