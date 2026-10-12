"""
Linux-style command aliases - Quart Application
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))  # noqa: E402

from quart import Quart, Blueprint, request  # noqa: E402
from flask_core import (  # noqa: E402
    setup_aaa_logging,
    init_database,
    async_endpoint,
    success_response,
    error_response,
    create_health_blueprint
)
from config import Config  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
alias_service = None


@app.before_serving
async def startup():
    global dal, alias_service
    from services.alias_service import AliasService
    logger.system("Starting alias_interaction_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    alias_service = AliasService(dal)
    app.config['alias_service'] = alias_service
    logger.system("alias_interaction_module started", result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({"status": "operational", "module": Config.MODULE_NAME})


@api_bp.route('/aliases', methods=['GET', 'POST'])
@async_endpoint
async def aliases():
    """List or create aliases"""
    if request.method == 'GET':
        community_id = request.args.get('community_id')
        aliases_list = await alias_service.list_aliases(community_id)
        return success_response(aliases_list)
    else:
        data = await request.get_json()
        alias = await alias_service.create_alias(
            data['community_id'], data['alias_name'],
            data['command'], data['created_by']
        )
        return success_response(alias, status_code=201)


@api_bp.route('/aliases/<alias_id>', methods=['DELETE'])
@async_endpoint
async def delete_alias(alias_id):
    """Delete alias"""
    await alias_service.delete_alias(alias_id)
    return success_response({"message": "Alias deleted"})


@api_bp.route('/aliases/execute', methods=['POST'])
@async_endpoint
async def execute_alias():
    """Execute alias with variable substitution"""
    data = await request.get_json()
    command = await alias_service.execute_alias(
        data['alias_name'], data['user'], data.get('args', [])
    )
    if command:
        return success_response({"command": command})
    return error_response("Alias not found", status_code=404)


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
