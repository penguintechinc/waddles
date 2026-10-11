"""
Twitch Bot Service - TwitchIO IRC integration
Supports !prefix commands via chat
"""
import asyncio
from typing import Dict, Any, Optional, List
import httpx
from twitchio.ext import commands

from flask_core import setup_aaa_logging


class TwitchBotService(commands.Bot):
    """
    Twitch IRC bot using TwitchIO supporting:
    - !prefix commands from chat
    - Whisper commands
    - Mod/sub/vip detection
    - Message forwarding to router
    """

    def __init__(
        self,
        token: str,
        client_id: str,
        nick: str,
        initial_channels: List[str],
        router_url: str,
        dal,
        channel_community_map: Dict[str, int],
        log_level: str = 'INFO'
    ):
        # Initialize TwitchIO Bot
        super().__init__(
            token=token,
            prefix='!',
            initial_channels=initial_channels
        )

        self.client_id = client_id
        self.nick = nick
        self.router_url = router_url
        self.dal = dal
        self.channel_community_map = channel_community_map
        self.logger = setup_aaa_logging('twitch_bot', '2.0.0')
        self._http_session: Optional[httpx.AsyncClient] = None

    async def event_ready(self):
        """Called when bot is ready and connected"""
        self.logger.system(
            f"Twitch bot connected as {self.nick}",
            action="bot_ready",
            result="SUCCESS"
        )
        self.logger.info(f"Connected to {len(self.connected_channels)} channels")

    async def event_channel_joined(self, channel):
        """Called when bot joins a channel"""
        self.logger.info(f"Joined channel: {channel.name}")

    async def event_message(self, message):
        """Handle incoming chat messages"""
        # Ignore echo (our own messages)
        if message.echo:
            return

        # Check for !prefix commands
        if message.content.startswith('!'):
            await self._handle_command(message)
        else:
            # Could also forward non-command messages if needed for AI/context
            pass

    # Commands that require broadcaster (channel owner) permission on Twitch.
    # The router enforces permission_level='owner' from the commands table,
    # but we also enforce it here for a fast fail before the network round-trip.
    _BROADCASTER_ONLY_COMMANDS = frozenset({
        "!join", "!approve", "!leave", "!link",
    })

    def _is_broadcaster(self, author, channel) -> bool:
        """Return True if the message author is the channel broadcaster."""
        if hasattr(author, 'is_broadcaster') and author.is_broadcaster:
            return True
        # Fallback: compare username to channel name (broadcaster owns the channel)
        return author.name.lower() == channel.name.lower()

    async def _handle_command(self, message):
        """Handle !prefix command and forward to router.

        All commands are forwarded generically — the router looks them up in
        the commands table and dispatches to the correct module.  Broadcaster-
        only commands are short-circuited here before the network call.
        """
        author = message.author
        channel = message.channel
        content = message.content.strip()

        # Extract the bare command token (e.g. "!join" from "!join CommunityXYZ")
        cmd_token = content.split()[0].lower() if content else ""

        # Enforce broadcaster-only commands locally for fast feedback
        if cmd_token in self._BROADCASTER_ONLY_COMMANDS:
            if not self._is_broadcaster(author, channel):
                await channel.send(
                    f"@{author.name} Only the channel broadcaster can use {cmd_token}."
                )
                return

        # Get community ID for this channel
        community_id = self.channel_community_map.get(
            channel.name.lower(),
            self.channel_community_map.get(str(getattr(author, 'id', '')), None)
        )

        event_data = {
            "entity_id": channel.name,
            "user_id": str(author.id) if author.id else author.name,
            "username": author.name,
            "display_name": author.display_name or author.name,
            "message": content,
            "message_type": "chatMessage",
            "platform": "twitch",
            "channel_id": channel.name,
            "server_id": channel.name,
            "metadata": {
                "is_mod": author.is_mod,
                "is_subscriber": author.is_subscriber,
                "is_broadcaster": self._is_broadcaster(author, channel),
                "is_vip": author.is_vip if hasattr(author, 'is_vip') else False,
                "badges": self._get_badges(author),
                "message_id": message.id if hasattr(message, 'id') else None,
                "community_id": community_id,
                "color": author.color if hasattr(author, 'color') else None,
            },
        }

        response = await self._send_to_router(event_data)
        await self._execute_response(channel, response, author)

    def _get_badges(self, author) -> Dict[str, str]:
        """Extract user badges"""
        badges = {}
        if hasattr(author, 'badges'):
            raw_badges = author.badges or {}
            if isinstance(raw_badges, dict):
                badges = raw_badges
            elif isinstance(raw_badges, list):
                for badge in raw_badges:
                    if isinstance(badge, dict):
                        badges[badge.get('name', '')] = badge.get('version', '1')
        return badges

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
                return {"success": False, "error": "Router error"}

        except Exception as e:
            self.logger.error(f"Router communication failed: {e}")
            return {"success": False, "error": str(e)}

    async def _execute_response(self, channel, response: Dict[str, Any], author):
        """Execute router response - send message to channel"""
        if not response.get('success', False):
            error = response.get('error', '')
            if error and not error.startswith('No command'):
                # Only send error if it's not a "command not found" type
                await channel.send(f"@{author.name} Error: {error[:450]}")
            return

        action = response.get('action', {})
        content = action.get('content', '')

        if content:
            # Twitch has 500 char limit per message
            # Split long messages if needed
            max_len = 490
            if len(content) <= max_len:
                await channel.send(content)
            else:
                # Split into multiple messages
                parts = [content[i:i + max_len] for i in range(0, len(content), max_len)]
                for part in parts[:3]:  # Max 3 messages to avoid spam
                    await channel.send(part)
                    await asyncio.sleep(0.5)  # Rate limit protection

    async def event_command_error(self, ctx, error):
        """Handle command errors"""
        self.logger.error(f"Command error in {ctx.channel.name}: {error}")

    def _get_http_session(self) -> httpx.AsyncClient:
        """Get or create HTTP session"""
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session

    async def join_channel(self, channel_name: str, community_id: Optional[int] = None):
        """Join a new channel dynamically"""
        try:
            await self.join_channels([channel_name])
            if community_id:
                self.channel_community_map[channel_name.lower()] = community_id
            self.logger.info(f"Joined channel: {channel_name}")
        except Exception as e:
            self.logger.error(f"Failed to join {channel_name}: {e}")

    async def leave_channel(self, channel_name: str):
        """Leave a channel"""
        try:
            await self.part_channels([channel_name])
            self.channel_community_map.pop(channel_name.lower(), None)
            self.logger.info(f"Left channel: {channel_name}")
        except Exception as e:
            self.logger.error(f"Failed to leave {channel_name}: {e}")

    async def send_message(self, channel_name: str, message: str):
        """Send a message to a specific channel"""
        channel = self.get_channel(channel_name)
        if channel:
            await channel.send(message)
        else:
            self.logger.warning(f"Channel not found: {channel_name}")

    async def stop(self):
        """Stop the bot gracefully"""
        self.logger.system("Stopping Twitch bot", action="bot_stop")
        if self._http_session and not self._http_session.is_closed:
            await self._http_session.aclose()
        await self.close()
