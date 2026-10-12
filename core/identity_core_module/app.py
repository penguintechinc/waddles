"""Identity linking system - Quart Application"""
import os
import sys

from quart import Blueprint, Quart

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from config import Config  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint, create_health_blueprint, init_database, install_rate_limiting,
    install_security_headers, setup_aaa_logging, success_response,
)
from services.grpc_handler import IdentityServiceServicer  # noqa: E402
from proto import identity_pb2_grpc  # noqa: E402
from flask_core.grpc_tls import bind_secure_port, default_server_options  # noqa: E402

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): shared global before_request rate-limit hook -- see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
grpc_server = None


@app.before_serving
async def startup():
    global dal, grpc_server
    logger.system("Starting identity_core_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    if not Config.JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET must be configured for Identity gRPC authentication"
        )

    from grpc import aio

    servicer = IdentityServiceServicer(dal=dal, logger=logger)
    grpc_server = aio.server(options=default_server_options())
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(
        servicer, grpc_server
    )
    listen_address = f"0.0.0.0:{Config.GRPC_PORT}"
    bind_secure_port(grpc_server, listen_address)
    await grpc_server.start()
    logger.system(
        "Identity gRPC server started (TLS)",
        action="grpc_startup",
        address=listen_address,
    )

    logger.system("identity_core_module started", result="SUCCESS")


@app.after_serving
async def shutdown():
    global grpc_server
    if grpc_server:
        logger.system("Stopping gRPC server", action="grpc_shutdown")
        await grpc_server.stop(grace=5)


@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({"status": "operational", "module": Config.MODULE_NAME})


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
