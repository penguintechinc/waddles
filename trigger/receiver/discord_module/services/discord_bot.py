"""
Discord Bot Service - py-cord integration with slash and prefix commands
"""
import asyncio
import os
import discord
from discord.ext import commands
import httpx
from typing import Optional, Dict, Any, List

from flask_core import setup_aaa_logging


class DiscordBotService:
    """
    Discord bot service using py-cord with support for:
    - Slash commands (/)
    - Prefix commands (!)
    - Modals and forms
    - Buttons and select menus
    - Autocomplete
    """

    def __init__(
        self,
        bot_token: str,
        application_id: str,
        router_url: str,
        dal,
        redis_url: Optional[str] = None,
        log_level: str = 'INFO'
    ):
        self.bot_token = bot_token
        self.application_id = application_id
        self.router_url = router_url
        self.dal = dal
        self.redis_url = redis_url
        self.logger = setup_aaa_logging('discord_bot', '2.0.0')
        self._http_session: Optional[httpx.AsyncClient] = None
        self._bot_task: Optional[asyncio.Task] = None

        # Setup intents for both slash and prefix commands
        intents = discord.Intents.default()
        intents.message_content = True  # Required for !prefix commands
        intents.guilds = True
        intents.members = True

        # Create the bot instance
        self.bot = discord.Bot(intents=intents)

        # Setup event handlers
        self._setup_events()

    def _setup_events(self):
        """Register bot event handlers"""

        @self.bot.event
        async def on_ready():
            self.logger.system(
                f"Discord bot connected as {self.bot.user}",
                action="bot_ready",
                result="SUCCESS"
            )
            # Sync commands on startup
            await self._sync_commands()

        @self.bot.event
        async def on_message(message: discord.Message):
            # Ignore bot messages
            if message.author.bot:
                return
            # Handle !prefix commands
            if message.content.startswith('!'):
                await self._handle_prefix_command(message)
                return

            # Mirror relay: forward non-command messages to hub for bridging
            if message.guild and message.channel and message.content:
                await self._relay_message_to_hub(message)

        @self.bot.event
        async def on_application_command_error(ctx, error):
            self.logger.error(
                f"Slash command error: {error}",
                action="slash_command_error",
                user=str(ctx.author.id)
            )
            await ctx.respond(
                "An error occurred processing your command.",
                ephemeral=True
            )

    async def _sync_commands(self):
        """Sync slash commands with Discord"""
        try:
            # Register base slash commands
            self._register_slash_commands()
            await self.bot.sync_commands()
            self.logger.system(
                "Slash commands synced",
                action="sync_commands",
                result="SUCCESS"
            )
        except Exception as e:
            self.logger.error(
                f"Failed to sync commands: {e}",
                action="sync_commands",
                result="FAILED"
            )

    def _register_slash_commands(self):
        """Register slash commands with the bot"""

        # Main waddlebot command group
        waddlebot = self.bot.create_group(
            "waddlebot",
            "WaddleBot commands"
        )

        @waddlebot.command(name="help", description="Get help with WaddleBot")
        async def help_command(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "help", {})

        @waddlebot.command(name="status", description="Check bot status")
        async def status_command(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "status", {})

        # Generic command with autocomplete
        @self.bot.slash_command(
            name="command",
            description="Execute a WaddleBot command"
        )
        async def generic_command(
            ctx: discord.ApplicationContext,
            name: discord.Option(
                str,
                description="Command name",
                autocomplete=self._command_autocomplete
            ),
            args: discord.Option(
                str,
                description="Command arguments",
                required=False,
                default=""
            )
        ):
            await self._handle_slash_command(ctx, name, {"args": args})

        # Feedback command with modal
        @self.bot.slash_command(
            name="feedback",
            description="Submit feedback"
        )
        async def feedback_command(ctx: discord.ApplicationContext):
            modal = FeedbackModal(self)
            await ctx.send_modal(modal)

        # Register all module slash command groups
        self._register_module_commands()
        self._register_context_commands()
        self._register_linking_commands()

    def _register_module_commands(self):
        """Register per-module SlashCommandGroups for all interactive modules."""

        # ── Forms ───────────────────────────────────────────────────────
        form = self.bot.create_group("form", "Community forms")

        @form.command(name="list", description="List available forms")
        async def form_list(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/form", {"args": "list"})

        @form.command(name="submit", description="Submit a community form")
        async def form_submit(ctx: discord.ApplicationContext,
                              name: discord.Option(str, "Form name")):
            await self._handle_slash_command(ctx, "/form", {"args": f"submit {name}"})

        # ── Polls ────────────────────────────────────────────────────────
        poll = self.bot.create_group("poll", "Community polls")

        @poll.command(name="list", description="List active polls")
        async def poll_list(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/poll", {"args": "list"})

        @poll.command(name="vote", description="Vote on a poll")
        async def poll_vote(ctx: discord.ApplicationContext,
                            name: discord.Option(str, "Poll name")):
            await self._handle_slash_command(ctx, "/poll", {"args": f"vote {name}"})

        # ── Tickets / Support ────────────────────────────────────────────
        ticket = self.bot.create_group("ticket", "Support tickets")

        @ticket.command(name="new", description="Open a new support ticket")
        async def ticket_new(ctx: discord.ApplicationContext,
                             category: discord.Option(str, "Category", required=False, default="")):
            await self._handle_slash_command(ctx, "/ticket", {"args": f"new {category}".strip()})

        @ticket.command(name="status", description="Check your ticket status")
        async def ticket_status(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/ticket", {"args": "status"})

        # ── Loyalty ──────────────────────────────────────────────────────
        @self.bot.slash_command(name="balance", description="Check your loyalty balance")
        async def balance(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/balance", {})

        @self.bot.slash_command(name="give", description="Give loyalty points to another user")
        async def give(ctx: discord.ApplicationContext,
                       user: discord.Option(discord.Member, "User to give points to"),
                       amount: discord.Option(int, "Amount to give")):
            await self._handle_slash_command(ctx, "/give", {"args": f"{user.id} {amount}"})

        @self.bot.slash_command(name="slots", description="Play the loyalty slots minigame")
        async def slots(ctx: discord.ApplicationContext,
                        bet: discord.Option(int, "Bet amount", required=False, default=0)):
            await self._handle_slash_command(ctx, "/slots", {"args": str(bet) if bet else ""})

        @self.bot.slash_command(name="duel", description="Challenge a user to a loyalty duel")
        async def duel(ctx: discord.ApplicationContext,
                       user: discord.Option(discord.Member, "User to duel"),
                       bet: discord.Option(int, "Bet amount")):
            await self._handle_slash_command(ctx, "/duel", {"args": f"{user.id} {bet}"})

        @self.bot.slash_command(name="giveaway", description="Start or enter a giveaway")
        async def giveaway(ctx: discord.ApplicationContext,
                           action: discord.Option(str, "start or enter", required=False, default="enter")):
            await self._handle_slash_command(ctx, "/giveaway", {"args": action})

        # ── Memories / Quotes ────────────────────────────────────────────
        quote = self.bot.create_group("quote", "Community quotes")

        @quote.command(name="add", description="Add a quote")
        async def quote_add(ctx: discord.ApplicationContext,
                            text: discord.Option(str, "Quote text")):
            await self._handle_slash_command(ctx, "/quote", {"args": f"add {text}"})

        @quote.command(name="random", description="Get a random quote")
        async def quote_random(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/quote", {"args": "random"})

        @quote.command(name="search", description="Search quotes")
        async def quote_search(ctx: discord.ApplicationContext,
                               term: discord.Option(str, "Search term")):
            await self._handle_slash_command(ctx, "/quote", {"args": f"search {term}"})

        @self.bot.slash_command(name="bookmark", description="Save a URL bookmark")
        async def bookmark(ctx: discord.ApplicationContext,
                           url: discord.Option(str, "URL to bookmark"),
                           tags: discord.Option(str, "Tags", required=False, default="")):
            await self._handle_slash_command(ctx, "/bookmark", {"args": f"{url} {tags}".strip()})

        @self.bot.slash_command(name="remind", description="Set a personal reminder")
        async def remind(ctx: discord.ApplicationContext,
                         time: discord.Option(str, "When (e.g. 30m, 2h)"),
                         message: discord.Option(str, "Reminder message")):
            await self._handle_slash_command(ctx, "/remind", {"args": f"{time} {message}"})

        # ── LFG ──────────────────────────────────────────────────────────
        lfg = self.bot.create_group("lfg", "Looking for group")

        @lfg.command(name="create", description="Create a new LFG post")
        async def lfg_create(ctx: discord.ApplicationContext,
                             game: discord.Option(str, "Game name"),
                             players: discord.Option(int, "Players needed", required=False, default=4)):
            await self._handle_slash_command(ctx, "/lfg", {"args": f"create {game} {players}"})

        @lfg.command(name="list", description="List active LFG posts")
        async def lfg_list(ctx: discord.ApplicationContext,
                           game: discord.Option(str, "Filter by game", required=False, default="")):
            await self._handle_slash_command(ctx, "/lfg", {"args": f"list {game}".strip()})

        @lfg.command(name="join", description="Join an LFG group")
        async def lfg_join(ctx: discord.ApplicationContext,
                           id: discord.Option(str, "LFG post ID")):
            await self._handle_slash_command(ctx, "/lfg", {"args": f"join {id}"})

        @lfg.command(name="leave", description="Leave your current LFG group")
        async def lfg_leave(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/lfg", {"args": "leave"})

        # ── Calendar / Events ────────────────────────────────────────────
        event = self.bot.create_group("event", "Community events")

        @event.command(name="list", description="List upcoming events")
        async def event_list(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/event", {"args": "list"})

        @event.command(name="view", description="View event details")
        async def event_view(ctx: discord.ApplicationContext,
                             id: discord.Option(str, "Event ID")):
            await self._handle_slash_command(ctx, "/event", {"args": f"view {id}"})

        @self.bot.slash_command(name="rsvp", description="RSVP to a community event")
        async def rsvp(ctx: discord.ApplicationContext,
                       event_id: discord.Option(str, "Event ID")):
            await self._handle_slash_command(ctx, "/rsvp", {"args": event_id})

        # ── Shoutout ─────────────────────────────────────────────────────
        @self.bot.slash_command(name="so", description="Give a shoutout to a user")
        async def shoutout(ctx: discord.ApplicationContext,
                           user: discord.Option(discord.Member, "User to shoutout")):
            await self._handle_slash_command(ctx, "/so", {"args": str(user.id)})

        # ── Translate ────────────────────────────────────────────────────
        @self.bot.slash_command(name="translate", description="Translate text to a language")
        async def translate(ctx: discord.ApplicationContext,
                            lang: discord.Option(str, "Target language code (e.g. es, fr)"),
                            text: discord.Option(str, "Text to translate")):
            await self._handle_slash_command(ctx, "/translate", {"args": f"{lang} {text}"})

        # ── Clip ─────────────────────────────────────────────────────────
        clip = self.bot.create_group("clip", "Stream clips")

        @clip.command(name="bookmark", description="Bookmark a clip URL")
        async def clip_bookmark(ctx: discord.ApplicationContext,
                                url: discord.Option(str, "Clip URL")):
            await self._handle_slash_command(ctx, "/clip", {"args": f"bookmark {url}"})

        @clip.command(name="list", description="List saved clips")
        async def clip_list(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/clip", {"args": "list"})

        # ── Alias ────────────────────────────────────────────────────────
        alias = self.bot.create_group("alias", "Command aliases")

        @alias.command(name="create", description="Create a command alias")
        async def alias_create(ctx: discord.ApplicationContext,
                               name: discord.Option(str, "Alias name"),
                               command: discord.Option(str, "Command to run")):
            await self._handle_slash_command(ctx, "/alias", {"args": f"create {name} {command}"})

        @alias.command(name="list", description="List your aliases")
        async def alias_list(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/alias", {"args": "list"})

        @alias.command(name="delete", description="Delete an alias")
        async def alias_delete(ctx: discord.ApplicationContext,
                               name: discord.Option(str, "Alias name")):
            await self._handle_slash_command(ctx, "/alias", {"args": f"delete {name}"})

        # ── AI ───────────────────────────────────────────────────────────
        @self.bot.slash_command(name="ask", description="Ask the AI a question")
        async def ask(ctx: discord.ApplicationContext,
                      question: discord.Option(str, "Your question")):
            await self._handle_slash_command(ctx, "/ask", {"args": question})

        # ── Reputation ───────────────────────────────────────────────────
        @self.bot.slash_command(name="rep", description="View a user's reputation score")
        async def rep(ctx: discord.ApplicationContext,
                      user: discord.Option(discord.Member, "User to check", required=False)):
            await self._handle_slash_command(ctx, "/rep",
                                             {"args": str(user.id) if user else ""})

        # ── Labels ───────────────────────────────────────────────────────
        label = self.bot.create_group("label", "User labels (mod only)")

        @label.command(name="add", description="Add a label to a user")
        async def label_add(ctx: discord.ApplicationContext,
                            user: discord.Option(discord.Member, "User"),
                            label_name: discord.Option(str, "Label to add")):
            await self._handle_slash_command(ctx, "/label",
                                             {"args": f"add {user.id} {label_name}"})

        @label.command(name="list", description="List a user's labels")
        async def label_list(ctx: discord.ApplicationContext,
                             user: discord.Option(discord.Member, "User")):
            await self._handle_slash_command(ctx, "/label",
                                             {"args": f"list {user.id}"})

        # ── Leaderboard ──────────────────────────────────────────────────
        @self.bot.slash_command(name="top", description="View community leaderboard")
        async def top(ctx: discord.ApplicationContext,
                      category: discord.Option(str, "Category (messages, loyalty, reputation...)",
                                               required=False, default="")):
            await self._handle_slash_command(ctx, "/top", {"args": category})

    def _register_context_commands(self):
        """Register /context SlashCommandGroup for community context switching."""
        context_grp = self.bot.create_group("context", "Manage your community context")

        @context_grp.command(name="show", description="Show your current community context")
        async def context_show(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/context", {"args": "show"})

        @context_grp.command(name="switch", description="Switch to a different linked community")
        async def context_switch(ctx: discord.ApplicationContext,
                                 community: discord.Option(str, "Community name or slug")):
            await self._handle_slash_command(ctx, "/context", {"args": f"switch {community}"})

        @context_grp.command(name="reset", description="Reset to the channel's default community")
        async def context_reset(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/context", {"args": "reset"})

        @context_grp.command(name="default", description="Set default community for this channel (admin only)")
        async def context_default(ctx: discord.ApplicationContext,
                                  community: discord.Option(str, "Community name or slug")):
            # Require server administrator permission
            if not ctx.author.guild_permissions.administrator:
                await ctx.respond("Server administrator permission required.", ephemeral=True)
                return
            await self._handle_slash_command(ctx, "/context", {"args": f"default {community}"})

    def _register_linking_commands(self):
        """Register server linking commands (/join, /approve, /leave, /linked, /link)."""

        def _require_admin(ctx: discord.ApplicationContext) -> bool:
            return ctx.author.guild_permissions.administrator

        @self.bot.slash_command(name="join",
                                description="Request to link this server to a community")
        async def join_cmd(ctx: discord.ApplicationContext,
                           community: discord.Option(str, "Community name or slug")):
            if not _require_admin(ctx):
                await ctx.respond("Server administrator permission required.", ephemeral=True)
                return
            await self._handle_slash_command(ctx, "/join", {"args": community})

        @self.bot.slash_command(name="approve",
                                description="Approve a pending community link request")
        async def approve_cmd(ctx: discord.ApplicationContext,
                              community: discord.Option(str, "Community name to approve")):
            if not _require_admin(ctx):
                await ctx.respond("Server administrator permission required.", ephemeral=True)
                return
            await self._handle_slash_command(ctx, "/approve", {"args": community})

        @self.bot.slash_command(name="leave",
                                description="Remove this server's link to a community")
        async def leave_cmd(ctx: discord.ApplicationContext,
                            community: discord.Option(str, "Community name")):
            if not _require_admin(ctx):
                await ctx.respond("Server administrator permission required.", ephemeral=True)
                return
            await self._handle_slash_command(ctx, "/leave", {"args": community})

        @self.bot.slash_command(name="linked",
                                description="List communities linked to this server")
        async def linked_cmd(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/linked", {})

        link_grp = self.bot.create_group("link", "Manage server community links")

        @link_grp.command(name="status", description="Show link status for this server")
        async def link_status(ctx: discord.ApplicationContext):
            await self._handle_slash_command(ctx, "/link", {"args": "status"})

        @link_grp.command(name="default", description="Set the default community (admin only)")
        async def link_default(ctx: discord.ApplicationContext,
                               community: discord.Option(str, "Community name or slug")):
            if not _require_admin(ctx):
                await ctx.respond("Server administrator permission required.", ephemeral=True)
                return
            await self._handle_slash_command(ctx, "/link", {"args": f"default {community}"})

    async def _command_autocomplete(
        self,
        ctx: discord.AutocompleteContext
    ) -> List[str]:
        """Provide autocomplete suggestions for commands"""
        try:
            # Fetch available commands from router
            async with self._get_http_session() as client:
                response = await client.get(
                    f"{self.router_url}/commands",
                    params={"platform": "discord"},
                    timeout=5.0
                )
                if response.status_code == 200:
                    data = response.json()
                    commands = data.get('commands', [])
                    # Filter by current input
                    current = ctx.value.lower() if ctx.value else ""
                    return [
                        cmd['name'] for cmd in commands
                        if cmd['name'].lower().startswith(current)
                    ][:25]  # Discord limit
        except Exception as e:
            self.logger.error(f"Autocomplete failed: {e}")
        return []

    async def _handle_slash_command(
        self,
        ctx: discord.ApplicationContext,
        command_name: str,
        options: Dict[str, Any]
    ):
        """Forward slash command to router"""
        # Defer response for potentially long operations
        await ctx.defer()

        event_data = self._build_event_data(
            message_type="slashCommand",
            user=ctx.author,
            channel=ctx.channel,
            guild=ctx.guild,
            message=f"/{command_name} {options.get('args', '')}".strip(),
            metadata={
                "interaction_id": str(ctx.interaction.id),
                "command_name": command_name,
                "options": options,
                "ephemeral_requested": False
            }
        )

        response = await self._send_to_router(event_data)
        await self._execute_response(ctx, response)

    async def _relay_message_to_hub(self, message: discord.Message):
        """Forward a non-command message to the hub for mirror group bridging"""
        try:
            hub_api_url = os.environ.get('HUB_API_URL', 'http://hub-api:3000')
            if not self._http_session:
                return
            await self._http_session.post(
                f"{hub_api_url}/api/v1/internal/relay/incoming",
                json={
                    "sourcePlatformChannelId": str(message.channel.id),
                    "platform": "discord",
                    "channelType": "chat",
                    "content": {"text": message.content},
                    "author": {
                        "username": message.author.display_name,
                        "avatarUrl": str(message.author.avatar.url) if message.author.avatar else None,
                        "platform": "discord",
                    },
                    "messageType": "message",
                },
                timeout=5.0,
            )
        except Exception as e:
            self.logger.error(f"Mirror relay to hub failed: {e}", action="mirror_relay")

    async def send_to_channel(self, channel_id: str, content: dict, author: dict, message_type: str = 'message'):
        """Send a relayed message to a Discord channel (called by internal relay endpoint)"""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if not channel:
                self.logger.error(f"Channel {channel_id} not found", action="relay_send")
                return False

            display_name = author.get('username', 'Unknown')
            platform = author.get('platform', 'hub')
            text = content.get('text', content.get('content', ''))

            if message_type == 'forum_post' and hasattr(channel, 'create_thread'):
                title = content.get('title', 'New Post')
                body = content.get('body', '')
                thread = await channel.create_thread(
                    name=title,
                    content=f"**{display_name}** (via {platform}):\n{body}",
                )
                return True

            await channel.send(f"**{display_name}** (via {platform}): {text}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to send relay message: {e}", action="relay_send")
            return False

    async def _handle_prefix_command(self, message: discord.Message):
        """Handle !prefix commands from chat"""
        event_data = self._build_event_data(
            message_type="chatMessage",
            user=message.author,
            channel=message.channel,
            guild=message.guild,
            message=message.content,
            metadata={
                "message_id": str(message.id),
                "channel_type": str(message.channel.type)
            }
        )

        response = await self._send_to_router(event_data)
        await self._execute_message_response(message, response)

    def _build_event_data(
        self,
        message_type: str,
        user: discord.User,
        channel: discord.abc.Messageable,
        guild: Optional[discord.Guild],
        message: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build standardized event data for router"""
        guild_id = str(guild.id) if guild else "DM"
        channel_id = str(channel.id) if hasattr(channel, 'id') else "DM"

        return {
            "entity_id": f"{guild_id}:{channel_id}",
            "user_id": str(user.id),
            "username": user.name,
            "display_name": user.display_name,
            "message": message,
            "message_type": message_type,
            "platform": "discord",
            "channel_id": channel_id,
            "server_id": guild_id,
            "metadata": metadata
        }

    async def _send_to_router(
        self,
        event_data: Dict[str, Any]
    ) -> Dict[str, Any]:
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

    async def _execute_response(
        self,
        ctx: discord.ApplicationContext,
        response: Dict[str, Any]
    ):
        """Execute router response for slash commands"""
        if not response.get('success', False):
            await ctx.followup.send(
                response.get('error', 'Command failed'),
                ephemeral=True
            )
            return

        action = response.get('action', {})
        action_type = action.get('type', 'message')

        if action_type == 'message':
            content = action.get('content', 'No response')
            embed = self._build_embed(action.get('embed'))
            view = self._build_view(action.get('components'))
            await ctx.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=action.get('ephemeral', False)
            )

        elif action_type == 'modal':
            # Can't show modal in followup, would need different flow
            await ctx.followup.send(
                "Modal requested - please use the command directly",
                ephemeral=True
            )

    async def _execute_message_response(
        self,
        message: discord.Message,
        response: Dict[str, Any]
    ):
        """Execute router response for prefix commands"""
        if not response.get('success', False):
            await message.reply(
                response.get('error', 'Command failed'),
                mention_author=False
            )
            return

        action = response.get('action', {})
        content = action.get('content', 'No response')
        embed = self._build_embed(action.get('embed'))
        view = self._build_view(action.get('components'))

        await message.reply(
            content=content,
            embed=embed,
            view=view,
            mention_author=False
        )

    def _build_embed(
        self,
        embed_config: Optional[Dict[str, Any]]
    ) -> Optional[discord.Embed]:
        """Build Discord embed from config"""
        if not embed_config:
            return None

        embed = discord.Embed(
            title=embed_config.get('title'),
            description=embed_config.get('description'),
            color=discord.Color(embed_config.get('color', 0x5865F2))
        )

        if embed_config.get('thumbnail'):
            embed.set_thumbnail(url=embed_config['thumbnail'])

        if embed_config.get('image'):
            embed.set_image(url=embed_config['image'])

        for field in embed_config.get('fields', []):
            embed.add_field(
                name=field.get('name', 'Field'),
                value=field.get('value', ''),
                inline=field.get('inline', False)
            )

        if embed_config.get('footer'):
            embed.set_footer(text=embed_config['footer'])

        return embed

    def _build_view(
        self,
        components: Optional[List[Dict[str, Any]]]
    ) -> Optional[discord.ui.View]:
        """Build View with buttons/selects from config"""
        if not components:
            return None

        view = discord.ui.View(timeout=300)

        for component in components:
            comp_type = component.get('type')

            if comp_type == 'button':
                button = discord.ui.Button(
                    style=self._get_button_style(component.get('style', 'primary')),
                    label=component.get('label', 'Button'),
                    custom_id=component.get('custom_id'),
                    disabled=component.get('disabled', False)
                )
                button.callback = self._create_button_callback(component['custom_id'])
                view.add_item(button)

            elif comp_type == 'select':
                select = discord.ui.Select(
                    placeholder=component.get('placeholder', 'Select...'),
                    custom_id=component.get('custom_id'),
                    options=[
                        discord.SelectOption(
                            label=opt.get('label'),
                            value=opt.get('value'),
                            description=opt.get('description')
                        )
                        for opt in component.get('options', [])
                    ]
                )
                select.callback = self._create_select_callback(component['custom_id'])
                view.add_item(select)

        return view

    def _get_button_style(self, style: str) -> discord.ButtonStyle:
        """Convert style string to Discord ButtonStyle"""
        styles = {
            'primary': discord.ButtonStyle.primary,
            'secondary': discord.ButtonStyle.secondary,
            'success': discord.ButtonStyle.success,
            'danger': discord.ButtonStyle.danger,
            'link': discord.ButtonStyle.link
        }
        return styles.get(style, discord.ButtonStyle.primary)

    def _create_button_callback(self, custom_id: str):
        """Create callback for button interaction"""
        async def callback(interaction: discord.Interaction):
            await self._handle_interaction(interaction, 'button', custom_id)
        return callback

    def _create_select_callback(self, custom_id: str):
        """Create callback for select interaction"""
        async def callback(interaction: discord.Interaction):
            await self._handle_interaction(
                interaction,
                'select',
                custom_id,
                values=interaction.data.get('values', [])
            )
        return callback

    async def _handle_interaction(
        self,
        interaction: discord.Interaction,
        interaction_type: str,
        custom_id: str,
        values: Optional[List[str]] = None
    ):
        """Handle button/select interactions"""
        await interaction.response.defer()

        event_data = self._build_event_data(
            message_type="interaction",
            user=interaction.user,
            channel=interaction.channel,
            guild=interaction.guild,
            message="",
            metadata={
                "interaction_type": interaction_type,
                "custom_id": custom_id,
                "values": values or [],
                "message_id": str(interaction.message.id) if interaction.message else None,
                "interaction_id": str(interaction.id)
            }
        )

        response = await self._send_to_router(event_data)

        # Handle response
        action = response.get('action', {})
        content = action.get('content', 'Done')
        embed = self._build_embed(action.get('embed'))

        await interaction.followup.send(
            content=content,
            embed=embed,
            ephemeral=action.get('ephemeral', True)
        )

    def _get_http_session(self) -> httpx.AsyncClient:
        """Get or create HTTP session"""
        if self._http_session is None or self._http_session.is_closed:
            self._http_session = httpx.AsyncClient()
        return self._http_session

    async def start(self):
        """Start the Discord bot in a background task"""
        self.logger.system("Starting Discord bot", action="bot_start")
        self._bot_task = asyncio.create_task(
            self.bot.start(self.bot_token)
        )

    async def stop(self):
        """Stop the Discord bot gracefully"""
        self.logger.system("Stopping Discord bot", action="bot_stop")
        if self._http_session and not self._http_session.is_closed:
            await self._http_session.aclose()
        if self.bot:
            await self.bot.close()
        if self._bot_task:
            self._bot_task.cancel()


class FeedbackModal(discord.ui.Modal):
    """Modal for feedback submission"""

    def __init__(self, bot_service: DiscordBotService):
        super().__init__(title="Submit Feedback")
        self.bot_service = bot_service

        self.add_item(
            discord.ui.InputText(
                label="Subject",
                placeholder="Brief summary of your feedback",
                max_length=100,
                required=True
            )
        )

        self.add_item(
            discord.ui.InputText(
                label="Details",
                placeholder="Provide more details about your feedback...",
                style=discord.InputTextStyle.long,
                max_length=1000,
                required=True
            )
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle modal submission"""
        subject = self.children[0].value
        details = self.children[1].value

        event_data = self.bot_service._build_event_data(
            message_type="interaction",
            user=interaction.user,
            channel=interaction.channel,
            guild=interaction.guild,
            message="",
            metadata={
                "interaction_type": "modal_submit",
                "custom_id": "feedback_modal",
                "values": {
                    "subject": subject,
                    "details": details
                }
            }
        )

        response = await self.bot_service._send_to_router(event_data)

        await interaction.response.send_message(
            "Thank you for your feedback!",
            ephemeral=True
        )
