"""
Core Community Service - Combined Quart Application

Merges 5 modules into a single service:
1. community_module (port 8020) - community management
2. workflow_core_module - workflow automation with gRPC
3. browser_source_core_module - OBS browser source integration with websockets
4. video_proxy_module - video stream proxying
Module RTC was not found in the repository.
"""
import asyncio
import logging
import logging.handlers
import os
import sys
import threading
import json
from concurrent import futures
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

_base_dir = os.path.dirname(__file__)


_COLLIDING_MODULES = frozenset({
    'services', 'controllers', 'config', 'validation_models', 'models',
})


def _setup_module_imports(module_name: str) -> None:
    """Flush cached packages that collide across modules and prioritize module dir."""
    for key in list(sys.modules):
        top = key.split('.', 1)[0]
        if top in _COLLIDING_MODULES:
            del sys.modules[key]
    sys.path.insert(0, os.path.join(_base_dir, module_name))


# Add first module to path for initial flask_core import resolution
_setup_module_imports('community_module')

from quart import Quart, Blueprint, request, websocket, jsonify
import jwt
from penguin_dal import DAL

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs'))

from flask_core import (
    setup_aaa_logging, init_database, async_endpoint, success_response,
    create_health_blueprint
)
from config import Config

# Global references
app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# Create unified API blueprint for all modules
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')

# Initialize logging
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# ============================================================================
# SHARED SERVICE STATE
# ============================================================================
dal = None

# Community module globals
# (minimal state - community_module is lightweight)

# Workflow core module globals
license_service = None
permission_service = None
validation_service = None
workflow_service = None
workflow_engine = None
grpc_server_workflow = None
grpc_thread_workflow = None

# Browser source module globals
overlay_service = None
caption_connections = {}  # community_id -> set of websocket connections
grpc_server_browser = None
grpc_thread_browser = None

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================


@app.before_serving
async def startup():
    """Initialize all 4 modules on startup"""
    global dal, license_service, permission_service, validation_service
    global workflow_service, workflow_engine, grpc_server_workflow, grpc_thread_workflow
    global overlay_service, grpc_server_browser, grpc_thread_browser

    logger.system(
        "Starting core-community service (unified)",
        action="startup",
        extra={
            "port": Config.MODULE_PORT,
            "modules": ["community", "workflow_core", "browser_source", "video_proxy"]
        }
    )

    try:
        # Initialize shared database
        dal = init_database(Config.DATABASE_URL)
        app.config['dal'] = dal

        # ====================================================================
        # COMMUNITY MODULE INIT
        # ====================================================================
        logger.system("Initializing community_module", action="module_init")

        # ====================================================================
        # WORKFLOW CORE MODULE INIT
        # ====================================================================
        logger.system("Initializing workflow_core_module", action="module_init")

        _setup_module_imports('workflow_core_module')

        if hasattr(Config, 'LICENSE_SERVER_URL'):
            from workflow_core_module.services.license_service import LicenseService
            license_service = LicenseService(
                license_server_url=Config.LICENSE_SERVER_URL,
                redis_url=Config.REDIS_URL if hasattr(Config, 'REDIS_URL') else None,
                release_mode=getattr(Config, 'RELEASE_MODE', False),
                logger_instance=logger
            )
            try:
                await license_service.connect()
            except Exception as e:
                logger.warning(f"Failed to connect license service: {e}")
                license_service = None

        try:
            from workflow_core_module.services.permission_service import PermissionService
            permission_service = PermissionService(dal=dal, logger=logger)
            app.config['permission_service'] = permission_service
        except ImportError:
            logger.warning("PermissionService not available")

        try:
            from workflow_core_module.services.validation_service import WorkflowValidationService
            validation_service = WorkflowValidationService()
        except ImportError:
            logger.warning("WorkflowValidationService not available")

        try:
            from workflow_core_module.services.workflow_service import WorkflowService
            workflow_service = WorkflowService(
                dal=dal,
                license_service=license_service,
                permission_service=permission_service,
                validation_service=validation_service,
                logger_instance=logger
            )
            app.config['workflow_service'] = workflow_service
        except ImportError:
            logger.warning("WorkflowService not available")

        try:
            from workflow_core_module.services.workflow_engine import WorkflowEngine
            workflow_engine = WorkflowEngine(
                dal=dal,
                router_url=getattr(Config, 'ROUTER_URL', 'http://localhost:8080'),
                max_loop_iterations=getattr(Config, 'MAX_LOOP_ITERATIONS', 100),
                max_total_operations=getattr(Config, 'MAX_TOTAL_OPERATIONS', 1000),
                max_loop_depth=getattr(Config, 'MAX_LOOP_DEPTH', 10),
                default_timeout=getattr(Config, 'WORKFLOW_TIMEOUT', 300),
                max_parallel_nodes=getattr(Config, 'MAX_PARALLEL_NODES', 50)
            )
            app.config['workflow_engine'] = workflow_engine
        except ImportError:
            logger.warning("WorkflowEngine not available")

        # Register workflow API controllers if available
        try:
            from workflow_core_module.controllers.workflow_api import register_workflow_api
            if workflow_service:
                register_workflow_api(app, workflow_service)
        except ImportError:
            logger.warning("Workflow API controllers not available")

        try:
            from workflow_core_module.controllers.execution_api import register_execution_api
            if workflow_engine:
                register_execution_api(app, workflow_engine)
        except ImportError:
            logger.warning("Execution API controllers not available")

        # ====================================================================
        # BROWSER SOURCE MODULE INIT
        # ====================================================================
        logger.system("Initializing browser_source_core_module", action="module_init")

        _setup_module_imports('browser_source_core_module')

        try:
            from browser_source_core_module.services.overlay_service import OverlayService
            overlay_service = OverlayService(dal)
            app.config['overlay_service'] = overlay_service
        except ImportError:
            logger.warning("OverlayService not available")

        # ====================================================================
        # VIDEO PROXY MODULE INIT
        # ====================================================================
        logger.system("Initializing video_proxy_module", action="module_init")

        _setup_module_imports('video_proxy_module')
        # Video proxy module uses same dal, just needs tables initialized
        # Tables are defined in the startup phase

        logger.system("core-community service started successfully", result="SUCCESS")

    except Exception as e:
        logger.error(f"Failed to start core-community service: {str(e)}", result="FAILURE")
        raise


