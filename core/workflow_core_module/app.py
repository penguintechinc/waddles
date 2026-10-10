"""Workflow Core Module - Quart Application with gRPC"""
import asyncio
import os
import sys
import threading

from quart import Blueprint, Quart

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from config import Config  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint, create_health_blueprint, init_database, install_rate_limiting,
    install_security_headers, setup_aaa_logging, success_response,
    describe_db_error,
)

# Import services
from services.license_service import LicenseService  # noqa: E402
from services.permission_service import PermissionService  # noqa: E402
from services.validation_service import WorkflowValidationService  # noqa: E402
from services.workflow_service import WorkflowService  # noqa: E402
from services.grpc_handler import WorkflowServiceServicer, WorkflowGrpcService  # noqa: E402

# Import controllers
from controllers.workflow_api import register_workflow_api  # noqa: E402
from controllers.execution_api import register_execution_api  # noqa: E402

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): every workflow/execution CRUD route had zero rate
# limiting -- shared global before_request hook, see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME, redis_url=Config.REDIS_URL)

# API Blueprint for v1
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')

# Initialize logging
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Service instances
dal = None
license_service = None
permission_service = None
validation_service = None
workflow_service = None
workflow_engine = None
grpc_server = None
grpc_thread = None


def _run_grpc_server(
    workflow_service,
    permission_service,
    workflow_engine,
    secret_key,
    grpc_port
):
    """
    Run gRPC server in a separate thread.

    Args:
        workflow_service: WorkflowService instance
        permission_service: PermissionService instance
        workflow_engine: WorkflowEngine instance
        secret_key: Secret key for JWT validation
        grpc_port: Port for gRPC server
    """
    import grpc
    from flask_core.grpc_tls import bind_secure_port, default_server_options

    try:
        import workflow_pb2_grpc

        # Create event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Create gRPC service handler
        handler = WorkflowServiceServicer(
            workflow_engine=workflow_engine,
            workflow_service=workflow_service,
            permission_service=permission_service,
            secret_key=secret_key,
            logger_instance=logger
        )

        # Create gRPC service
        grpc_service = WorkflowGrpcService(handler)

        # Create gRPC server
        server = grpc.aio.server(options=default_server_options())
        workflow_pb2_grpc.add_WorkflowServiceServicer_to_server(
            grpc_service,
            server
        )

        # Add port and start
        bind_secure_port(server, f"[::]:{grpc_port}")

        logger.system(
            f"Starting gRPC server (TLS) on port {grpc_port}",
            action="grpc_startup"
        )

        # Run server in event loop
        loop.run_until_complete(server.start())
        loop.run_forever()

    except Exception as e:
        logger.error(
            f"Failed to start gRPC server: {describe_db_error(e)}",
            extra={
                "action": "grpc_startup",
                "result": "FAILURE"
            }
        )


