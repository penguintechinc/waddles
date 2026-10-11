"""
Twitch EventSub Handler - Handle EventSub webhooks for subs, raids, follows, etc.
"""
import hmac
import hashlib
from typing import Dict, Any, Optional
import httpx
from flask_core import setup_aaa_logging


class EventSubHandler:
    """
    Handles Twitch EventSub webhooks:
    - Subscription verification
    - Event processing (subs, raids, follows, etc.)
    - Forwarding events to router
    """

    EVENTSUB_MESSAGE_TYPE = 'Twitch-Eventsub-Message-Type'
    EVENTSUB_SIGNATURE = 'Twitch-Eventsub-Message-Signature'
    EVENTSUB_TIMESTAMP = 'Twitch-Eventsub-Message-Timestamp'
    EVENTSUB_MESSAGE_ID = 'Twitch-Eventsub-Message-Id'

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        eventsub_secret: str,
        router_url: str,
        callback_url: Optional[str] = None,
        log_level: str = 'INFO'
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.eventsub_secret = eventsub_secret
        self.router_url = router_url
        self.callback_url = callback_url
        self.logger = setup_aaa_logging('eventsub', '2.0.0')
        self._http_session: Optional[httpx.AsyncClient] = None
        self._processed_ids: set = set()  # Track processed message IDs

    async def handle_webhook(
        self,
        headers: Dict[str, str],
        body: bytes,
        body_json: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle incoming EventSub webhook"""
        message_type = headers.get(self.EVENTSUB_MESSAGE_TYPE, '')
        message_id = headers.get(self.EVENTSUB_MESSAGE_ID, '')

        # Prevent duplicate processing
        if message_id in self._processed_ids:
            self.logger.debug(f"Duplicate message ignored: {message_id}")
            return {"status": "duplicate"}

        # Verify signature
        if not self._verify_signature(headers, body):
            self.logger.warning("Invalid EventSub signature")
            return {"error": "Invalid signature"}, 403

        # Handle different message types
        if message_type == 'webhook_callback_verification':
            # Subscription verification
            challenge = body_json.get('challenge', '')
            self.logger.info("EventSub subscription verified")
            return {"challenge": challenge}

        elif message_type == 'notification':
            # Event notification
            self._processed_ids.add(message_id)
            # Keep set from growing indefinitely
            if len(self._processed_ids) > 10000:
                self._processed_ids = set(list(self._processed_ids)[-5000:])

            await self._process_event(body_json)
            return {"status": "ok"}

        elif message_type == 'revocation':
            # Subscription revoked
            subscription = body_json.get('subscription', {})
            self.logger.warning(
                f"EventSub subscription revoked: {subscription.get('type')} - "
                f"{subscription.get('status')}"
            )
            return {"status": "acknowledged"}

        return {"status": "unknown_type"}

    def _verify_signature(self, headers: Dict[str, str], body: bytes) -> bool:
        """Verify HMAC-SHA256 signature"""
        signature = headers.get(self.EVENTSUB_SIGNATURE, '')
        timestamp = headers.get(self.EVENTSUB_TIMESTAMP, '')
        message_id = headers.get(self.EVENTSUB_MESSAGE_ID, '')

        if not all([signature, timestamp, message_id]):
            return False

        # Build message to verify
        message = message_id.encode() + timestamp.encode() + body

        # Calculate expected signature
        expected = 'sha256=' + hmac.new(
            self.eventsub_secret.encode(),
            message,
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(signature, expected)

    async def _process_event(self, body: Dict[str, Any]):
        """Process an EventSub notification"""
        subscription = body.get('subscription', {})
        event = body.get('event', {})
        event_type = subscription.get('type', '')

        self.logger.info(f"Processing EventSub event: {event_type}")

        # Build event data based on type
        event_data = self._build_event_data(event_type, event, subscription)

        if event_data:
            await self._send_to_router(event_data)

    def _build_event_data(
        self,
        event_type: str,
        event: Dict[str, Any],
        subscription: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Build router event data from EventSub event"""
        broadcaster_id = (
            event.get('broadcaster_user_id')
            or subscription.get('condition', {}).get('broadcaster_user_id', '')
        )
        broadcaster_name = event.get('broadcaster_user_login', '')

        base_data = {
            "entity_id": broadcaster_id,
            "platform": "twitch",
            "channel_id": broadcaster_name,
            "server_id": broadcaster_id,
        }

        if event_type == 'channel.subscribe':
            return {
                **base_data,
                "user_id": event.get('user_id', ''),
                "username": event.get('user_login', ''),
                "display_name": event.get('user_name', ''),
                "message": "",
                "message_type": "subscription",
                "metadata": {
                    "tier": event.get('tier', '1000'),
                    "is_gift": event.get('is_gift', False)
                }
            }

        elif event_type == 'channel.subscription.gift':
            return {
                **base_data,
                "user_id": event.get('user_id', ''),
                "username": event.get('user_login', ''),
                "display_name": event.get('user_name', ''),
                "message": "",
                "message_type": "gift_subscription",
                "metadata": {
                    "tier": event.get('tier', '1000'),
                    "total": event.get('total', 1),
                    "cumulative_total": event.get('cumulative_total'),
                    "is_anonymous": event.get('is_anonymous', False)
                }
            }

        elif event_type == 'channel.raid':
            return {
                **base_data,
                "user_id": event.get('from_broadcaster_user_id', ''),
                "username": event.get('from_broadcaster_user_login', ''),
                "display_name": event.get('from_broadcaster_user_name', ''),
                "message": "",
                "message_type": "raid",
                "metadata": {
                    "viewers": event.get('viewers', 0)
                }
            }

        elif event_type == 'channel.follow':
            return {
                **base_data,
                "user_id": event.get('user_id', ''),
                "username": event.get('user_login', ''),
                "display_name": event.get('user_name', ''),
                "message": "",
                "message_type": "follow",
                "metadata": {
                    "followed_at": event.get('followed_at')
                }
            }

        elif event_type == 'channel.cheer':
            return {
                **base_data,
                "user_id": event.get('user_id', ''),
                "username": event.get('user_login', ''),
                "display_name": event.get('user_name', ''),
                "message": event.get('message', ''),
                "message_type": "cheer",
                "metadata": {
                    "bits": event.get('bits', 0),
                    "is_anonymous": event.get('is_anonymous', False)
                }
            }

        elif event_type == 'stream.online':
            return {
                **base_data,
                "user_id": broadcaster_id,
                "username": broadcaster_name,
                "message": "",
                "message_type": "stream_online",
                "metadata": {
                    "stream_type": event.get('type', 'live'),
                    "started_at": event.get('started_at')
                }
            }

        elif event_type == 'stream.offline':
            return {
                **base_data,
                "user_id": broadcaster_id,
                "username": broadcaster_name,
                "message": "",
                "message_type": "stream_offline",
                "metadata": {}
            }

        # Unhandled event type
        self.logger.debug(f"Unhandled EventSub type: {event_type}")
        return None

    async def _send_to_router(self, event_data: Dict[str, Any]):
        """Send event to router"""
        try:
            async with self._get_http_session() as client:
                response = await client.post(
                    f"{self.router_url}/events",
                    json=event_data,
                    timeout=30.0
                )

                self.logger.audit(
                    action="router_forward",
                    user=event_data.get('user_id'),
                    community=str(event_data.get("community_id") or ""),
                    result="SUCCESS" if response.status_code < 400 else "FAILED",
                    detail=f"EventSub event sent: {event_data.get('message_type')}"
                )

        except Exception as e:
            self.logger.error(f"Failed to forward EventSub event: {e}")

    def _get_http_session(self) -> httpx.AsyncClient:
        """Get or create HTTP session"""
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session

    async def subscribe_to_events(
        self,
        broadcaster_id: str,
        event_types: list = None
    ):
        """Subscribe to EventSub events for a broadcaster"""
        if not self.callback_url:
            self.logger.error("Cannot subscribe without callback_url")
            return

        if event_types is None:
            event_types = [
                'channel.follow',
                'channel.subscribe',
                'channel.subscription.gift',
                'channel.cheer',
                'channel.raid',
                'stream.online',
                'stream.offline'
            ]

        # Get app access token
        token = await self._get_app_access_token()
        if not token:
            return

        for event_type in event_types:
            await self._create_subscription(broadcaster_id, event_type, token)

    async def _get_app_access_token(self) -> Optional[str]:
        """Get Twitch app access token"""
        try:
            async with self._get_http_session() as client:
                response = await client.post(
                    'https://id.twitch.tv/oauth2/token',
                    params={
                        'client_id': self.client_id,
                        'client_secret': self.client_secret,
                        'grant_type': 'client_credentials'
                    }
                )
                if response.status_code == 200:
                    return response.json().get('access_token')
        except Exception as e:
            self.logger.error(f"Failed to get app token: {e}")
        return None

    async def _create_subscription(
        self,
        broadcaster_id: str,
        event_type: str,
        token: str
    ):
        """Create an EventSub subscription"""
        try:
            # Build condition based on event type
            condition = {"broadcaster_user_id": broadcaster_id}
            if event_type == 'channel.follow':
                condition["moderator_user_id"] = broadcaster_id

            async with self._get_http_session() as client:
                response = await client.post(
                    'https://api.twitch.tv/helix/eventsub/subscriptions',
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Client-Id': self.client_id,
                        'Content-Type': 'application/json'
                    },
                    json={
                        "type": event_type,
                        "version": "1",
                        "condition": condition,
                        "transport": {
                            "method": "webhook",
                            "callback": self.callback_url,
                            "secret": self.eventsub_secret
                        }
                    }
                )

                if response.status_code in (200, 202):
                    self.logger.info(f"Subscribed to {event_type} for {broadcaster_id}")
                else:
                    self.logger.warning(
                        f"Failed to subscribe to {event_type}: {response.status_code}"
                    )

        except Exception as e:
            self.logger.error(f"Subscription failed: {e}")

    async def stop(self):
        """Clean up resources"""
        if self._http_session and not self._http_session.is_closed:
            await self._http_session.aclose()
