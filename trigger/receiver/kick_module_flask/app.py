"""
KICK collector - Quart Application
Handles KICK webhooks and chat integration via Pusher
"""
import asyncio
import hashlib
import hmac
import os
import sys

from quart import Blueprint, Quart, request

# Setup path for shared libraries
sys.path.insert(0,
                os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             'libs'))

from flask_core import (async_endpoint, create_health_blueprint,  # noqa: E402
                        init_database, setup_aaa_logging,
                        success_response, error_response)
from config import Config  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
webhook_bp = Blueprint('webhook', __name__, url_prefix='/webhook')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None


@app.before_serving
async def startup():
    global dal
    logger.system("Starting kick_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    logger.system("kick_module started", result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION
    })


def verify_kick_signature(payload: bytes, signature: str) -> bool:
    """Verify KICK webhook signature using HMAC-SHA256."""
    if not Config.KICK_WEBHOOK_SECRET:
        logger.warning("KICK_WEBHOOK_SECRET not configured, skipping signature verification")
        return True

    expected_signature = hmac.new(
        Config.KICK_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


@webhook_bp.route('/kick', methods=['POST'])
@async_endpoint
async def kick_webhook():
    """Handle incoming KICK webhooks."""
    signature = request.headers.get('X-Kick-Signature', '')
    payload = await request.get_data()

    if not verify_kick_signature(payload, signature):
        logger.auth(action="webhook_verify", user="anonymous", result="FAILURE",
                    detail="Invalid webhook signature")
        return error_response("Invalid signature", 401)

    try:
        event = await request.get_json()
        event_type = event.get('type', 'unknown')

        logger.audit(
            action="webhook_receive",
            user="system",
            community="",
            result="RECEIVED",
            detail=f"KICK webhook received: {event_type}",
            source_event_type=event_type
        )

        # Process event based on type
        await process_kick_event(event)

        return success_response({"received": True})

    except Exception as e:
        logger.error(f"Webhook processing error: {e}", action="webhook_process", result="FAILURE")
        return error_response("Processing failed", 500)


async def process_kick_event(event: dict):
    """Process KICK events and forward to router."""
    event_type = event.get('type', 'unknown')

    # Map KICK events to WaddleBot event types
    event_mapping = {
        'ChatMessage': 'chat',
        'Subscription': 'subscription',
        'GiftedSubscription': 'gift_subscription',
        'ChannelFollow': 'follow',
        'StreamStart': 'stream_start',
        'StreamEnd': 'stream_end',
        'Raid': 'raid',
        'Host': 'host',
        'Ban': 'moderation',
        'Timeout': 'moderation',
    }

    waddlebot_type = event_mapping.get(event_type, 'unknown')

    # Build router payload
    payload = {
        'platform': 'kick',
        'server_id': str(event.get('channel_id', '')),
        'channel_id': str(event.get('chatroom_id', '')),
        'user_id': str(event.get('sender', {}).get('id', '')),
        'username': event.get('sender', {}).get('username', ''),
        'message': event.get('content', ''),
        'event_type': waddlebot_type,
        'raw_event': event,
    }

    # Forward to router
    await submit_to_router(payload)


async def submit_to_router(payload: dict):
    """Submit event to router module."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{Config.ROUTER_API_URL}/events",
                json=payload,
                headers={'Content-Type': 'application/json'}
            ) as response:
                if response.status == 200:
                    logger.audit(
                        action="router_submit",
                        user="system",
                        community="",
                        result="SUCCESS",
                        detail="Event submitted to router"
                    )
                else:
                    logger.error(
                        f"Router submission failed: {response.status}",
                        action="router_submit",
                        result="FAILURE"
                    )
    except Exception as e:
        logger.error(f"Router submission error: {e}", action="router_submit", result="FAILURE")


# Register blueprints
app.register_blueprint(api_bp)
app.register_blueprint(webhook_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
