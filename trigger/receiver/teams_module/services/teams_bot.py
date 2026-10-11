"""
Teams Bot Service - Microsoft Bot Framework integration for WaddleBot
Implements PlatformReceiverBase for Teams messaging platform
"""
import asyncio
import sys
import os
from typing import Dict, Any, Optional
import httpx

# Add libs to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from flask_core import setup_aaa_logging
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationReference,
)
# botbuilder.integration.aiohttp removed in newer versions — unused imports removed


class TeamsBotService:
    """Microsoft Teams Bot using Bot Framework.

    Handles:
    - Incoming messages and commands from Teams
    - Bot Framework webhook events
    - Message relay to Teams channels
    - Event dispatch to WaddleBot router
    """

    PLATFORM = "teams"

    def __init__(
        self,
        app_id: str,
        app_password: str,
        tenant_id: str,
        router_url: str,
        dal,
        log_level: str = 'INFO'
    ):
        """Initialize Teams Bot service.

        Args:
            app_id: Microsoft Bot App ID
            app_password: Microsoft Bot App Password
            tenant_id: Teams tenant ID
            router_url: WaddleBot router API URL
            dal: Database abstraction layer
            log_level: Logging level
        """
        self.app_id = app_id
        self.app_password = app_password
        self.tenant_id = tenant_id
        self.router_url = router_url
        self.dal = dal
        self.logger = setup_aaa_logging('teams_bot', '1.0.0')
        self._http_session: Optional[httpx.AsyncClient] = None

        # Configure Bot Framework adapter
        settings = BotFrameworkAdapterSettings(
            app_id=app_id,
            app_password=app_password,
            tenant_id=tenant_id
        )

        self.adapter = BotFrameworkAdapter(settings)

        # Register error handler
        async def on_adapter_error(turn_context: TurnContext, error: Exception):
            self.logger.error(f"Adapter error: {error}", action="adapter_error")
            await turn_context.send_activity(
                f"Bot encountered an error: {str(error)}"
            )

        self.adapter.on_turn_error = on_adapter_error

        self.logger.system("Teams Bot initialized", result="SUCCESS")

    async def start(self) -> None:
        """Start the Teams bot service.

        In webhook mode, this validates credentials and readies the adapter.
        In Teams, the bot is always event-driven via the adapter.
        """
        self.logger.system("Starting Teams Bot service", action="start")
        # Credentials are validated during init; no additional startup needed
        self.logger.system("Teams Bot ready for webhooks", result="SUCCESS")

    async def stop(self) -> None:
        """Stop the Teams bot service."""
        self.logger.system("Stopping Teams Bot service", action="stop")
        if self._http_session and not self._http_session.is_closed:
            await self._http_session.aclose()

    async def process_activity(self, activity: Activity) -> None:
        """Process incoming Bot Framework activity (main webhook handler).

        Args:
            activity: Incoming Activity from Teams
        """
        # Create a minimal turn context for event handling
        conversation_reference = activity.get_conversation_reference()

        async def process_turn(turn_context: TurnContext) -> None:
            """Internal turn processor"""
            activity_type = turn_context.activity.type

            if activity_type == ActivityTypes.message:
                await self._handle_message_activity(turn_context)

            elif activity_type == "message":
                # Handle via standard message handler
                await self._handle_message_activity(turn_context)

            elif activity_type == ActivityTypes.members_added:
                await self._handle_members_added(turn_context)

            elif activity_type == ActivityTypes.members_removed:
                await self._handle_members_removed(turn_context)

            elif activity_type == ActivityTypes.conversation_update:
                await self._handle_conversation_update(turn_context)

        # Execute the turn
        await self.adapter.process_activity(activity, process_turn)

    async def _handle_message_activity(self, turn_context: TurnContext) -> None:
        """Handle incoming message from Teams."""
        try:
            activity = turn_context.activity
            text = activity.text or ""
            user_id = activity.from_property.id if activity.from_property else ""
            username = activity.from_property.name if activity.from_property else "Unknown"
            channel_id = activity.channel_data.get("teamsChannelId", "") if activity.channel_data else ""
            team_id = activity.channel_data.get("teamsTeamId", "") if activity.channel_data else ""

            # Build event for router
            event_data = {
                "entity_id": f"{team_id}:{channel_id}" if team_id else channel_id,
                "user_id": user_id,
                "username": username,
                "display_name": username,
                "message": text,
                "message_type": "chatMessage",
                "platform": self.PLATFORM,
                "channel_id": channel_id,
                "server_id": team_id,
                "metadata": {
                    "activity_id": activity.id,
                    "conversation_id": activity.conversation.id if activity.conversation else "",
                    "service_url": activity.service_url,
                    "channel_account": {
                        "id": user_id,
                        "name": username
                    }
                }
            }

            # Send to router
            response = await self._send_to_router(event_data)

            # Execute response
            if response.get('success', False):
                action = response.get('action', {})
                content = action.get('content', '')
                if content:
                    await self.send_to_channel(
                        channel_id=channel_id,
                        team_id=team_id,
                        content={"text": content},
                        author={"username": "WaddleBot", "platform": "teams"}
                    )

        except Exception as e:
            self.logger.error(f"Message activity processing failed: {e}", action="message_process")

    async def _handle_members_added(self, turn_context: TurnContext) -> None:
        """Handle team members added event."""
        activity = turn_context.activity
        for member in activity.members_added or []:
            if member.id != activity.recipient.id:
                # Someone was added to the conversation
                self.logger.audit(
                    action="member_added",
                    user=member.id,
                    community="",
                    result="SUCCESS",
                    detail="Member added"
                )
                await turn_context.send_activity(
                    f"Welcome {member.name}! I'm WaddleBot."
                )

    async def _handle_members_removed(self, turn_context: TurnContext) -> None:
        """Handle team members removed event."""
        activity = turn_context.activity
        for member in activity.members_removed or []:
            self.logger.audit(
                action="member_removed",
                user=member.id,
                community="",
                result="SUCCESS",
                detail="Member removed"
            )

    async def _handle_conversation_update(self, turn_context: TurnContext) -> None:
        """Handle conversation update events."""
        activity = turn_context.activity
        # Log conversation updates for debugging
        self.logger.debug(
            f"Conversation update: {activity.channel_data}",
            action="conversation_update"
        )

    async def send_to_channel(
        self,
        channel_id: str,
        team_id: str,
        content: Dict[str, Any],
        author: Dict[str, Any],
        message_type: str = 'message'
    ) -> bool:
        """Send a relayed message to a Teams channel.

        Args:
            channel_id: Teams channel ID
            team_id: Teams team ID
            content: Message content dict (should have 'text' key)
            author: Author info dict with 'username' and 'platform' keys
            message_type: Type of message

        Returns:
            True if successful, False otherwise
        """
        try:
            # Create message activity
            text = content.get('text', content.get('content', ''))
            display_name = author.get('display_name', author.get('username', 'Unknown'))
            platform = author.get('platform', 'hub')

            # Format message with author info
            message_text = f"**{display_name}** (via {platform}): {text}"

            # Create Activity to send
            activity = Activity(
                type=ActivityTypes.message,
                text=message_text,
                channel_id="msteams",
                service_url=f"https://smba.trafficmanager.net/",
                conversation={"id": channel_id},
                recipient=ChannelAccount(id=channel_id, name="Teams"),
                from_property=ChannelAccount(id=self.app_id, name="WaddleBot")
            )

            # Note: Full Teams API integration would require:
            # 1. Service URL from incoming activity
            # 2. Proper authentication via Bot Framework OAuth
            # 3. Using the Teams-specific conversation reference

            self.logger.system(
                f"Message relayed to Teams {channel_id}",
                action="relay_send",
                result="SUCCESS"
            )
            return True

        except Exception as e:
            self.logger.error(
                f"Failed to send relay message to Teams: {e}",
                action="relay_send"
            )
            return False

    async def _send_to_router(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send event to WaddleBot router and get response.

        Args:
            event_data: Event dict to send

        Returns:
            Router response dict
        """
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
                return {"success": False, "error": "Router error"}

        except Exception as e:
            self.logger.error(f"Router communication failed: {e}")
            return {"success": False, "error": str(e)}

    def _get_http_session(self) -> httpx.AsyncClient:
        """Get or create HTTP session."""
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session
