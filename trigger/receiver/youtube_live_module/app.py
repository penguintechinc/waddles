"""
YouTube Live Collector - Quart Application
===========================================

Trigger receiver module for YouTube Live streaming events.
Polls live chat and receives PubSubHubbub webhooks for stream events.
"""
import asyncio
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
from services.youtube_client import YouTubeClient  # noqa: E402
from services.chat_poller import ChatPoller  # noqa: E402
from services.webhook_handler import WebhookHandler  # noqa: E402

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Services (initialized on startup)
dal = None
youtube_client: YouTubeClient = None
chat_poller: ChatPoller = None
webhook_handler: WebhookHandler = None


@app.before_serving
async def startup():
    """Initialize services on application startup."""
    global dal, youtube_client, chat_poller, webhook_handler

    logger.system("Starting youtube_live_module", action="startup")

    # Initialize database
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    # Initialize YouTube client
    youtube_client = YouTubeClient(Config.YOUTUBE_API_KEY)

    # Initialize chat poller
    chat_poller = ChatPoller(youtube_client)
    await chat_poller.start()

    # Initialize webhook handler
    webhook_handler = WebhookHandler()
    await webhook_handler.start()

    logger.system("youtube_live_module started", result="SUCCESS")


@app.after_serving
async def shutdown():
    """Cleanup on application shutdown."""
    logger.system("Shutting down youtube_live_module", action="shutdown")

    if chat_poller:
        await chat_poller.stop()

    if webhook_handler:
        await webhook_handler.stop()

    if youtube_client:
        await youtube_client.close()

    logger.system("youtube_live_module stopped", result="SUCCESS")


# =============================================================================
# Status Endpoints
# =============================================================================

@api_bp.route('/status')
@async_endpoint
async def status():
    """Get module status including poller state."""
    poller_status = chat_poller.get_status() if chat_poller else {}

    return success_response({
        "status": "operational",
        "module": Config.MODULE_NAME,
        "version": Config.MODULE_VERSION,
        "poller": poller_status
    })


# =============================================================================
# Channel Management Endpoints
# =============================================================================

@api_bp.route('/channels/register', methods=['POST'])
@async_endpoint
async def register_channel():
    """
    Register a YouTube channel for monitoring.

    Request body:
        {
            "channel_id": "UCxxxxxx",
            "subscribe_webhook": true  // optional, default true
        }
    """
    data = await request.get_json()

    if not data or 'channel_id' not in data:
        return error_response("channel_id is required", 400)

    channel_id = data['channel_id']
    subscribe_webhook = data.get('subscribe_webhook', True)

    # Verify channel exists
    channel_info = await youtube_client.get_channel_info(channel_id)
    if not channel_info:
        return error_response(f"Channel not found: {channel_id}", 404)

    # Add to chat poller
    chat_poller.add_channel(channel_id)

    # Subscribe to PubSubHubbub
    webhook_subscribed = False
    if subscribe_webhook:
        webhook_subscribed = await youtube_client.subscribe_to_channel(
            channel_id
        )

    logger.audit(
        action="register_channel",
        user="system",
        community="",
        result="SUCCESS",
        detail=f"Registered channel: {channel_id}",
        channel_id=channel_id
    )

    return success_response({
        "channel_id": channel_id,
        "channel_name": channel_info.title,
        "thumbnail_url": channel_info.thumbnail_url,
        "webhook_subscribed": webhook_subscribed,
        "chat_polling": True
    })


@api_bp.route('/channels/<channel_id>', methods=['DELETE'])
@async_endpoint
async def unregister_channel(channel_id: str):
    """
    Unregister a YouTube channel from monitoring.

    Args:
        channel_id: YouTube channel ID
    """
    # Remove from chat poller
    chat_poller.remove_channel(channel_id)

    # Unsubscribe from PubSubHubbub
    await youtube_client.unsubscribe_from_channel(channel_id)

    logger.audit(
        action="unregister_channel",
        user="system",
        community="",
        result="SUCCESS",
        detail=f"Unregistered channel: {channel_id}",
        channel_id=channel_id
    )

    return success_response({
        "channel_id": channel_id,
        "status": "unregistered"
    })


@api_bp.route('/channels', methods=['GET'])
@async_endpoint
async def list_channels():
    """List all registered channels."""
    poller_status = chat_poller.get_status() if chat_poller else {}

    return success_response({
        "channels": list(chat_poller.state.monitored_channels),
        "active_chats": poller_status.get('chats', [])
    })


# =============================================================================
# Webhook Endpoints (PubSubHubbub)
# =============================================================================

@api_bp.route('/webhook', methods=['GET'])
@async_endpoint
async def webhook_verify():
    """
    Handle PubSubHubbub subscription verification.

    Query params:
        hub.mode: 'subscribe' or 'unsubscribe'
        hub.topic: The topic URL
        hub.challenge: Challenge to echo back
        hub.lease_seconds: Lease duration (optional)
    """
    hub_mode = request.args.get('hub.mode')
    hub_topic = request.args.get('hub.topic')
    hub_challenge = request.args.get('hub.challenge')
    hub_lease = request.args.get('hub.lease_seconds')

    if not all([hub_mode, hub_topic, hub_challenge]):
        return error_response("Missing required parameters", 400)

    result = webhook_handler.verify_subscription(
        hub_mode, hub_topic, hub_challenge, hub_lease
    )

    if result:
        # Return the challenge as plain text
        return result, 200, {'Content-Type': 'text/plain'}
    else:
        return error_response("Verification failed", 404)


@api_bp.route('/webhook', methods=['POST'])
@async_endpoint
async def webhook_callback():
    """
    Handle PubSubHubbub notification callback.

    Body: Atom XML feed with video/stream updates
    """
    body = await request.get_data()

    if not body:
        return error_response("Empty request body", 400)

    result = await webhook_handler.process_notification(body)

    if result.get('success'):
        return success_response(result)
    else:
        return error_response(result.get('error', 'Unknown error'), 400)


# =============================================================================
# Broadcast Discovery Endpoints
# =============================================================================

@api_bp.route('/broadcasts/<channel_id>', methods=['GET'])
@async_endpoint
async def get_broadcasts(channel_id: str):
    """
    Get active live broadcasts for a channel.

    Args:
        channel_id: YouTube channel ID
    """
    broadcasts = await youtube_client.get_live_broadcasts(channel_id)

    return success_response({
        "channel_id": channel_id,
        "broadcasts": [
            {
                "broadcast_id": b.broadcast_id,
                "title": b.title,
                "live_chat_id": b.live_chat_id,
                "status": b.status,
                "start_time": b.start_time
            }
            for b in broadcasts
        ]
    })


# =============================================================================
# Register Blueprint
# =============================================================================

app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
