"""
Combined Serverless Action Module - Unified Quart Application

Merges three serverless action modules into a single service:
- AWS Lambda invocation (port 8103, API v1, gRPC 50051)
- Apache OpenWhisk invocation (port 8103, API v1, gRPC 50052)
- GCP Cloud Functions invocation (port 8103, API v1, gRPC 50053)

Single REST port (8103) with module-prefixed routes:
/api/v1/lambda/* -> Lambda service
/api/v1/openwhisk/* -> OpenWhisk service
/api/v1/gcp/* -> GCP service

Each module has independent gRPC server on separate ports.
"""

import asyncio
import logging
import logging.handlers
import os
import sys
from concurrent import futures
from datetime import datetime

import grpc
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart, jsonify, request

# Initialize Quart app
app = Quart(__name__)


# Configure logging
def setup_logging():
    """Setup comprehensive logging to stdout only (container best practice)."""
    log_format = (
        "[%(asctime)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d "
        "%(message)s"
    )
    formatter = logging.Formatter(log_format)

    # Console handler only - container logs are captured by orchestrator
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)


setup_logging()
logger = logging.getLogger(__name__)

# ============================================================================
# LAMBDA MODULE
# ============================================================================

try:
    from lambda_action_module.config import Config as LambdaConfig
    from lambda_action_module.services.lambda_service import LambdaService
    from lambda_action_module.services.grpc_handler import LambdaActionServicer
    from lambda_action_module.services.grpc_auth_interceptor import (
        AuthInterceptor as LambdaAuthInterceptor,
    )
    from lambda_action_module.app import verify_jwt as lambda_verify_jwt
    from penguin_dal import DAL

    lambda_enabled = True
    lambda_db = DAL(LambdaConfig.DATABASE_URL, folder=None, pool_size=10, migrate_enabled=False)
    lambda_service = LambdaService(lambda_db)
    logger.info("Lambda module initialized")
except Exception as e:
    lambda_enabled = False
    logger.warning(f"Lambda module initialization failed: {e}")