@app.after_serving
async def shutdown():
    """Cleanup on shutdown"""
    global license_service, workflow_engine, grpc_server_workflow, grpc_thread_workflow

    logger.system("Shutting down core-community service", action="shutdown")
    try:
        if license_service:
            try:
                await license_service.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting license service: {str(e)}")

        if workflow_engine:
            try:
                workflow_engine.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down workflow engine: {str(e)}")

        if dal:
            try:
                dal.close()
            except Exception as e:
                logger.warning(f"Error closing database: {str(e)}")

        logger.system("core-community service shutdown complete", result="SUCCESS")
    except Exception as e:
        logger.error(f"Error during shutdown: {str(e)}", result="FAILURE")


# ============================================================================
# COMMUNITY MODULE ENDPOINTS - /api/v1/community/*
# ============================================================================

@api_bp.route('/community/status', methods=['GET'])
@async_endpoint
async def community_status():
    return success_response({"status": "operational", "module": "community_module"})


# ============================================================================
# WORKFLOW CORE MODULE ENDPOINTS - /api/v1/workflow/*
# ============================================================================

@api_bp.route('/workflow/status', methods=['GET'])
@async_endpoint
async def workflow_status():
    return success_response({
        "status": "operational",
        "module": "workflow_core_module",
        "version": Config.MODULE_VERSION,
        "features": {
            "workflows_enabled": getattr(Config, 'FEATURE_WORKFLOWS_ENABLED', True),
            "release_mode": getattr(Config, 'RELEASE_MODE', False)
        }
    })


@api_bp.route('/workflow/health', methods=['GET'])
@async_endpoint
async def workflow_health():
    return success_response({
        "healthy": True,
        "module": "workflow_core_module"
    })


# ============================================================================
# BROWSER SOURCE MODULE ENDPOINTS - /api/v1/browser-source/*
# ============================================================================

@api_bp.route('/browser-source/status', methods=['GET'])
@async_endpoint
async def browser_source_status():
    return success_response({
        "status": "operational",
        "module": "browser_source_core_module"
    })


