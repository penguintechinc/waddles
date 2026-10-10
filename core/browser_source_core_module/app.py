"""Browser source for OBS - Quart Application.

MIGRATION NOTE: the caption overlay in this module (``/overlay/captions/<key>``,
``/ws/captions/<community_id>``, ``/api/v1/internal/captions`` and the gRPC
``SendCaption``) has a Rust port in ``core/svc_presentation`` (flag
``waddles.core.overlay-captions``, migration 102). This copy stays live until
that flag is switched on at the parity cutover, after which the caption path
here is to be deleted -- see ``core/svc_presentation/README.md``.
"""
import asyncio
import json
import logging
import os
import sys
from concurrent import futures
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'libs'))  # noqa: E402

import grpc  # noqa: E402
from quart import Blueprint, Quart, request, websocket  # noqa: E402

from config import Config  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint, create_health_blueprint, init_database,
    install_rate_limiting, setup_aaa_logging, success_response)
from flask_core.auth import verify_service_key  # noqa: E402
from flask_core.grpc_tls import bind_secure_port, default_server_options  # noqa: E402
from flask_core.security_headers import OVERLAY_CSP, install_security_headers  # noqa: E402
from proto import browser_source_pb2_grpc  # noqa: E402
from services.grpc_handler import BrowserSourceServiceServicer  # noqa: E402
from services.overlay_service import OverlayService  # noqa: E402

# Global WebSocket connections registry for captions
caption_connections = {}  # community_id -> set of websocket connections

