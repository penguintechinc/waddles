"""
OpenWhisk Action Module - Main Application
Receives tasks from processor/router via gRPC and pushes actions to OpenWhisk.

This is a stateless, clusterable container with:
- gRPC server for receiving tasks
- REST API for third-party use
- JWT authentication
- PyDAL for database operations
"""
import asyncio
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from quart import Quart, request, jsonify
from pydal import DAL

from config import Config
from services.openwhisk_service import OpenWhiskService
from services.auth_service import AuthService
from services.grpc_handler import OpenWhiskActionServicer, GrpcServer

# Initialize Quart app
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
        f"{Config.LOG_DIR}/openwhisk_action.log",
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

# Define database tables (migrate=True creates table if not exists, won't fail if exists)
db.define_table(
    "openwhisk_action_executions",
    db.Field("execution_id", "string", required=True),
    db.Field("namespace", "string", required=True),
    db.Field("action_name", "string", required=True),
    db.Field("action_type", "string", required=True),  # action, sequence, web_action, trigger
    db.Field("payload", "text"),
    db.Field("blocking", "boolean", default=True),
    db.Field("timeout", "integer"),
    db.Field("activation_id", "string"),
    db.Field("result", "text"),
    db.Field("duration_ms", "integer"),
    db.Field("status", "string"),
    db.Field("success", "boolean"),
    db.Field("error", "text"),
    db.Field("created_at", "datetime", default=lambda: datetime.utcnow()),
    db.Field("completed_at", "datetime"),
    migrate=False  # Don't try to migrate - table already exists
)

# Initialize services
openwhisk_service = OpenWhiskService()
auth_service = AuthService()
grpc_servicer = OpenWhiskActionServicer(openwhisk_service)
grpc_server = GrpcServer(grpc_servicer, Config.GRPC_PORT)


