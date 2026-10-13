"""
GCP Functions Action Module - Main Application
Receives tasks from processor/router via gRPC and pushes actions to GCP Cloud Functions.

This is a stateless, clusterable container with:
- gRPC server for receiving tasks
- REST API for third-party use
- JWT authentication
- PyDAL for database operations
"""
import asyncio
import logging
import logging.handlers
import os
import sys
import json
from datetime import datetime
from typing import Dict, Any, Optional

from quart import Quart, request, jsonify
from pydal import DAL

from config import Config
from services.gcp_functions_service import GCPFunctionsService
from services.auth_service import AuthService
from services.grpc_handler import GCPFunctionsActionServicer, GrpcServer

# Initialize Flask/Quart app
app = Quart(__name__)
app.config.from_object(Config)


# Setup logging
def setup_logging():
    """Setup comprehensive AAA logging."""
    log_format = (
        "[%(asctime)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d "
        "%(message)s"
    )

    formatter = logging.Formatter(log_format)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # File handler with rotation
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        f"{Config.LOG_DIR}/gcp_functions_action.log",
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(formatter)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper()))
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Optional syslog
    if Config.ENABLE_SYSLOG:
        try:
            syslog_handler = logging.handlers.SysLogHandler(
                address=(Config.SYSLOG_HOST, Config.SYSLOG_PORT),
                facility=getattr(
                    logging.handlers.SysLogHandler,
                    f"LOG_{Config.SYSLOG_FACILITY}"
                )
            )
            syslog_handler.setFormatter(formatter)
            root_logger.addHandler(syslog_handler)
        except Exception as e:
            print(f"Failed to setup syslog: {e}")


setup_logging()
logger = logging.getLogger(__name__)

# Initialize database
db = DAL(Config.DATABASE_URL, folder="databases", pool_size=10)

# Define database tables (migrate=False since table may already exist)
db.define_table(
    "gcp_function_invocations",
    db.Field("execution_id", "string", length=255, unique=True),
    db.Field("project_id", "string", length=255),
    db.Field("region", "string", length=100),
    db.Field("function_name", "string", length=255),
    db.Field("payload", "text"),
    db.Field("status_code", "integer"),
    db.Field("success", "boolean"),
    db.Field("response", "text"),
    db.Field("error", "text"),
    db.Field("execution_time_ms", "integer"),
    db.Field("created_at", "datetime", default=datetime.utcnow),
    migrate=False  # Don't try to migrate - table already exists
)

# Initialize services
gcp_service = GCPFunctionsService()
grpc_servicer = GCPFunctionsActionServicer(gcp_service)
grpc_server = GrpcServer(grpc_servicer, Config.GRPC_PORT)


# Authentication decorator
def require_auth(f):
    """Decorator for JWT authentication."""
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = AuthService.verify_token(token)

        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        # Store payload in request context
        request.auth_payload = payload
        return await f(*args, **kwargs)

    decorated_function.__name__ = f.__name__
    return decorated_function

# REST API Endpoints


@app.route("/health", methods=["GET"])
async def health():
    """Health check endpoint."""
    try:
        # Check database connectivity
        db.executesql("SELECT 1")

        return jsonify({
            "status": "healthy",
            "module": Config.MODULE_NAME,
            "version": Config.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "database": "connected",
            "gcp_project": Config.GCP_PROJECT_ID,
            "gcp_region": Config.GCP_REGION,
            "grpc_port": Config.GRPC_PORT,
            "rest_port": Config.REST_PORT
        })
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 503


@app.route("/api/v1/auth/token", methods=["POST"])
async def generate_token():
    """Generate JWT token for API access."""
    data = await request.get_json()

    # Validate API key
    api_key = data.get("api_key", "")

    if not AuthService.validate_api_key(api_key):
        return jsonify({"error": "Invalid API key"}), 401

    # Create token
    token = AuthService.create_service_token(
        data.get("service", "unknown"),
        data.get("permissions", ["invoke_functions"])
    )

    return jsonify({
        "token": token,
        "expires_in": Config.JWT_EXPIRATION_SECONDS
    })


@app.route("/api/v1/functions/invoke", methods=["POST"])
@require_auth
async def invoke_function():
    """Invoke a Cloud Function via REST API."""
    try:
        data = await request.get_json()

        project = data.get("project", Config.GCP_PROJECT_ID)
        region = data.get("region", Config.GCP_REGION)
        function_name = data.get("function_name")
        payload = data.get("payload", {})
        headers = data.get("headers")

        if not function_name:
            return jsonify({"error": "function_name is required"}), 400

        logger.info(f"REST API invoke: {function_name} in {project}/{region}")

        # Invoke function
        result = await gcp_service.invoke_function(
            project,
            region,
            function_name,
            payload,
            headers
        )

        # Store invocation record
        execution_id = f"{function_name}_{int(datetime.utcnow().timestamp())}"
        db.gcp_function_invocations.insert(
            execution_id=execution_id,
            project_id=project,
            region=region,
            function_name=function_name,
            payload=json.dumps(payload),
            status_code=result.get("status_code", 500),
            success=result.get("success", False),
            response=result.get("response", ""),
            error=result.get("error", ""),
            execution_time_ms=result.get("execution_time_ms", 0)
        )
        db.commit()

        return jsonify({
            **result,
            "execution_id": execution_id
        })

    except Exception as e:
        logger.error(f"Function invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/functions/invoke-http", methods=["POST"])