# Lambda REST endpoints
@app.route("/api/v1/lambda/health", methods=["GET"])
async def lambda_health():
    """Lambda health check endpoint."""
    if not lambda_enabled:
        return jsonify({"status": "disabled", "module": "lambda"}), 503
    try:
        lambda_db.executesql("SELECT 1")
        return jsonify({
            "status": "healthy",
            "module": "lambda",
            "version": LambdaConfig.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.error(f"Lambda health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@app.route("/api/v1/lambda/token", methods=["POST"])
async def lambda_generate_token():
    """Generate JWT token for Lambda authentication."""
    if not lambda_enabled:
        return jsonify({"error": "Lambda module disabled"}), 503
    try:
        from datetime import timedelta
        import jwt
        data = await request.get_json()
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")

        if not client_id or not client_secret:
            return jsonify({"error": "Missing client_id or client_secret"}), 400

        payload = {
            "client_id": client_id,
            "exp": datetime.utcnow() + timedelta(seconds=LambdaConfig.JWT_EXPIRATION_SECONDS),
            "iat": datetime.utcnow(),
        }
        token = jwt.encode(payload, LambdaConfig.MODULE_SECRET_KEY, algorithm=LambdaConfig.JWT_ALGORITHM)

        return jsonify({"token": token, "expires_in": LambdaConfig.JWT_EXPIRATION_SECONDS})
    except Exception as e:
        logger.error(f"Lambda token generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/lambda/invoke", methods=["POST"])
async def lambda_invoke():
    """Invoke Lambda function."""
    if not lambda_enabled:
        return jsonify({"error": "Lambda module disabled"}), 503
    try:
        data = await request.get_json()
        function_name = data.get("function_name")
        payload = data.get("payload")

        if not function_name or not payload:
            return jsonify({"error": "Missing function_name or payload"}), 400

        invocation_type = data.get("invocation_type", "RequestResponse")
        alias = data.get("alias")
        version = data.get("version")

        success, status_code, response_payload, func_error, log_result, exec_version = \
            await lambda_service.invoke_function(
                function_name, payload, invocation_type, alias, version
            )

        if success:
            return jsonify({
                "success": True,
                "status_code": status_code,
                "payload": response_payload,
                "executed_version": exec_version,
                "log_result": log_result
            })
        else:
            return jsonify({
                "success": False,
                "error": func_error or exec_version,
                "status_code": status_code
            }), 500
    except Exception as e:
        logger.error(f"Lambda invocation failed: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# OPENWHISK MODULE
# ============================================================================

try:
    from openwhisk_action_module.config import Config as OpenWhiskConfig
    from openwhisk_action_module.services.openwhisk_service import OpenWhiskService
    from openwhisk_action_module.services.auth_service import AuthService
    from openwhisk_action_module.services.grpc_handler import OpenWhiskActionServicer
    from openwhisk_action_module.services.grpc_auth_interceptor import (
        AuthInterceptor as OpenWhiskAuthInterceptor,
    )

    openwhisk_enabled = True
    openwhisk_service = OpenWhiskService()
    openwhisk_auth_service = AuthService()
    logger.info("OpenWhisk module initialized")
except Exception as e:
    openwhisk_enabled = False
    logger.warning(f"OpenWhisk module initialization failed: {e}")


# OpenWhisk REST endpoints
@app.route("/api/v1/openwhisk/health", methods=["GET"])
async def openwhisk_health():
    """OpenWhisk health check endpoint."""
    if not openwhisk_enabled:
        return jsonify({"status": "disabled", "module": "openwhisk"}), 503
    try:
        return jsonify({
            "status": "healthy",
            "module": "openwhisk",
            "version": OpenWhiskConfig.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "openwhisk_api_host": OpenWhiskConfig.OPENWHISK_API_HOST,
            "namespace": OpenWhiskConfig.OPENWHISK_NAMESPACE,
        })
    except Exception as e:
        logger.error(f"OpenWhisk health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@app.route("/api/v1/openwhisk/token", methods=["POST"])
async def openwhisk_generate_token():
    """Generate JWT token for OpenWhisk authentication."""
    if not openwhisk_enabled:
        return jsonify({"error": "OpenWhisk module disabled"}), 503
    try:
        data = await request.get_json()
        api_key = data.get("api_key", "")

        if not openwhisk_auth_service.validate_api_key(api_key):
            return jsonify({"error": "Invalid API key"}), 401

        token = openwhisk_auth_service.create_token({
            "service": data.get("service", "unknown"),
            "permissions": ["execute_actions"]
        })

        return jsonify({
            "token": token,
            "expires_in": OpenWhiskConfig.JWT_EXPIRATION_SECONDS
        })
    except Exception as e:
        logger.error(f"OpenWhisk token generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/openwhisk/invoke", methods=["POST"])
async def openwhisk_invoke():
    """Invoke OpenWhisk action."""
    if not openwhisk_enabled:
        return jsonify({"error": "OpenWhisk module disabled"}), 503
    try:
        data = await request.get_json()
        namespace = data.get("namespace", OpenWhiskConfig.OPENWHISK_NAMESPACE)
        action_name = data.get("action_name")
        payload = data.get("payload", {})
        blocking = data.get("blocking", True)
        timeout = data.get("timeout")

        if not action_name:
            return jsonify({"error": "action_name is required"}), 400

        result = await openwhisk_service.invoke_action(
            namespace, action_name, payload, blocking, timeout
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"OpenWhisk invocation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# GCP CLOUD FUNCTIONS MODULE
# ============================================================================

try:
    from gcp_functions_action_module.config import Config as GCPConfig
    from gcp_functions_action_module.services.gcp_functions_service import GCPFunctionsService
    from gcp_functions_action_module.services.auth_service import AuthService as GCPAuthService
    from gcp_functions_action_module.services.grpc_handler import GCPFunctionsActionServicer
    from gcp_functions_action_module.services.grpc_auth_interceptor import (
        AuthInterceptor as GCPAuthInterceptor,
    )

    gcp_enabled = True
    gcp_service = GCPFunctionsService()
    gcp_auth_service = GCPAuthService()
    logger.info("GCP Cloud Functions module initialized")
except Exception as e:
    gcp_enabled = False
    logger.warning(f"GCP Cloud Functions module initialization failed: {e}")


# GCP REST endpoints
@app.route("/api/v1/gcp/health", methods=["GET"])
async def gcp_health():
    """GCP Cloud Functions health check endpoint."""
    if not gcp_enabled:
        return jsonify({"status": "disabled", "module": "gcp"}), 503
    try:
        return jsonify({
            "status": "healthy",
            "module": "gcp",
            "version": GCPConfig.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "gcp_project": GCPConfig.GCP_PROJECT_ID,
            "gcp_region": GCPConfig.GCP_REGION,
        })
    except Exception as e:
        logger.error(f"GCP health check failed: {e}")
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


@app.route("/api/v1/gcp/token", methods=["POST"])
async def gcp_generate_token():
    """Generate JWT token for GCP authentication."""
    if not gcp_enabled:
        return jsonify({"error": "GCP module disabled"}), 503
    try:
        data = await request.get_json()
        api_key = data.get("api_key", "")

        if not GCPAuthService.validate_api_key(api_key):
            return jsonify({"error": "Invalid API key"}), 401

        token = GCPAuthService.create_service_token(
            data.get("service", "unknown"),
            data.get("permissions", ["invoke_functions"])
        )

        return jsonify({
            "token": token,
            "expires_in": GCPConfig.JWT_EXPIRATION_SECONDS
        })
    except Exception as e:
        logger.error(f"GCP token generation failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/gcp/invoke", methods=["POST"])
async def gcp_invoke():
    """Invoke GCP Cloud Function."""
    if not gcp_enabled:
        return jsonify({"error": "GCP module disabled"}), 503
    try:
        data = await request.get_json()
        project = data.get("project", GCPConfig.GCP_PROJECT_ID)
        region = data.get("region", GCPConfig.GCP_REGION)
        function_name = data.get("function_name")
        payload = data.get("payload", {})
        headers = data.get("headers")

        if not function_name:
            return jsonify({"error": "function_name is required"}), 400

        result = await gcp_service.invoke_function(
            project, region, function_name, payload, headers
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"GCP invocation failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# UNIFIED HEALTH ENDPOINT
# ============================================================================

@app.route("/health", methods=["GET"])
async def unified_health():
    """Unified health check for all modules."""
    return jsonify({
        "status": "healthy",
        "service": "action-serverless",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
        "modules": {
            "lambda": "enabled" if lambda_enabled else "disabled",
            "openwhisk": "enabled" if openwhisk_enabled else "disabled",
            "gcp": "enabled" if gcp_enabled else "disabled",
        }
    })


@app.route("/api/v1/health", methods=["GET"])
async def api_health():
    """API health check endpoint."""
    return await unified_health()


# ============================================================================
# gRPC SERVERS (concurrent with REST)
# ============================================================================

async def start_grpc_servers():
    """Start gRPC servers for each module on separate ports."""
    tasks = []

    # Lambda gRPC (port 50051)
    if lambda_enabled:
        try:
            async def serve_lambda_grpc():
                from lambda_action_module.grpc_proto import lambda_action_pb2_grpc
                from lambda_action_module.services.grpc_tls import (
                    bind_secure_port,
                    default_server_options,
                )
                server = grpc.aio.server(
                    futures.ThreadPoolExecutor(max_workers=10),
                    interceptors=[LambdaAuthInterceptor()],
                    options=default_server_options(),
                )
                lambda_action_pb2_grpc.add_LambdaActionServicer_to_server(
                    LambdaActionServicer(lambda_service), server
                )
                bind_secure_port(server, "0.0.0.0:50051")
                logger.info("Starting Lambda gRPC server (TLS) on 0.0.0.0:50051")
                await server.start()
                await server.wait_for_termination()

            tasks.append(serve_lambda_grpc())
        except Exception as e:
            logger.error(f"Failed to start Lambda gRPC: {e}")

    # OpenWhisk gRPC (port 50052)
    if openwhisk_enabled:
        try:
            async def serve_openwhisk_grpc():
                from openwhisk_action_module.services.grpc_handler import GrpcServer
                grpc_server = GrpcServer(
                    OpenWhiskActionServicer(openwhisk_service),
                    50052,
                    interceptors=[OpenWhiskAuthInterceptor()],
                )
                await grpc_server.start()
                logger.info("Starting OpenWhisk gRPC server on 0.0.0.0:50052")
                # Keep running
                while True:
                    await asyncio.sleep(1)

            tasks.append(serve_openwhisk_grpc())
        except Exception as e:
            logger.error(f"Failed to start OpenWhisk gRPC: {e}")

    # GCP gRPC (port 50053)
    if gcp_enabled:
        try:
            async def serve_gcp_grpc():
                from gcp_functions_action_module.services.grpc_handler import GrpcServer
                grpc_server = GrpcServer(
                    GCPFunctionsActionServicer(gcp_service),
                    50053,
                    interceptors=[GCPAuthInterceptor()],
                )
                await grpc_server.start()
                logger.info("Starting GCP gRPC server on 0.0.0.0:50053")
                # Keep running
                while True:
                    await asyncio.sleep(1)

            tasks.append(serve_gcp_grpc())
        except Exception as e:
            logger.error(f"Failed to start GCP gRPC: {e}")

    if tasks:
        await asyncio.gather(*tasks)


# ============================================================================
# APPLICATION STARTUP/SHUTDOWN
# ============================================================================

async def serve_rest():
    """Start REST API server on port 8103."""
    config = HypercornConfig()
    config.bind = ["0.0.0.0:8103"]
    config.workers = 4
    logger.info("Starting REST API server on 0.0.0.0:8103")
    await serve(app, config)


async def main():
    """Main entry point."""
    logger.info("Starting combined serverless action module")
    logger.info(f"Enabled modules: Lambda={lambda_enabled}, OpenWhisk={openwhisk_enabled}, GCP={gcp_enabled}")

    try:
        # Run gRPC and REST servers concurrently
        await asyncio.gather(
            start_grpc_servers(),
            serve_rest()
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        # Cleanup
        if lambda_enabled:
            await lambda_service.close()
            lambda_db.close()
        if openwhisk_enabled:
            await openwhisk_service.close()
        if gcp_enabled:
            await gcp_service.close()
        logger.info("Combined serverless action module stopped")


if __name__ == "__main__":
    asyncio.run(main())