# Authentication decorator
def require_auth(f):
    """Decorator for JWT authentication."""
    async def decorated_function(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = auth_service.verify_token(token)

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
            "grpc_port": Config.GRPC_PORT,
            "rest_port": Config.REST_PORT,
            "openwhisk_api_host": Config.OPENWHISK_API_HOST,
            "namespace": Config.OPENWHISK_NAMESPACE
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

    api_key = data.get("api_key", "")

    if not auth_service.validate_api_key(api_key):
        logger.warning("Invalid API key attempt")
        return jsonify({"error": "Invalid API key"}), 401

    token = auth_service.create_token({
        "service": data.get("service", "unknown"),
        "permissions": ["execute_actions"]
    })

    logger.info(f"JWT token generated for service: {data.get('service', 'unknown')}")

    return jsonify({
        "token": token,
        "expires_in": Config.JWT_EXPIRATION_SECONDS
    })


@app.route("/api/v1/actions/invoke", methods=["POST"])
@require_auth
async def invoke_action():
    """Invoke OpenWhisk action via REST API."""
    try:
        data = await request.get_json()

        namespace = data.get("namespace", Config.OPENWHISK_NAMESPACE)
        action_name = data.get("action_name")
        payload = data.get("payload", {})
        blocking = data.get("blocking", True)
        timeout = data.get("timeout")

        if not action_name:
            return jsonify({"error": "action_name is required"}), 400

        execution_id = f"exec_{datetime.utcnow().timestamp()}"

        # Store execution record
        db.openwhisk_action_executions.insert(
            execution_id=execution_id,
            namespace=namespace,
            action_name=action_name,
            action_type="action",
            payload=json.dumps(payload),
            blocking=blocking,
            timeout=timeout
        )
        db.commit()

        # Invoke action
        result = await openwhisk_service.invoke_action(
            namespace,
            action_name,
            payload,
            blocking,
            timeout
        )

        # Update execution record
        db(db.openwhisk_action_executions.execution_id == execution_id).update(
            activation_id=result.get("activation_id", ""),
            result=json.dumps(result.get("result", {})),
            duration_ms=int(result.get("duration", 0)),
            status=result.get("status", ""),
            success=result.get("success", False),
            error=result.get("error", ""),
            completed_at=datetime.utcnow()
        )
        db.commit()

        return jsonify({
            "execution_id": execution_id,
            **result
        })

    except Exception as e:
        logger.error(f"Action invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/actions/invoke-async", methods=["POST"])
@require_auth
async def invoke_action_async():
    """Invoke OpenWhisk action asynchronously via REST API."""
    try:
        data = await request.get_json()

        namespace = data.get("namespace", Config.OPENWHISK_NAMESPACE)
        action_name = data.get("action_name")
        payload = data.get("payload", {})

        if not action_name:
            return jsonify({"error": "action_name is required"}), 400

        execution_id = f"exec_{datetime.utcnow().timestamp()}"

        # Store execution record
        db.openwhisk_action_executions.insert(
            execution_id=execution_id,
            namespace=namespace,
            action_name=action_name,
            action_type="action",
            payload=json.dumps(payload),
            blocking=False
        )
        db.commit()

        # Invoke action asynchronously
        result = await openwhisk_service.invoke_action_async(
            namespace,
            action_name,
            payload
        )

        # Update execution record
        db(db.openwhisk_action_executions.execution_id == execution_id).update(
            activation_id=result.get("activation_id", ""),
            success=result.get("success", False),
            error=result.get("error", ""),
            completed_at=datetime.utcnow()
        )
        db.commit()

        return jsonify({
            "execution_id": execution_id,
            **result
        })

    except Exception as e:
        logger.error(f"Async action invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/sequences/invoke", methods=["POST"])
@require_auth
async def invoke_sequence():
    """Invoke OpenWhisk sequence via REST API."""
    try:
        data = await request.get_json()

        namespace = data.get("namespace", Config.OPENWHISK_NAMESPACE)
        sequence_name = data.get("sequence_name")
        payload = data.get("payload", {})

        if not sequence_name:
            return jsonify({"error": "sequence_name is required"}), 400

        execution_id = f"exec_{datetime.utcnow().timestamp()}"

        # Store execution record
        db.openwhisk_action_executions.insert(
            execution_id=execution_id,
            namespace=namespace,
            action_name=sequence_name,
            action_type="sequence",
            payload=json.dumps(payload),
            blocking=True
        )
        db.commit()

        # Invoke sequence
        result = await openwhisk_service.invoke_sequence(
            namespace,
            sequence_name,
            payload
        )

        # Update execution record
        db(db.openwhisk_action_executions.execution_id == execution_id).update(
            activation_id=result.get("activation_id", ""),
            result=json.dumps(result.get("result", {})),
            duration_ms=int(result.get("duration", 0)),
            status=result.get("status", ""),
            success=result.get("success", False),
            error=result.get("error", ""),
            completed_at=datetime.utcnow()
        )
        db.commit()

        return jsonify({
            "execution_id": execution_id,
            **result
        })

    except Exception as e:
        logger.error(f"Sequence invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/web-actions/invoke", methods=["POST"])
@require_auth
async def invoke_web_action():
    """Invoke OpenWhisk web action via REST API."""
    try:
        data = await request.get_json()

        namespace = data.get("namespace", Config.OPENWHISK_NAMESPACE)
        package_name = data.get("package_name", "default")
        action_name = data.get("action_name")
        payload = data.get("payload", {})
        method = data.get("method", "POST")
        headers = data.get("headers", {})

        if not action_name:
            return jsonify({"error": "action_name is required"}), 400

        execution_id = f"exec_{datetime.utcnow().timestamp()}"

        # Store execution record
        db.openwhisk_action_executions.insert(
            execution_id=execution_id,
            namespace=namespace,
            action_name=f"{package_name}/{action_name}",
            action_type="web_action",
            payload=json.dumps(payload)
        )
        db.commit()

        # Invoke web action
        result = await openwhisk_service.invoke_web_action(
            namespace,
            package_name,
            action_name,
            payload,
            method,
            headers
        )

        # Update execution record
        db(db.openwhisk_action_executions.execution_id == execution_id).update(
            result=json.dumps(result.get("response", {})),
            success=result.get("success", False),
            error=result.get("error", ""),
            completed_at=datetime.utcnow()
        )
        db.commit()

        return jsonify({
            "execution_id": execution_id,
            **result
        })

    except Exception as e:
        logger.error(f"Web action invocation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/triggers/fire", methods=["POST"])
@require_auth
async def fire_trigger():
    """Fire OpenWhisk trigger via REST API."""
    try:
        data = await request.get_json()

        namespace = data.get("namespace", Config.OPENWHISK_NAMESPACE)
        trigger_name = data.get("trigger_name")
        payload = data.get("payload", {})

        if not trigger_name:
            return jsonify({"error": "trigger_name is required"}), 400

        execution_id = f"exec_{datetime.utcnow().timestamp()}"

        # Store execution record
        db.openwhisk_action_executions.insert(
            execution_id=execution_id,
            namespace=namespace,
            action_name=trigger_name,
            action_type="trigger",
            payload=json.dumps(payload)
        )
        db.commit()

        # Fire trigger
        result = await openwhisk_service.fire_trigger(
            namespace,
            trigger_name,
            payload
        )

        # Update execution record
        db(db.openwhisk_action_executions.execution_id == execution_id).update(
            activation_id=result.get("activation_id", ""),
            success=result.get("success", False),
            error=result.get("error", ""),
            completed_at=datetime.utcnow()
        )
        db.commit()

        return jsonify({
            "execution_id": execution_id,
            **result
        })

    except Exception as e:
        logger.error(f"Trigger fire failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/activations/<activation_id>", methods=["GET"])
@require_auth
async def get_activation(activation_id: str):
    """Get activation details via REST API."""
    try:
        namespace = request.args.get("namespace", Config.OPENWHISK_NAMESPACE)

        result = await openwhisk_service.get_activation(
            namespace,
            activation_id
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"Get activation failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/actions", methods=["GET"])
@require_auth
async def list_actions():
    """List actions via REST API."""
    try:
        namespace = request.args.get("namespace", Config.OPENWHISK_NAMESPACE)
        limit = int(request.args.get("limit", 30))
        skip = int(request.args.get("skip", 0))

        result = await openwhisk_service.list_actions(
            namespace,
            limit,
            skip
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"List actions failed: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/v1/stats", methods=["GET"])
@require_auth
async def get_stats():
    """Get module statistics."""
    try:
        # Count executions
        total_executions = db(db.openwhisk_action_executions).count()
        successful_executions = db(
            db.openwhisk_action_executions.success == True
        ).count()
        failed_executions = db(
            db.openwhisk_action_executions.success == False
        ).count()

        return jsonify({
            "module": Config.MODULE_NAME,
            "version": Config.MODULE_VERSION,
            "stats": {
                "total_executions": total_executions,
                "successful_executions": successful_executions,
                "failed_executions": failed_executions,
                "grpc_port": Config.GRPC_PORT,
                "rest_port": Config.REST_PORT
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
        f"OpenWhisk Action Module started - "
        f"REST: {Config.REST_PORT}, gRPC: {Config.GRPC_PORT}"
    )


@app.after_serving
async def shutdown():
    """Application shutdown."""
    logger.info("Shutting down OpenWhisk Action Module")

    # Stop gRPC server
    await grpc_server.stop()

    # Close OpenWhisk service session
    await openwhisk_service.close()

    # Close database
    db.close()

    logger.info("OpenWhisk Action Module stopped")


if __name__ == "__main__":
    # Run with Hypercorn in production
    # hypercorn app:app --bind 0.0.0.0:8082 --workers 4
    app.run(host="0.0.0.0", port=Config.REST_PORT, debug=False)
