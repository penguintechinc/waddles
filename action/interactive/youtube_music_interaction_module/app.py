"""
YouTube Music integration - Quart Application
"""
import asyncio
import os
import sys

from quart import Blueprint, Quart

# Setup path for shared libraries
sys.path.insert(0,
                os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             'libs'))

from flask_core import (async_endpoint, create_health_blueprint,  # noqa: E402
                        init_database, setup_aaa_logging,
                        success_response)
from config import Config  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None


@app.before_serving
async def startup():
    global dal
    logger.system("Starting youtube_music_interaction_module",
                  action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    logger.system("youtube_music_interaction_module started",
                  result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME
    })


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