app = Quart(__name__)
# security.md A05 hardening -- this module serves real OBS browser-source
# overlay HTML (templates/*.html: inline <script>/<style>, YouTube iframe
# API, Twitch clip embeds, placeholder images) alongside its JSON API, so
# it gets the relaxed OVERLAY_CSP rather than the JSON-only DEFAULT_CSP
# every other core/* module installs.
#
# `frame-ancestors` is stripped from the shared OVERLAY_CSP here (unlike
# svc_presentation, which keeps `frame-ancestors 'none'` unmodified):
# `overlay_bp`'s two HTML routes below each already return an explicit
# `X-Frame-Options: ALLOWALL` ("Required for OBS browser source" --
# intentionally embeddable from any origin, not just direct OBS
# navigation). CSP `frame-ancestors`, when present, overrides
# `X-Frame-Options` in every browser that supports it -- shipping the
# unmodified `'none'` value would silently re-block the exact framing
# those routes opt into, regardless of what `X-Frame-Options` says.
# `headers.setdefault()` (see `security_headers.py`) already protects
# `X-Frame-Options` itself from being clobbered; this is the CSP-specific
# companion so the two policies stay in agreement instead of fighting.
_OVERLAY_CSP_FRAMEABLE = OVERLAY_CSP.replace("frame-ancestors 'none'; ", "")
install_security_headers(app, csp=_OVERLAY_CSP_FRAMEABLE)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): every route below (status, internal caption ingest,
# overlay serving) had zero rate limiting -- shared global before_request
# hook, see flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None
overlay_service = None


@app.before_serving
async def startup():
    """Initialize database and start module."""
    global dal, overlay_service
    logger.system("Starting browser_source_core_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    overlay_service = OverlayService(dal)
    logger.system("browser_source_core_module started", result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    """Return module status."""
    response_data = {
        "status": "operational",
        "module": Config.MODULE_NAME
    }
    return success_response(response_data)


@api_bp.route('/internal/captions', methods=['POST'])
@async_endpoint
async def receive_caption():
    """Receive caption from router (internal service-to-service)"""
    from config import Config

    # SECURITY (C6): the previous `if hasattr(...) and Config.SERVICE_API_KEY:`
    # check SKIPPED validation entirely (fail-OPEN) whenever SERVICE_API_KEY
    # was unset/misconfigured -- a misconfiguration silently left this
    # endpoint unauthenticated. `verify_service_key` fails CLOSED (rejects
    # when unconfigured) and uses a constant-time comparison.
    service_key = request.headers.get('X-Service-Key', '')
    if not verify_service_key(service_key, Config.SERVICE_API_KEY):
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

        # Send to all connected websockets
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

app.register_blueprint(api_bp)

overlay_bp = Blueprint('overlay', __name__, url_prefix='/overlay')


@overlay_bp.route('/<overlay_key>')
@async_endpoint
async def serve_overlay(overlay_key: str):
    """
    Serve unified overlay for a community.
    URL format: /overlay/<64-char-hex-key>
    """
    global overlay_service

    # Get client info for logging
    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_agent = request.headers.get('User-Agent', '')

    # Validate overlay key
    result = await overlay_service.validate_overlay_key(overlay_key)

    if not result:
        # Log invalid access attempt
        await overlay_service.log_access(
            community_id=0,
            overlay_key=overlay_key[:64] if overlay_key else '',
            ip_address=ip_address,
            user_agent=user_agent,
            was_valid=False
        )
        return '<html><body><h1>Invalid overlay key</h1></body></html>', 404

    # Log valid access
    await overlay_service.log_access(
        community_id=result['community_id'],
        overlay_key=overlay_key,
        ip_address=ip_address,
        user_agent=user_agent,
        source_types=result['enabled_sources'],
        was_valid=True
    )

    # Generate and return overlay HTML
    html = await overlay_service.get_overlay_html(
        community_id=result['community_id'],
        theme_config=result['theme_config'],
        enabled_sources=result['enabled_sources']
    )

    return html, 200, {
        'Content-Type': 'text/html',
        'X-Frame-Options': 'ALLOWALL',  # Required for OBS browser source
        'Cache-Control': 'no-cache'
    }


@overlay_bp.route('/captions/<overlay_key>')
@async_endpoint
async def serve_caption_overlay(overlay_key: str):
    """Serve caption overlay HTML for OBS"""
    result = await overlay_service.validate_overlay_key(overlay_key)

    if not result:
        return '<html><body><h1>Invalid overlay key</h1></body></html>', 404

    # Read and serve template
    template_path = os.path.join(
        os.path.dirname(__file__),
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


@app.websocket('/ws/captions/<int:community_id>')
async def caption_websocket(community_id: int):
    """WebSocket endpoint for live captions"""
    global caption_connections, overlay_service, dal

    # Validate overlay key from query param
    overlay_key = request.args.get('key')
    result = await overlay_service.validate_overlay_key(overlay_key)

    if not result or result.get('community_id') != community_id:
        await websocket.close(1008, "Invalid overlay key")
        return

    # Register connection
    if community_id not in caption_connections:
        caption_connections[community_id] = set()
    caption_connections[community_id].add(websocket._get_current_object())

    try:
        # Send recent captions on connect (last 5 minutes)
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

        # Keep connection alive
        while True:
            message = await websocket.receive()
            if message == 'ping':
                await websocket.send('pong')
    finally:
        # Cleanup on disconnect
        if community_id in caption_connections:
            caption_connections[community_id].discard(websocket._get_current_object())


async def serve_grpc():
    """Start gRPC server"""
    global dal, overlay_service, caption_connections

    logger = logging.getLogger(__name__)
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=default_server_options(),
    )

    # Add gRPC service
    browser_source_pb2_grpc.add_BrowserSourceServiceServicer_to_server(
        BrowserSourceServiceServicer(overlay_service, dal, caption_connections),
        server
    )

    bind_secure_port(server, f"0.0.0.0:{Config.GRPC_PORT}")
    logger.info(f"Starting gRPC server (TLS) on 0.0.0.0:{Config.GRPC_PORT}")
    await server.start()
    await server.wait_for_termination()


async def serve_rest():
    """Start REST API server"""
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    logger = logging.getLogger(__name__)
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    logger.info(f"Starting REST API server on 0.0.0.0:{Config.MODULE_PORT}")
    await hypercorn.asyncio.serve(app, config)


async def main():
    """Main entry point - run both gRPC and REST servers"""
    logger = logging.getLogger(__name__)
    logger.info(f"Starting {Config.MODULE_NAME} v{Config.MODULE_VERSION}")

    try:
        # Run both gRPC and REST servers concurrently
        await asyncio.gather(serve_grpc(), serve_rest())
    except KeyboardInterrupt:
        logger.info("Shutting down servers")


if __name__ == '__main__':
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL),
        format="[%(asctime)s] %(levelname)s %(name)s:%(lineno)d - %(message)s"
    )
    asyncio.run(main())