@app.before_serving
async def startup():
    """Initialize application on startup"""
    global dal, license_service, permission_service, validation_service, workflow_service, workflow_engine, grpc_server, grpc_thread

    logger.system(
        "Starting workflow_core_module",
        action="startup",
        extra={
            "rest_port": Config.PORT,
            "grpc_port": Config.GRPC_PORT,
            "feature_workflows": Config.FEATURE_WORKFLOWS_ENABLED
        }
    )
    try:
        # Initialize database. `tenant_middleware` (added for the A01
        # community-scoping fix below) calls `dal(query)` directly, which
        # only the raw pydal `DAL` supports -- `AsyncDAL` has no `__call__`.
        # Store both under the same `dal`/`async_dal` config keys
        # `core/svc_streaming`, `core/svc_presentation`, and
        # `security_core_module` already use for this identical split;
        # `PermissionService`/`WorkflowService`/`WorkflowEngine` only ever
        # call `.executesql()` (never an `AsyncDAL`-only async method), so
        # the raw pydal object works for them unchanged.
        async_dal = init_database(Config.DATABASE_URI)
        dal = async_dal.dal
        app.config['dal'] = dal
        app.config['async_dal'] = async_dal

        # Initialize services
        license_service = LicenseService(
            license_server_url=Config.LICENSE_SERVER_URL,
            redis_url=Config.REDIS_URL,
            release_mode=Config.RELEASE_MODE,
            logger_instance=logger
        )
        await license_service.connect()

        permission_service = PermissionService(dal=dal, logger=logger)
        app.config['permission_service'] = permission_service

        validation_service = WorkflowValidationService()

        workflow_service = WorkflowService(
            dal=dal,
            license_service=license_service,
            permission_service=permission_service,
            validation_service=validation_service,
            logger_instance=logger
        )
        app.config['workflow_service'] = workflow_service

        # Initialize workflow engine
        from services.workflow_engine import WorkflowEngine
        workflow_engine = WorkflowEngine(
            dal=dal,
            router_url=Config.ROUTER_URL,
            max_loop_iterations=Config.MAX_LOOP_ITERATIONS,
            max_total_operations=Config.MAX_TOTAL_OPERATIONS,
            max_loop_depth=Config.MAX_LOOP_DEPTH,
            default_timeout=Config.WORKFLOW_TIMEOUT,
            max_parallel_nodes=Config.MAX_PARALLEL_NODES
        )
        app.config['workflow_engine'] = workflow_engine

        # Register workflow API
        register_workflow_api(app, workflow_service)

        # Register execution API
        register_execution_api(app, workflow_engine)

        # Start gRPC server in background thread
        try:
            grpc_thread = threading.Thread(
                target=_run_grpc_server,
                args=(
                    workflow_service,
                    permission_service,
                    workflow_engine,
                    Config.SECRET_KEY,
                    Config.GRPC_PORT
                ),
                daemon=True,
                name="grpc_server_thread"
            )
            grpc_thread.start()

            logger.system(
                "gRPC server started in background thread",
                action="grpc_startup",
                result="SUCCESS"
            )
        except Exception as e:
            logger.warning(
                f"Failed to start gRPC server: {describe_db_error(e)}",
                extra={
                    "action": "grpc_startup",
                    "result": "FAILURE"
                }
            )
            # Don't fail startup if gRPC fails - REST API should still work

        logger.system("workflow_core_module started successfully", result="SUCCESS")
    except Exception as e:
        logger.error(
            f"Failed to start workflow_core_module: {describe_db_error(e)}",
            result="FAILURE",
        )
        raise


@app.after_serving
async def shutdown():
    """Cleanup on shutdown"""
    global license_service, workflow_engine, grpc_server, grpc_thread

    logger.system("Shutting down workflow_core_module", action="shutdown")
    try:
        # Shutdown gRPC server
        if grpc_server:
            try:
                await grpc_server.stop(0)
                logger.system("gRPC server stopped", action="grpc_shutdown", result="SUCCESS")
            except Exception as e:
                logger.warning(
                    f"Error stopping gRPC server: {describe_db_error(e)}",
                    action="grpc_shutdown",
                )

        # Shutdown workflow engine
        if workflow_engine:
            workflow_engine.shutdown()

        # Disconnect license service
        if license_service:
            await license_service.disconnect()

        # Close database connection if needed
        if dal:
            pass

        logger.system("workflow_core_module shutdown complete", result="SUCCESS")
    except Exception as e:
        logger.error(f"Error during shutdown: {describe_db_error(e)}", result="FAILURE")


@api_bp.route('/status', methods=['GET'])
@async_endpoint
async def status():
    """Get workflow module status"""
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "features": {
            "workflows_enabled": Config.FEATURE_WORKFLOWS_ENABLED,
            "release_mode": Config.RELEASE_MODE
        }
    })


@api_bp.route('/health', methods=['GET'])
@async_endpoint
async def health_check():
    """Health check endpoint"""
    return success_response({
        "healthy": True,
        "module": Config.MODULE_NAME
    })


# Register API blueprint
app.register_blueprint(api_bp)


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