@require_auth
async def invoke_http_function():
    """Invoke an HTTP-triggered function via REST API."""
    try:
        data = await request.get_json()

        url = data.get("url")
        payload = data.get("payload")
        method = data.get("method", "POST")
        headers = data.get("headers")
        timeout = data.get("timeout")

        if not url:
            return jsonify({"error": "url is required"}), 400

        logger.info(f"REST API HTTP invoke: {method} {url}")

        # Invoke HTTP function
        result = await gcp_service.invoke_http_function(
            url,
            payload,
            method,
            headers,
            timeout
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"HTTP function invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/functions/batch", methods=["POST"])
@require_auth
async def batch_invoke_functions():
    """Batch invoke multiple Cloud Functions via REST API."""
    try:
        data = await request.get_json()
        invocations = data.get("invocations", [])

        if not invocations:
            return jsonify({"error": "No invocations provided"}), 400

        if len(invocations) > Config.MAX_BATCH_SIZE:
            return jsonify({
                "error": f"Batch size exceeds maximum of {Config.MAX_BATCH_SIZE}"
            }), 400

        logger.info(f"REST API batch invoke: {len(invocations)} functions")

        # Execute all invocations concurrently
        tasks = []
        for inv in invocations:
            project = inv.get("project", Config.GCP_PROJECT_ID)
            region = inv.get("region", Config.GCP_REGION)
            function_name = inv.get("function_name")
            payload = inv.get("payload", {})
            headers = inv.get("headers")

            if function_name:
                task = gcp_service.invoke_function(
                    project,
                    region,
                    function_name,
                    payload,
                    headers
                )
                tasks.append(task)

        # Wait for all to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes and failures
        success_count = 0
        failure_count = 0
        responses = []

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failure_count += 1
                responses.append({
                    "success": False,
                    "error": str(result)
                })
            else:
                if result.get("success"):
                    success_count += 1
                else:
                    failure_count += 1
                responses.append(result)

        return jsonify({
            "responses": responses,
            "total_count": len(invocations),
            "success_count": success_count,
            "failure_count": failure_count
        })

    except Exception as e:
        logger.error(f"Batch invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/functions/list", methods=["GET"])
@require_auth
async def list_functions():
    """List Cloud Functions via REST API."""
    try:
        project = request.args.get("project", Config.GCP_PROJECT_ID)
        region = request.args.get("region", Config.GCP_REGION)

        logger.info(f"REST API list functions: {project}/{region}")

        # List functions
        functions = await gcp_service.list_functions(project, region)

        return jsonify({
            "project": project,
            "region": region,
            "functions": functions,
            "count": len(functions)
        })

    except Exception as e:
        logger.error(f"List functions failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/functions/<function_name>/details", methods=["GET"])
@require_auth
async def get_function_details(function_name: str):
    """Get function details via REST API."""
    try:
        project = request.args.get("project", Config.GCP_PROJECT_ID)
        region = request.args.get("region", Config.GCP_REGION)

        logger.info(f"REST API get function details: {function_name} in {project}/{region}")

        # Get function details
        details = await gcp_service.get_function_details(project, region, function_name)

        if details:
            return jsonify({
                "success": True,
                "function": details
            })
        else:
            return jsonify({
                "success": False,
                "error": "Function not found"
            }), 404

    except Exception as e:
        logger.error(f"Get function details failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/stats", methods=["GET"])
@require_auth
async def get_stats():
    """Get module statistics."""
    try:
        # Count invocations
        total_invocations = db(db.gcp_function_invocations).count()
        successful_invocations = db(db.gcp_function_invocations.success == True).count()
        failed_invocations = db(db.gcp_function_invocations.success == False).count()

        # Average execution time
        avg_time = db.gcp_function_invocations.execution_time_ms.avg()
        avg_execution_time = db(db.gcp_function_invocations).select(avg_time).first()[avg_time]

        return jsonify({
            "module": Config.MODULE_NAME,
            "version": Config.MODULE_VERSION,
            "stats": {
                "total_invocations": total_invocations,
                "successful_invocations": successful_invocations,
                "failed_invocations": failed_invocations,
                "average_execution_time_ms": int(avg_execution_time or 0),
                "grpc_port": Config.GRPC_PORT,
                "rest_port": Config.REST_PORT,
                "gcp_project": Config.GCP_PROJECT_ID,
                "gcp_region": Config.GCP_REGION
            },
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.error(f"Stats retrieval failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# Application Lifecycle


@app.before_serving
async def startup():
    """Application startup."""
    logger.info(f"Starting {Config.MODULE_NAME} v{Config.MODULE_VERSION}")

    # Validate configuration
    try:
        Config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Start gRPC server
    await grpc_server.start()

    logger.info(
        f"GCP Functions Action Module started - "
        f"REST: {Config.REST_PORT}, gRPC: {Config.GRPC_PORT}"
    )


@app.after_serving
async def shutdown():
    """Application shutdown."""
    logger.info("Shutting down GCP Functions Action Module")

    # Close GCP service session
    await gcp_service.close()

    # Stop gRPC server
    await grpc_server.stop()

    # Close database
    db.close()

    logger.info("GCP Functions Action Module stopped")


if __name__ == "__main__":
    # Run with Hypercorn in production
    # hypercorn app:app --bind 0.0.0.0:8081 --workers 4
    app.run(host="0.0.0.0", port=Config.REST_PORT, debug=False)
