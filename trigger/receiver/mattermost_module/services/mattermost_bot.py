"""
Mattermost Bot Service - WebSocket and webhook integration
Supports slash commands, chat messages, and status change events
"""
import asyncio
import hashlib
import hmac
import json
from typing import Dict, Any, Optional
import httpx
from mattermostdriver import Client

from flask_core import setup_aaa_logging


class MattermostBotService:
    """
    Mattermost bot service supporting:
    - WebSocket event streaming
    - Slash commands
    - Chat messages (posted events)
    - Status change events
    - Webhook verification (HMAC)
    - Message relay to channels
    """

    PLATFORM = "mattermost"

    def __init__(
        self,
        mattermost_url: str,
        bot_token: str,
        webhook_secret: str,
        router_url: str,
        dal,
        log_level: str = 'INFO'
    ):
        self.mattermost_url = mattermost_url.rstrip('/')
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret
        self.router_url = router_url
        self.dal = dal
        self.logger = setup_aaa_logging('mattermost_bot', '1.0.0')
        self._http_session: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        # Create Mattermost client
        self.client = Client({
            'url': self.mattermost_url,
            'token': bot_token,
            'port': 443
        })

    async def start(self) -> None:
        """Connect to Mattermost and start WebSocket listener"""
        try:
            # Test connection
            user = await self._async_api_call(lambda: self.client.users.get_user('me'))
            if user:
                self.logger.system(
                    f"Connected to Mattermost as {user.get('username')}",
                    action="bot_connect",
                    result="SUCCESS"
                )
            else:
                self.logger.warning("Connected to Mattermost but user info unavailable")

            # Start WebSocket listener
            self._running = True
            self._ws_task = asyncio.create_task(self._websocket_listener())
            self.logger.system("Mattermost WebSocket listener started", result="SUCCESS")

        except Exception as e:
            self.logger.error(f"Failed to start Mattermost bot: {e}", action="bot_start")
            raise

    async def stop(self) -> None:
        """Disconnect from Mattermost"""
        self.logger.system("Stopping Mattermost bot", action="bot_stop")
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._http_session and not self._http_session.is_closed:
            await self._http_session.aclose()

    async def _websocket_listener(self) -> None:
        """Listen for WebSocket events from Mattermost (placeholder for event streaming)"""
        # Note: The mattermostdriver client has WebSocket support, but it's synchronous
        # In a production setup, you would integrate with asyncio-compatible WebSocket
        # For now, this is a placeholder that could be enhanced with:
        # - Async WebSocket using websockets library
        # - Polling-based event checking
        # - Redis subscription for distributed deployments
        while self._running:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    def verify_webhook_signature(self, headers: Dict[str, str], body: Any) -> bool:
        """Verify webhook request signature using HMAC-SHA256

        Args:
            headers: Request headers from the webhook
            body: Request body (dict or JSON-serializable)

        Returns:
            True if signature is valid
        """
        if not self.webhook_secret:
            self.logger.warning("Webhook secret not configured - skipping signature verification")
            return True

        signature = headers.get('X-Mattermost-Webhook-Signature', '')
        if not signature:
            self.logger.warning("Missing webhook signature header")
            return False

        # Reconstruct the body as JSON string for signature verification
        if isinstance(body, dict):
            body_str = json.dumps(body, separators=(',', ':'))
        else:
            body_str = str(body)

        # Compute HMAC-SHA256
        expected_signature = hmac.new(
            self.webhook_secret.encode(),
            body_str.encode(),
            hashlib.sha256
        ).hexdigest()

        # Compare signatures (timing-safe comparison)
        return hmac.compare_digest(signature, expected_signature)

    async def handle_webhook_event(self, event: Dict[str, Any]) -> None:
        """Process incoming webhook event

        Args:
            event: Event data from Mattermost webhook
        """
        event_type = event.get('event', '')

        if event_type == 'posted':
            await self._handle_post_event(event)
        elif event_type == 'status_change':
            await self._handle_status_change_event(event)
        else:
            self.logger.debug(f"Unhandled webhook event type: {event_type}")

    async def _handle_post_event(self, event: Dict[str, Any]) -> None:
        """Handle a post (message) event"""
        try:
            data = event.get('data', {})
            post = json.loads(data.get('post', '{}')) if isinstance(data.get('post'), str) else data.get('post', {})

            user_id = post.get('user_id', '')
            channel_id = post.get('channel_id', '')
            team_id = data.get('team_id', '')
            text = post.get('message', '')

            if not text or post.get('type') == 'system_message':
                return  # Ignore bot/system messages

            # Get user info for username
            user = await self._async_api_call(
                lambda: self.client.users.get_user(user_id)
            )
            username = user.get('username', user_id) if user else user_id

            # Check for !prefix commands
            if text.startswith('!'):
                await self._handle_prefix_command(
                    user_id=user_id,
                    username=username,
                    channel_id=channel_id,
                    team_id=team_id,
                    text=text,
                    post_id=post.get('id', '')
                )
            else:
                # Mirror relay: forward to hub for bridging
                await self._relay_message_to_hub(
                    channel_id=channel_id,
                    user_id=user_id,
                    username=username,
                    text=text
                )

        except Exception as e:
            self.logger.error(f"Error handling post event: {e}", action="post_event")

    async def _handle_status_change_event(self, event: Dict[str, Any]) -> None:
        """Handle user status change event"""
        try:
            data = event.get('data', {})
            user_id = data.get('user_id', '')
            status = data.get('status', 'offline')

            self.logger.debug(f"User {user_id} status changed to {status}")

            # Could forward to router as a stream-like event if needed
            event_data = {
                "entity_id": f"mattermost:{user_id}",
                "user_id": user_id,
                "username": "",
                "display_name": "",
                "message": "",
                "message_type": "status_change",
                "platform": self.PLATFORM,
                "channel_id": "",
                "server_id": "mattermost",
                "metadata": {"status": status}
            }

            await self._send_to_router(event_data)

        except Exception as e:
            self.logger.error(f"Error handling status change event: {e}", action="status_event")

    async def _handle_prefix_command(
        self,
        user_id: str,
        username: str,
        channel_id: str,
        team_id: str,
        text: str,
        post_id: str
    ) -> None:
        """Handle !prefix command"""
        try:
            parts = text.split(maxsplit=1)
            command = parts[0]
            args = parts[1] if len(parts) > 1 else ''

            event_data = {
                "entity_id": f"{team_id}:{channel_id}",
                "user_id": user_id,
                "username": username,
                "display_name": username,
                "message": text,
                "message_type": "chatMessage",
                "platform": self.PLATFORM,
                "channel_id": channel_id,
                "server_id": team_id,
                "metadata": {
                    "command": command,
                    "text": args,
                    "post_id": post_id
                }
            }

            response = await self._send_to_router(event_data)

            # Execute response if present
            if response.get('success') and response.get('action'):
                action = response['action']
                if action.get('type') == 'message':
                    await self.send_to_channel(
                        channel_id=channel_id,
                        content={'text': action.get('content', '')},
                        author={'username': 'WaddleBot', 'platform': 'mattermost'},
                        message_type='response'
                    )

        except Exception as e:
            self.logger.error(f"Error handling prefix command: {e}", action="prefix_command")

    async def _relay_message_to_hub(
        self,
        channel_id: str,
        user_id: str,
        username: str,
        text: str
    ) -> None:
        """Forward a non-command message to the hub for mirror group bridging"""
        try:
            import os
            hub_api_url = os.environ.get('HUB_API_URL', 'http://hub-api:3000')
            if not self._http_session:
                return

            await self._http_session.post(
                f"{hub_api_url}/api/v1/internal/relay/incoming",
                json={
                    "sourcePlatformChannelId": channel_id,
                    "platform": "mattermost",
                    "channelType": "chat",
                    "content": {"text": text},
                    "author": {
                        "username": username,
                        "platform": "mattermost",
                    },
                    "messageType": "message",
                },
                timeout=5.0,
            )
        except Exception as e:
            self.logger.error(f"Mirror relay to hub failed: {e}", action="mirror_relay")

    async def handle_slash_command(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle Mattermost slash command request

        Args:
            data: Command data from Mattermost

        Returns:
            Response dict or None
        """
        try:
            user_id = data.get('user_id', '')
            channel_id = data.get('channel_id', '')
            team_id = data.get('team_id', '')
            command = data.get('command', '')
            text = data.get('text', '').strip()
            trigger_id = data.get('trigger_id', '')

            # Get user info
            user = await self._async_api_call(
                lambda: self.client.users.get_user(user_id)
            )
            username = user.get('username', user_id) if user else user_id

            event_data = {
                "entity_id": f"{team_id}:{channel_id}",
                "user_id": user_id,
                "username": username,
                "display_name": username,
                "message": f"{command} {text}".strip(),
                "message_type": "slashCommand",
                "platform": self.PLATFORM,
                "channel_id": channel_id,
                "server_id": team_id,
                "metadata": {
                    "command": command,
                    "text": text,
                    "trigger_id": trigger_id
                }
            }

            response = await self._send_to_router(event_data)
            return response

        except Exception as e:
            self.logger.error(f"Error handling slash command: {e}", action="slash_command")
            return {"success": False, "error": str(e)}

    async def send_to_channel(
        self,
        channel_id: str,
        content: dict,
        author: dict,
        message_type: str = 'message'
    ) -> bool:
        """Send a relayed message to a Mattermost channel

        Args:
            channel_id: Target Mattermost channel ID
            content: Message content dict with 'text' key
            author: Author info dict with 'username' and 'platform'
            message_type: Type of message

        Returns:
            True if message sent successfully
        """
        try:
            display_name = author.get('username', 'Unknown')
            platform = author.get('platform', 'hub')
            text = content.get('text', content.get('content', ''))

            message = f"**{display_name}** (via {platform}): {text}"

            post = await self._async_api_call(
                lambda: self.client.posts.create_post(
                    channel_id=channel_id,
                    options={
                        'message': message
                    }
                )
            )

            if post:
                self.logger.audit(
                    action="relay_send",
                    user="system",
                    community="",
                    result="SUCCESS",
                    detail="Relayed message sent to channel",
                    channel=channel_id
                )
                return True

            return False

        except Exception as e:
            self.logger.error(f"Failed to send relay message: {e}", action="relay_send")
            return False

    async def _send_to_router(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send event to router and get response"""
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
                    detail="Event sent to router"
                )

                if response.status_code == 200:
                    return response.json()
                return {"success": False, "error": f"Router error {response.status_code}"}

        except Exception as e:
            self.logger.error(f"Router communication failed: {e}", action="router_forward")
            return {"success": False, "error": str(e)}

    async def _async_api_call(self, sync_callable):
        """Execute a synchronous Mattermost API call in a thread pool"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, sync_callable)

    def _get_http_session(self) -> httpx.AsyncClient:
        """Get or create HTTP session"""
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session