@api_bp.route('/browser-source/internal/captions', methods=['POST'])
@async_endpoint
async def receive_caption():
    """Receive caption from router (internal service-to-service)"""
    # Validate service key
    service_key = request.headers.get('X-Service-Key')
    if hasattr(Config, 'SERVICE_API_KEY') and Config.SERVICE_API_KEY:
        if service_key != Config.SERVICE_API_KEY:
            return {"error": "Unauthorized"}, 401

    data = await request.get_json()
    community_id = data.get('community_id')

    # Broadcast to WebSocket connections for this community
    if community_id in caption_connections:
        caption_payload = {
            'type': 'caption',
            'username': data.get('username'),
            'original': data.get('original_message'),
            'translated': data.get('translated_message'),
            'detected_lang': data.get('detected_language'),
            'target_lang': data.get('target_language'),
            'confidence': data.get('confidence'),
            'timestamp': datetime.utcnow().isoformat()
        }

        for ws in list(caption_connections[community_id]):
            try:
                await ws.send(json.dumps(caption_payload))
            except:
                caption_connections[community_id].discard(ws)

    # Store in database for recent history
    try:
        dal.executesql(
            """INSERT INTO caption_events
               (community_id, platform, username,
                original_message, translated_message, detected_language,
                target_language, confidence_score)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [community_id, data.get('platform', ''), data.get('username'),
             data.get('original_message'), data.get('translated_message'),
             data.get('detected_language'), data.get('target_language'),
             data.get('confidence')]
        )
    except Exception as e:
        logger.error(f"Failed to store caption event: {e}")

    return success_response({"received": True})


# ============================================================================
# VIDEO PROXY MODULE ENDPOINTS - /api/v1/stream/*
# ============================================================================

def generate_stream_key() -> str:
    """Generate a secure random stream key."""
    import secrets
    return secrets.token_urlsafe(32)


def format_stream_config(row) -> Dict[str, Any]:
    """Format stream config row for JSON response."""
    return {
        "id": row.id,
        "community_id": row.community_id,
        "stream_key": row.stream_key,
        "ingest_url": row.ingest_url,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None
    }


def format_destination(row) -> Dict[str, Any]:
    """Format destination row for JSON response."""
    return {
        "id": row.id,
        "config_id": row.config_id,
        "platform": row.platform,
        "rtmp_url": row.rtmp_url,
        "stream_key": row.stream_key[:8] + "..." if row.stream_key else "",
        "is_active": row.is_active,
        "force_cut": row.force_cut,
        "max_resolution": row.max_resolution,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None
    }


@api_bp.route('/stream/config', methods=['POST'])
@async_endpoint
async def create_stream_config():
    """Create stream configuration for a community."""
    try:
        data = await request.get_json()
        community_id = data.get("community_id")

        if not community_id:
            return jsonify({"error": "community_id is required"}), 400

        existing = dal(dal.stream_configs.community_id == community_id).select().first()
        if existing:
            return jsonify({
                "error": "Stream config already exists for this community",
                "config": format_stream_config(existing)
            }), 409

        stream_key = generate_stream_key()
        ingest_url = f"rtmp://{getattr(Config, 'MODULE_HOST', 'localhost')}:{Config.MODULE_PORT}/live/{stream_key}"

        config_id = dal.stream_configs.insert(
            community_id=community_id,
            stream_key=stream_key,
            ingest_url=ingest_url,
            is_active=True
        )
        dal.commit()

        dal.stream_status.insert(
            config_id=config_id,
            is_streaming=False,
            viewer_count=0,
            bitrate_kbps=0
        )
        dal.commit()

        config_row = dal.stream_configs[config_id]
        logger.info(f"Created stream config for community {community_id}")

        return jsonify({
            "success": True,
            "config": format_stream_config(config_row)
        }), 201

    except Exception as e:
        logger.error(f"Failed to create stream config: {e}", exc_info=True)
        dal.rollback()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/config/<community_id>', methods=['GET'])
@async_endpoint
async def get_stream_config(community_id: str):
    """Get stream configuration for a community."""
    try:
        config_row = dal(dal.stream_configs.community_id == community_id).select().first()

        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        return jsonify({
            "success": True,
            "config": format_stream_config(config_row)
        })

    except Exception as e:
        logger.error(f"Failed to get stream config: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/key/regenerate/<community_id>', methods=['POST'])
@async_endpoint
async def regenerate_stream_key(community_id: str):
    """Regenerate stream key for a community."""
    try:
        config_row = dal(dal.stream_configs.community_id == community_id).select().first()

        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        new_stream_key = generate_stream_key()
        new_ingest_url = f"rtmp://{getattr(Config, 'MODULE_HOST', 'localhost')}:{Config.MODULE_PORT}/live/{new_stream_key}"

        dal(dal.stream_configs.id == config_row.id).update(
            stream_key=new_stream_key,
            ingest_url=new_ingest_url,
            updated_at=datetime.utcnow()
        )
        dal.commit()

        updated_row = dal.stream_configs[config_row.id]
        logger.info(f"Regenerated stream key for community {community_id}")

        return jsonify({
            "success": True,
            "config": format_stream_config(updated_row)
        })

    except Exception as e:
        logger.error(f"Failed to regenerate stream key: {e}", exc_info=True)
        dal.rollback()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/destinations/<int:config_id>', methods=['GET'])
@async_endpoint
async def get_destinations(config_id: int):
    """List all destinations for a stream config."""
    try:
        config_row = dal.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        destinations = dal(dal.stream_destinations.config_id == config_id).select()

        return jsonify({
            "success": True,
            "count": len(destinations),
            "destinations": [format_destination(dest) for dest in destinations]
        })

    except Exception as e:
        logger.error(f"Failed to get destinations: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/destinations', methods=['POST'])
@async_endpoint
async def add_destination():
    """Add a destination for a stream config."""
    try:
        data = await request.get_json()

        config_id = data.get("config_id")
        platform = data.get("platform")
        rtmp_url = data.get("rtmp_url")
        stream_key = data.get("stream_key")
        max_resolution = data.get("max_resolution", "1080p")

        if not all([config_id, platform, rtmp_url, stream_key]):
            return jsonify({
                "error": "config_id, platform, rtmp_url, and stream_key are required"
            }), 400

        config_row = dal.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        dest_count = dal(dal.stream_destinations.config_id == config_id).count()
        free_max_destinations = getattr(Config, 'FREE_MAX_DESTINATIONS', 3)
        if dest_count >= free_max_destinations:
            return jsonify({
                "error": f"Maximum destinations ({free_max_destinations}) reached"
            }), 403

        dest_id = dal.stream_destinations.insert(
            config_id=config_id,
            platform=platform,
            rtmp_url=rtmp_url,
            stream_key=stream_key,
            is_active=True,
            force_cut=False,
            max_resolution=max_resolution
        )

        dal.commit()

        dest_row = dal.stream_destinations[dest_id]

        logger.info(f"Added destination {platform} for config {config_id}")

        return jsonify({
            "success": True,
            "destination": format_destination(dest_row)
        }), 201

    except Exception as e:
        logger.error(f"Failed to add destination: {e}", exc_info=True)
        dal.rollback()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/destinations/<int:destination_id>', methods=['DELETE'])
@async_endpoint
async def remove_destination(destination_id: int):
    """Remove a destination."""
    try:
        dest_row = dal.stream_destinations[destination_id]
        if not dest_row:
            return jsonify({"error": "Destination not found"}), 404

        dal(dal.stream_destinations.id == destination_id).delete()
        dal.commit()

        logger.info(f"Removed destination {destination_id}")

        return jsonify({
            "success": True,
            "message": "Destination removed successfully"
        })

    except Exception as e:
        logger.error(f"Failed to remove destination: {e}", exc_info=True)
        dal.rollback()
        return jsonify({"error": str(e)}), 500


@api_bp.route('/stream/status/<int:config_id>', methods=['GET'])
@async_endpoint
async def get_stream_status(config_id: int):
    """Get stream status for a config."""
    try:
        config_row = dal.stream_configs[config_id]
        if not config_row:
            return jsonify({"error": "Stream config not found"}), 404

        status_row = dal(dal.stream_status.config_id == config_id).select().first()

        if not status_row:
            return jsonify({"error": "Stream status not found"}), 404

        return jsonify({
            "success": True,
            "status": {
                "config_id": status_row.config_id,
                "is_streaming": status_row.is_streaming,
                "viewer_count": status_row.viewer_count,
                "bitrate_kbps": status_row.bitrate_kbps,
                "start_time": status_row.start_time.isoformat() if status_row.start_time else None,
                "last_update": status_row.last_update.isoformat() if status_row.last_update else None
            }
        })

    except Exception as e:
        logger.error(f"Failed to get stream status: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ============================================================================
# SHARED ENDPOINTS
# ============================================================================

@api_bp.route('/status', methods=['GET'])
@async_endpoint
async def unified_status():
    """Get unified service status"""
    return success_response({
        "status": "operational",
        "service": "core-community",
        "version": Config.MODULE_VERSION,
        "modules": {
            "community": "active",
            "workflow_core": "active",
            "browser_source": "active",
            "video_proxy": "active"
        },
        "timestamp": datetime.utcnow().isoformat()
    })


# Register the unified API blueprint
app.register_blueprint(api_bp)


# ============================================================================
# BROWSER SOURCE OVERLAY ROUTES (separate blueprint - not under /api/v1)
# ============================================================================

overlay_bp = Blueprint('overlay', __name__, url_prefix='/overlay')


@overlay_bp.route('/<overlay_key>')
@async_endpoint
async def serve_overlay(overlay_key: str):
    """Serve unified overlay for a community."""
    if not overlay_service:
        return '<html><body><h1>Overlay service not available</h1></body></html>', 503

    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_agent = request.headers.get('User-Agent', '')

    result = await overlay_service.validate_overlay_key(overlay_key)

    if not result:
        await overlay_service.log_access(
            community_id=0,
            overlay_key=overlay_key[:64] if overlay_key else '',
            ip_address=ip_address,
            user_agent=user_agent,
            was_valid=False
        )
        return '<html><body><h1>Invalid overlay key</h1></body></html>', 404

    await overlay_service.log_access(
        community_id=result['community_id'],
        overlay_key=overlay_key,
        ip_address=ip_address,
        user_agent=user_agent,
        source_types=result.get('enabled_sources'),
        was_valid=True
    )

    html = await overlay_service.get_overlay_html(
        community_id=result['community_id'],
        theme_config=result.get('theme_config'),
        enabled_sources=result.get('enabled_sources')
    )

    return html, 200, {
        'Content-Type': 'text/html',
        'X-Frame-Options': 'ALLOWALL',
        'Cache-Control': 'no-cache'
    }


@overlay_bp.route('/captions/<overlay_key>')
@async_endpoint
async def serve_caption_overlay(overlay_key: str):
    """Serve caption overlay HTML for OBS"""
    if not overlay_service:
        return '<html><body><h1>Overlay service not available</h1></body></html>', 503

    result = await overlay_service.validate_overlay_key(overlay_key)

    if not result:
        return '<html><body><h1>Invalid overlay key</h1></body></html>', 404

    template_path = os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'core',
        'browser_source_core_module',
        'templates',
        'caption-overlay.html'
    )

    try:
        with open(template_path, 'r') as f:
            html = f.read()
    except FileNotFoundError:
        return '<html><body><h1>Template not found</h1></body></html>', 500

    return html, 200, {
        'Content-Type': 'text/html',
        'X-Frame-Options': 'ALLOWALL',
        'Cache-Control': 'no-cache'
    }


app.register_blueprint(overlay_bp)


# ============================================================================
# WEBSOCKET ENDPOINTS
# ============================================================================

@app.websocket('/ws/captions/<int:community_id>')
async def caption_websocket(community_id: int):
    """WebSocket endpoint for live captions"""
    overlay_key = request.args.get('key')
    result = await overlay_service.validate_overlay_key(overlay_key) if overlay_service else None

    if not result or result.get('community_id') != community_id:
        await websocket.close(1008, "Invalid overlay key")
        return

    if community_id not in caption_connections:
        caption_connections[community_id] = set()
    caption_connections[community_id].add(websocket._get_current_object())

    try:
        recent = dal.executesql(
            """SELECT username, original_message, translated_message,
                      detected_language, target_language, confidence_score,
                      created_at
               FROM caption_events
               WHERE community_id = %s
               AND created_at > NOW() - INTERVAL '5 minutes'
               ORDER BY created_at DESC
               LIMIT 10""",
            [community_id]
        )

        for row in reversed(recent if recent else []):
            await websocket.send(json.dumps({
                'type': 'caption',
                'username': row[0],
                'original': row[1],
                'translated': row[2],
                'detected_lang': row[3],
                'target_lang': row[4],
                'confidence': float(row[5]) if row[5] else 0.0,
                'timestamp': row[6].isoformat() if row[6] else None
            }))

        while True:
            message = await websocket.receive()
            if message == 'ping':
                await websocket.send('pong')
    finally:
        if community_id in caption_connections:
            caption_connections[community_id].discard(websocket._get_current_object())


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
