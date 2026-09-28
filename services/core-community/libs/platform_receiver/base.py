"""Platform Receiver Base — extensible contract for all platform bot services.

Every platform bot (Discord, Twitch, Slack, Kick, YouTube, etc.) wraps a
platform-specific SDK but communicates with the WaddleBot router using the
same standard event schema.  Implementing this base class guarantees a new
platform can be wired in without touching the router or any modules.

Usage
-----
class KickBotService(PlatformReceiverBase):
    PLATFORM = "kick"
    BROADCASTER_COMMANDS = frozenset({"!join", "!approve", "!leave", "!link"})

    async def start(self): ...
    async def stop(self): ...

    # Kick SDK calls self.on_chat_message() → builds event → sends to router
    async def on_chat_message(self, message):
        await self.dispatch(self.build_chat_event(
            user_id=message.sender.id,
            username=message.sender.slug,
            display_name=message.sender.username,
            content=message.content,
            channel_id=message.chatroom_id,
            server_id=message.chatroom_id,  # Kick uses channels, no guilds
            metadata={"badges": message.sender.badges},
        ))
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import logging
import httpx

logger = logging.getLogger(__name__)


class PlatformReceiverBase(ABC):
    """Abstract base class for all platform receiver bots.

    Sub-classes must set PLATFORM (e.g. "discord") and implement start()/stop().
    All command dispatch goes through dispatch() → router → modules.
    """

    PLATFORM: str = "unknown"

    # Commands that require channel-owner / broadcaster / server-admin level.
    # Sub-classes should override to match the platform's permission model.
    BROADCASTER_COMMANDS: frozenset = frozenset({
        "!join", "!approve", "!leave", "!link",
        "/join", "/approve", "/leave", "/link",
    })

    def __init__(self, router_url: str, log_level: str = "INFO"):
        self.router_url = router_url
        self.logger = logging.getLogger(f"platform.{self.PLATFORM}")
        self._http_session: Optional[httpx.AsyncClient] = None

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle (implement in sub-class)
    # ──────────────────────────────────────────────────────────────────────

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and start processing events."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect cleanly."""

    # ──────────────────────────────────────────────────────────────────────
    # Standard event builders — sub-classes call these to produce events
    # ──────────────────────────────────────────────────────────────────────

    def build_chat_event(
        self,
        user_id: str,
        username: str,
        display_name: str,
        content: str,
        channel_id: str,
        server_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a standard chatMessage event for dispatch to the router."""
        return {
            "entity_id": f"{server_id}:{channel_id}" if server_id != channel_id else channel_id,
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "message": content,
            "message_type": "chatMessage",
            "platform": self.PLATFORM,
            "channel_id": channel_id,
            "server_id": server_id,
            "metadata": metadata or {},
        }

    def build_slash_event(
        self,
        user_id: str,
        username: str,
        command: str,
        args: str,
        channel_id: str,
        server_id: str,
        trigger_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a standard slashCommand event."""
        extra = {"command": command, "text": args}
        if trigger_id:
            extra["trigger_id"] = trigger_id
        if metadata:
            extra.update(metadata)
        return {
            "entity_id": f"{server_id}:{channel_id}" if server_id != channel_id else channel_id,
            "user_id": user_id,
            "username": username,
            "display_name": username,
            "message": f"{command} {args}".strip(),
            "message_type": "slashCommand",
            "platform": self.PLATFORM,
            "channel_id": channel_id,
            "server_id": server_id,
            "metadata": extra,
        }

    def build_stream_event(
        self,
        event_type: str,
        channel_id: str,
        server_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a standard stream lifecycle event (online, offline, sub, etc.)."""
        return {
            "entity_id": f"{server_id}:{channel_id}" if server_id != channel_id else channel_id,
            "user_id": "",
            "username": "",
            "display_name": "",
            "message": "",
            "message_type": event_type,
            "platform": self.PLATFORM,
            "channel_id": channel_id,
            "server_id": server_id,
            "metadata": metadata or {},
        }

    # ──────────────────────────────────────────────────────────────────────
    # Dispatcher — sends event to router
    # ──────────────────────────────────────────────────────────────────────

    async def dispatch(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Forward a standard event dict to the WaddleBot router.

        Returns the router response dict.  On network error, returns
        ``{"success": False, "error": "..."}``.
        """
        try:
            async with self._get_http_session() as client:
                resp = await client.post(
                    f"{self.router_url}/events",
                    json=event,
                    timeout=30.0,
                )
                self.logger.debug(
                    f"Dispatched {event.get('message_type')} for {event.get('username')} → {resp.status_code}"
                )
                if resp.status_code == 200:
                    return resp.json()
                return {"success": False, "error": f"Router HTTP {resp.status_code}"}
        except Exception as exc:
            self.logger.error(f"Router dispatch failed: {exc}")
            return {"success": False, "error": str(exc)}

    # ──────────────────────────────────────────────────────────────────────
    # Permission helpers (override per-platform as needed)
    # ──────────────────────────────────────────────────────────────────────

    def is_broadcaster(self, author_name: str, channel_name: str) -> bool:
        """Default broadcaster check: compare username to channel name.

        Works for Twitch (channel name == broadcaster login) and Kick.
        Override for Discord (guild admin check) and Slack (workspace admin).
        """
        return author_name.lower() == channel_name.lower()

    def is_broadcaster_command(self, cmd_token: str) -> bool:
        """Return True if this command requires broadcaster/owner permission."""
        return cmd_token.lower() in self.BROADCASTER_COMMANDS

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    def _get_http_session(self) -> httpx.AsyncClient:
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session
