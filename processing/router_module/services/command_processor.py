"""Command Processor - Async command routing and execution"""
import asyncio
import logging
from typing import Any, Dict, List, Optional
import json

import aiohttp

from config import Config
from services.command_registry import CommandRegistry, CommandInfo
from services.context_service import ContextService
from services.grpc_clients import get_grpc_manager
from services.ai_chatter_config_cache import AiChatterConfigCache

logger = logging.getLogger(__name__)


class CommandProcessor:
    def __init__(
        self,
        dal,
        cache_manager,
        rate_limiter,
        session_manager,
        command_registry: CommandRegistry,
        stream_pipeline=None,
        ai_chatter_config_cache=None,
    ):
        self.dal = dal
        self.cache = cache_manager
        self.rate_limiter = rate_limiter
        self.session_manager = session_manager
        self.command_registry = command_registry
        self._http_session = None
        self._response_cache: Dict[str, Any] = {}  # session_id -> response
        self._grpc_manager = get_grpc_manager() if Config.GRPC_ENABLED else None
        self.stream_pipeline = stream_pipeline  # Optional Redis streams pipeline
        self.ai_chatter_config_cache = ai_chatter_config_cache  # AIChatter config cache

        # Context service: per-user community context overrides
        self.context_service = ContextService(dal, cache_manager)

        # Translate module gRPC channel (lazy-initialised)
        self._translate_channel = None
        self._translate_stub = None

    def _setup_proto_path(self):
        """Add proto path to sys.path if not already present"""
        import sys
        import os
        proto_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'libs', 'grpc_protos')
        if proto_path not in sys.path:
            sys.path.insert(0, proto_path)

    async def _get_http_session(self):
        """Get or create HTTP session for activity tracking"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            )
        return self._http_session

    async def process_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process single event with async command routing"""
        try:
            entity_id = event_data.get('entity_id')
            user_id = event_data.get('user_id')
            message = event_data.get('message', '')
            message_type = event_data.get('message_type', 'chatMessage')
            metadata = event_data.get('metadata', {})

            # Generate session ID
            session_id = await self.session_manager.create_session(entity_id, user_id)

            # Handle slash commands (Discord /command, Slack /waddlebot)
            if message_type == 'slashCommand':
                return await self._process_slash_command(
                    event_data, entity_id, user_id, session_id, metadata
                )

            # Handle interactions (button clicks, modal submits, select menus)
            if message_type in ('interaction', 'modal_submit', 'button_click', 'select_menu'):
                return await self._process_interaction(
                    event_data, entity_id, user_id, session_id, metadata
                )

            # Handle presence updates (forward to presence service)
            if message_type == 'presence_update':
                asyncio.create_task(self._forward_presence_update(event_data))
                return {"success": True, "session_id": session_id, "processed": True}

            # Handle stream events (no command response, just activity tracking)
            if message_type in ('stream_online', 'stream_offline', 'subscription',
                                'gift_subscription', 'follow', 'raid', 'cheer'):
                asyncio.create_task(self._record_stream_activity(event_data))
                # Forward reputation-impacting events
                if message_type in ('subscription', 'gift_subscription', 'follow',
                                    'raid', 'cheer'):
                    asyncio.create_task(self._record_reputation_event(event_data))
                return {"success": True, "session_id": session_id, "processed": True}

            # Record message activity for leaderboards (fire-and-forget)
            if message_type == 'chatMessage':
                asyncio.create_task(self._record_message_activity(event_data))
                # Also record for reputation tracking
                asyncio.create_task(self._record_reputation_event(event_data))

                # AIChatter: proactive response if community has opted in (fire-and-forget)
                asyncio.create_task(self._maybe_ai_chatter_respond(event_data))

                # Translate message if translation enabled for this community
                translation_result = await self._translate_message(event_data, entity_id)
                if translation_result:
                    translated_text = translation_result['translated_text']

                    # Check translated text against content filter (may contain
                    # profanity not present in the original language)
                    community_id = await self._get_community_for_entity(entity_id)
                    if community_id:
                        filter_result = await self._check_content_filter(
                            community_id=community_id,
                            message=translated_text,
                            platform=event_data.get('platform', 'unknown'),
                            user_id=user_id,
                        )
                        if filter_result and not filter_result.get('allowed', True):
                            logger.info(
                                f"Translated message blocked by content filter: "
                                f"reason={filter_result.get('blocked_reason')}, "
                                f"pattern={filter_result.get('matched_pattern')}"
                            )
                            # Skip caption broadcast for filtered translations
                            translation_result = None

                if translation_result:
                    # Update event data with translated message
                    event_data['message'] = translation_result['translated_text']
                    if 'metadata' not in event_data:
                        event_data['metadata'] = {}
                    event_data['metadata']['translation'] = translation_result
                    event_data['metadata']['original_message'] = message

                    # Send caption to overlay (fire-and-forget)
                    asyncio.create_task(
                        self._send_caption_event(event_data, translation_result, entity_id)
                    )

            # Check for prefix commands (! or #)
            if message.startswith('!') or message.startswith('#'):
                command = message.split()[0] if message else ''

                # Check rate limit (60 requests per 60 seconds)
                if not await self.rate_limiter.check_rate_limit(f"{user_id}:{command}", limit=60, window=60):
                    return {"success": False, "error": "Rate limit exceeded"}

                # Track command usage for reputation (small penalty)
                cmd_event = {**event_data, 'message_type': 'command'}
                asyncio.create_task(self._record_reputation_event(cmd_event))

                # Execute command
                result = await self.execute_command(command, entity_id, user_id, message, session_id)

                # Check for workflows triggered by this command
                await self._check_and_trigger_workflows(command, entity_id, user_id, message, session_id, event_data)

                # Publish result to actions stream if stream mode is enabled
                if Config.STREAM_PIPELINE_ENABLED:
                    await self.publish_to_stream(Config.STREAM_ACTIONS, result)

                return result

            return {"success": True, "session_id": session_id, "processed": False}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _process_slash_command(
        self,
        event_data: Dict[str, Any],
        entity_id: str,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Process slash command from Discord or Slack"""
        command_name = metadata.get('command_name', '')
        options = metadata.get('options', {})
        platform = event_data.get('platform', 'unknown')

        # Build command string from slash command (e.g., /help -> !help)
        # This allows reusing existing command routing
        command = f"!{command_name}"

        # Build message from options
        args_str = ' '.join(str(v) for v in options.values()) if options else ''
        message = f"{command} {args_str}".strip()

        # Check rate limit (60 requests per 60 seconds)
        if not await self.rate_limiter.check_rate_limit(f"{user_id}:{command}", limit=60, window=60):
            return {"success": False, "error": "Rate limit exceeded"}

        # Track slash command usage for reputation (small penalty)
        cmd_event = {
            **event_data,
            'message_type': 'slashCommand',
            'metadata': {**metadata, 'command_name': command_name}
        }
        asyncio.create_task(self._record_reputation_event(cmd_event))

        # Execute as regular command
        result = await self.execute_command(
            command, entity_id, user_id, message, session_id
        )

        # Add interaction metadata for deferred responses
        result['interaction_id'] = metadata.get('interaction_id')
        result['interaction_token'] = metadata.get('interaction_token')
        result['platform'] = platform

        return result

    async def _process_interaction(
        self,
        event_data: Dict[str, Any],
        entity_id: str,
        user_id: str,
        session_id: str,
        metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Process button click, modal submit, or select menu.

        custom_id format: module:action:context  (e.g. "inventory:buy:item_123")
        Looks up the module URL and POSTs to its /api/v1/execute endpoint.
        """
        interaction_type = event_data.get('message_type')
        custom_id = metadata.get('custom_id', '')
        values = metadata.get('values', {})
        platform = event_data.get('platform', 'unknown')

        # Parse custom_id → module : action : context
        parts = custom_id.split(':')
        module_name = parts[0] if parts else ''
        action = parts[1] if len(parts) > 1 else ''
        context = ':'.join(parts[2:]) if len(parts) > 2 else ''

        logger.info(f"Processing interaction: {interaction_type} - {custom_id}")

        if not module_name:
            return {
                "success": False,
                "error": "Interaction has no module identifier in custom_id",
                "session_id": session_id,
            }

        # Resolve community for this entity
        community_id = await self._get_community_for_entity(entity_id)

        # Look up the module's HTTP URL from the command registry (fast) or DB (fallback)
        module_url: Optional[str] = None
        try:
            result = self.dal.executesql(
                "SELECT url FROM hub_modules WHERE name = %s AND is_active = true LIMIT 1",
                [module_name],
            )
            if result:
                module_url = result[0][0]
        except Exception as e:
            logger.warning(f"Failed to look up module URL for '{module_name}': {e}")

        if not module_url:
            # Reasonable default: modules are discoverable by name on a fixed port
            module_url = f"http://{module_name}:8000"
            logger.debug(f"Module URL not found in DB, falling back to {module_url}")

        # Build the execute payload (same schema as execute_command uses)
        payload = {
            "action": action,
            "context": context,
            "values": values,
            "user_id": user_id,
            "entity_id": entity_id,
            "community_id": community_id,
            "session_id": session_id,
            "platform": platform,
            "interaction_type": interaction_type,
            "interaction_id": metadata.get('interaction_id'),
            "interaction_token": metadata.get('interaction_token'),
        }

        module_response = await self._call_module_with_retry(
            f"{module_url}/api/v1/execute",
            payload,
            max_retries=2,
        )

        if module_response:
            await self.handle_module_response({"session_id": session_id, "response": module_response})
            return {
                "success": True,
                "session_id": session_id,
                "module": module_name,
                "action": action,
                "interaction_id": metadata.get('interaction_id'),
                "interaction_token": metadata.get('interaction_token'),
                "platform": platform,
                "response": module_response,
            }

        return {
            "success": False,
            "error": f"Module '{module_name}' did not respond to interaction",
            "session_id": session_id,
            "interaction_id": metadata.get('interaction_id'),
            "interaction_token": metadata.get('interaction_token'),
            "platform": platform,
        }

    async def _forward_presence_update(self, event_data: Dict[str, Any]):
        """Forward presence update events to the presence service."""
        try:
            session = await self._get_http_session()
            presence_url = getattr(Config, 'PRESENCE_API_URL', None)
            if not presence_url:
                logger.debug("PRESENCE_API_URL not configured, skipping presence update")
                return

            async with session.post(
                f"{presence_url}/api/v1/presence/update",
                json=event_data,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Presence update forward failed: HTTP %d",
                        resp.status,
                        extra={"event": event_data.get("user_id")}
                    )
        except Exception as exc:
            logger.warning("Failed to forward presence update: %s", exc)

    async def _record_stream_activity(self, event_data: Dict[str, Any]):
        """Record stream events (subs, follows, raids, etc.) for activity tracking"""
        try:
            entity_id = event_data.get('entity_id')
            community_id = await self._get_community_for_entity(entity_id)

            if not community_id:
                return

            # Try gRPC first if enabled
            if self._grpc_manager and Config.GRPC_ENABLED:
                try:
                    # Import proto files dynamically to avoid startup errors if not generated
                    self._setup_proto_path()
                    from hub_internal_pb2 import RecordActivityRequest
                    from hub_internal_pb2_grpc import HubInternalServiceStub

                    channel = await self._grpc_manager.get_channel('hub_internal')
                    stub = HubInternalServiceStub(channel)

                    # Convert metadata dict to string dict (proto maps require string values)
                    metadata = event_data.get('metadata', {})
                    metadata_str = {k: str(v) for k, v in metadata.items()}

                    request = RecordActivityRequest(
                        token=self._grpc_manager.generate_token(),
                        community_id=community_id,
                        platform=event_data.get('platform', 'unknown'),
                        platform_user_id=event_data.get('user_id', ''),
                        platform_username=event_data.get('username', ''),
                        channel_id=event_data.get('channel_id', entity_id),
                        event_type=event_data.get('message_type'),
                        metadata=metadata_str
                    )

                    await self._grpc_manager.call_with_retry(stub.RecordActivity, request, timeout=5.0)
                    logger.debug(f"Recorded stream activity via gRPC for community {community_id}")
                    return
                except Exception as e:
                    logger.warning(f"gRPC call failed for stream activity, falling back to REST: {e}")

            # Fallback to REST
            if not Config.HUB_API_URL or not Config.SERVICE_API_KEY:
                return

            session = await self._get_http_session()
            payload = {
                'community_id': community_id,
                'platform': event_data.get('platform', 'unknown'),
                'platform_user_id': event_data.get('user_id', ''),
                'platform_username': event_data.get('username', ''),
                'channel_id': event_data.get('channel_id', entity_id),
                'event_type': event_data.get('message_type'),
                'metadata': event_data.get('metadata', {})
            }

            await session.post(
                f"{Config.HUB_API_URL}/api/v1/internal/activity/event",
                json=payload,
                headers={'X-Service-Key': Config.SERVICE_API_KEY},
            )
            logger.debug(f"Recorded stream activity via REST for community {community_id}")
        except Exception as e:
            logger.warning(f"Failed to record stream activity: {e}")

    async def _record_message_activity(self, event_data: Dict[str, Any]):
        """Record message activity to hub for leaderboard tracking"""
        try:
            # Get community_id from entity mapping (entity_id -> community)
            entity_id = event_data.get('entity_id')
            community_id = await self._get_community_for_entity(entity_id)

            if not community_id:
                return

            # Try gRPC first if enabled
            if self._grpc_manager and Config.GRPC_ENABLED:
                try:
                    # Import proto files dynamically
                    self._setup_proto_path()
                    from hub_internal_pb2 import RecordMessageRequest
                    from hub_internal_pb2_grpc import HubInternalServiceStub

                    channel = await self._grpc_manager.get_channel('hub_internal')
                    stub = HubInternalServiceStub(channel)

                    request = RecordMessageRequest(
                        token=self._grpc_manager.generate_token(),
                        community_id=community_id,
                        platform=event_data.get('platform', 'unknown'),
                        platform_user_id=event_data.get('user_id', ''),
                        platform_username=event_data.get('username', ''),
                        channel_id=event_data.get('channel_id', entity_id),
                        message_content=event_data.get('message', '')
                    )

                    await self._grpc_manager.call_with_retry(stub.RecordMessage, request, timeout=5.0)
                    logger.debug(f"Recorded message activity via gRPC for community {community_id}")
                    return
                except Exception as e:
                    logger.warning(f"gRPC call failed for message activity, falling back to REST: {e}")

            # Fallback to REST
            if not Config.HUB_API_URL or not Config.SERVICE_API_KEY:
                return

            session = await self._get_http_session()
            payload = {
                'community_id': community_id,
                'platform': event_data.get('platform', 'unknown'),
                'platform_user_id': event_data.get('user_id', ''),
                'platform_username': event_data.get('username', ''),
                'channel_id': event_data.get('channel_id', entity_id),
            }

            await session.post(
                f"{Config.HUB_API_URL}/api/v1/internal/activity/message",
                json=payload,
                headers={'X-Service-Key': Config.SERVICE_API_KEY},
            )
            logger.debug(f"Recorded message activity via REST for community {community_id}")
        except Exception as e:
            # Don't let activity tracking failures affect message processing
            logger.warning(f"Failed to record message activity: {e}")

    async def _get_community_for_entity(
        self,
        entity_id: str,
        user_id: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> int | None:
        """Get community ID for an entity, respecting per-user context overrides.

        Resolution order:
          1. Per-user override (Redis / user_platform_context table)
          2. Channel/server primary community (community_servers, cached)
          3. None
        """
        if not entity_id:
            return None

        # 1. Per-user override (only when user_id and platform are provided)
        if user_id and platform:
            user_ctx = await self.context_service.get_context(platform, user_id, entity_id)
            if user_ctx:
                return user_ctx

        # 2. Channel/server default — check cache first
        cache_key = f"entity:community:{entity_id}"
        cached = await self.cache.get(cache_key)
        if cached:
            return int(cached)

        # 3. Query database for server -> primary community mapping
        try:
            result = self.dal.executesql(
                """SELECT cs.community_id FROM community_servers cs
                   WHERE cs.platform_server_id = %s
                     AND cs.status = 'approved'
                   ORDER BY cs.is_primary DESC NULLS LAST
                   LIMIT 1""",
                [entity_id],
            )
            if result:
                community_id = result[0][0]
                await self.cache.set(cache_key, str(community_id), ttl=Config.ROUTER_ENTITY_CACHE_TTL)
                return community_id
        except Exception as e:
            logger.warning(f"Failed to lookup community for entity {entity_id}: {e}")

        return None

    async def _record_reputation_event(self, event_data: Dict[str, Any]):
        """Forward event to reputation module for score adjustment"""
        try:
            if not Config.REPUTATION_ENABLED:
                return

            entity_id = event_data.get('entity_id')
            community_id = await self._get_community_for_entity(entity_id)

            if not community_id:
                return

            # Try gRPC first if enabled
            if self._grpc_manager and Config.GRPC_ENABLED:
                try:
                    # Import proto files dynamically
                    self._setup_proto_path()
                    from reputation_pb2 import RecordEventRequest
                    from reputation_pb2_grpc import ReputationServiceStub

                    channel = await self._grpc_manager.get_channel('reputation')
                    stub = ReputationServiceStub(channel)

                    # Build metadata dict with all event metadata
                    metadata = {
                        'username': event_data.get('username', ''),
                        'channel_id': event_data.get('channel_id', entity_id),
                    }
                    # Add event metadata if present
                    event_metadata = event_data.get('metadata', {})
                    for k, v in event_metadata.items():
                        metadata[k] = str(v)

                    request = RecordEventRequest(
                        token=self._grpc_manager.generate_token(),
                        community_id=community_id,
                        user_id=0,  # Will be resolved by reputation module
                        platform=event_data.get('platform', 'unknown'),
                        platform_user_id=event_data.get('user_id', ''),
                        event_type=event_data.get('message_type', 'chatMessage'),
                        metadata=metadata
                    )

                    await self._grpc_manager.call_with_retry(stub.RecordEvent, request, timeout=5.0)
                    logger.debug(f"Recorded reputation event via gRPC for community {community_id}")
                    return
                except Exception as e:
                    logger.warning(f"gRPC call failed for reputation event, falling back to REST: {e}")

            # Fallback to REST
            if not Config.REPUTATION_API_URL:
                return

            session = await self._get_http_session()
            payload = {
                'community_id': community_id,
                'user_id': None,  # Will be resolved by reputation module
                'platform': event_data.get('platform', 'unknown'),
                'platform_user_id': event_data.get('user_id', ''),
                'event_type': event_data.get('message_type', 'chatMessage'),
                'metadata': {
                    'username': event_data.get('username', ''),
                    'channel_id': event_data.get('channel_id', entity_id),
                    **event_data.get('metadata', {})
                }
            }

            headers = {}
            if Config.SERVICE_API_KEY:
                headers['X-Service-Key'] = Config.SERVICE_API_KEY

            await session.post(
                f"{Config.REPUTATION_API_URL}/api/v1/internal/events",
                json=payload,
                headers=headers,
            )
            logger.debug(f"Recorded reputation event via REST for community {community_id}")
        except Exception as e:
            logger.warning(f"Failed to record reputation event: {e}")

    async def _is_module_enabled(self, module_name: str, community_id: int) -> bool:
        """Check if a module is enabled for a community. Cached in Redis."""
        if not community_id:
            return True  # No community context, allow by default

        # For marketplace modules, check subscription and module status first
        if module_name.startswith('marketplace:'):
            return await self._is_marketplace_module_enabled(module_name, community_id)

        cache_key = f"module_enabled:{community_id}:{module_name}"

        # Check Redis cache first
        try:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached == b'1'
        except Exception as e:
            logger.warning(f"Redis cache check failed: {e}")

        # Query database
        try:
            result = self.dal.executesql(
                """
                SELECT is_enabled FROM module_installations
                WHERE community_id = %s AND module_id = %s
                """,
                [community_id, module_name]
            )

            # Default to enabled if no record exists
            is_enabled = result[0][0] if result and result[0] else True

            # Cache result for 5 minutes
            try:
                await self.cache.set(cache_key, b'1' if is_enabled else b'0', ttl=300)
            except Exception:
                pass

            return is_enabled
        except Exception as e:
            logger.error(f"Failed to check module status: {e}")
            return True  # Default to enabled on error

    async def _is_marketplace_module_enabled(self, module_name: str, community_id: int) -> bool:
        """Check if a marketplace module subscription is active for this community.

        Marketplace modules require:
        1. Community subscription to the marketplace module
        2. Subscription status = 'active'
        3. is_enabled = true
        4. Marketplace module itself must be approved and not deleted
        """
        if not module_name.startswith('marketplace:'):
            return True

        cache_key = f"marketplace_enabled:{community_id}:{module_name}"

        # Check Redis cache first
        try:
            cached = await self.cache.get(cache_key)
            if cached is not None:
                return cached == b'1'
        except Exception as e:
            logger.warning(f"Redis cache check failed for marketplace module: {e}")

        try:
            # Extract module ID from module_name format: 'marketplace:{moduleId}'
            module_id = int(module_name.split(':', 1)[1])
        except (ValueError, IndexError):
            logger.error(f"Invalid marketplace module name format: {module_name}")
            return False

        try:
            # Check marketplace_subscriptions status and module approval status
            result = self.dal.executesql(
                """SELECT ms.status
                   FROM marketplace_subscriptions ms
                   JOIN marketplace_modules mm ON mm.id = ms.module_id
                   WHERE ms.community_id = %s
                   AND ms.module_id = %s
                   AND ms.is_enabled = true
                   AND mm.status = 'approved'
                   AND mm.deleted_at IS NULL""",
                [community_id, module_id]
            )

            is_enabled = result[0][0] == 'active' if result and result[0] else False

            # Cache result for 5 minutes
            try:
                await self.cache.set(
                    cache_key,
                    b'1' if is_enabled else b'0',
                    ttl=300
                )
            except Exception:
                pass

            return is_enabled
        except Exception as e:
            logger.warning(f"Failed to check marketplace module status: {e}")
            return True  # Fail open (don't block commands if check fails)

    async def execute_command(
        self,
        command: str,
        entity_id: str,
        user_id: str,
        message: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """Execute command asynchronously"""
        try:
            # Get community ID for entity
            community_id = await self._get_community_for_entity(entity_id)
            if not community_id:
                return {
                    "success": False,
                    "error": "Community not found for this channel",
                    "session_id": session_id
                }

            # Look up command in registry
            cmd_info = await self.command_registry.get_command(command, community_id)
            if not cmd_info:
                # Command not found
                return {
                    "success": False,
                    "error": f"Unknown command: {command}",
                    "session_id": session_id,
                    "help_url": "/commands"
                }

            # Check if module is enabled for this community
            if not await self._is_module_enabled(cmd_info.module_name, community_id):
                return {
                    "success": False,
                    "error": f"The '{cmd_info.module_name}' module is disabled for this community",
                    "session_id": session_id
                }

            # Check if command is enabled
            if not cmd_info.is_enabled:
                return {
                    "success": False,
                    "error": f"Command {command} is currently disabled",
                    "session_id": session_id
                }

            # Check cooldown
            if cmd_info.cooldown_seconds > 0:
                cooldown_key = f"cooldown:{user_id}:{command}"
                if not await self.rate_limiter.check_rate_limit(
                    cooldown_key,
                    limit=1,
                    window=cmd_info.cooldown_seconds
                ):
                    return {
                        "success": False,
                        "error": f"Command on cooldown. Wait {cmd_info.cooldown_seconds} seconds.",
                        "session_id": session_id
                    }

            # Parse command arguments
            parts = message.split(maxsplit=1)
            args = parts[1] if len(parts) > 1 else ""

            # Build payload for module
            payload = {
                "command": command,
                "args": args,
                "user_id": user_id,
                "entity_id": entity_id,
                "community_id": community_id,
                "session_id": session_id,
                "message": message,
            }

            # Call module HTTP endpoint with retry logic
            module_response = await self._call_module_with_retry(
                cmd_info.module_url,
                payload,
                max_retries=3
            )

            if module_response:
                # Store response for retrieval
                await self.handle_module_response({
                    "session_id": session_id,
                    "response": module_response
                })

                return {
                    "success": True,
                    "session_id": session_id,
                    "command": command,
                    "module": cmd_info.module_name,
                    "response": module_response
                }
            else:
                return {
                    "success": False,
                    "error": "Module did not respond",
                    "session_id": session_id
                }

        except Exception as e:
            logger.error(f"Error executing command {command}: {e}")
            return {
                "success": False,
                "error": str(e),
                "session_id": session_id
            }

    async def _call_module_with_retry(
        self,
        module_url: str,
        payload: Dict[str, Any],
        max_retries: int = 3
    ) -> Optional[Dict[str, Any]]:
        """Call module with exponential backoff retry"""
        session = await self._get_http_session()

        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=Config.ROUTER_REQUEST_TIMEOUT)
                headers = {}
                if Config.SERVICE_API_KEY:
                    headers['X-Service-Key'] = Config.SERVICE_API_KEY

                async with session.post(
                    f"{module_url}/api/v1/execute",
                    json=payload,
                    headers=headers,
                    timeout=timeout
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        # Rate limited, wait and retry
                        wait_time = 2 ** attempt
                        logger.warning(f"Module rate limited, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Module returned status {response.status}")
                        return None

            except asyncio.TimeoutError:
                logger.warning(f"Module timeout (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
            except Exception as e:
                logger.error(f"Module call failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None

        return None

    async def handle_module_response(self, response_data: Dict[str, Any]):
        """Handle response from interaction module"""
        try:
            session_id = response_data.get('session_id')
            response = response_data.get('response', {})

            if not session_id:
                logger.warning("No session_id in module response")
                return

            # Cache response for retrieval (1 hour TTL)
            cache_key = f"response:{session_id}"
            await self.cache.set(
                cache_key,
                json.dumps(response),
                ttl=3600
            )

            # Also store in memory for immediate access
            self._response_cache[session_id] = response

            logger.info(f"Stored module response for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to handle module response: {e}")

    async def get_response(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored response by session ID"""
        # Check memory cache first
        if session_id in self._response_cache:
            return self._response_cache[session_id]

        # Check Redis cache
        cache_key = f"response:{session_id}"
        cached = await self.cache.get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in cached response for {session_id}")

        return None

    async def list_commands(
        self,
        community_id: Optional[int] = None,
        category: Optional[str] = None,
        enabled_only: bool = True
    ) -> List[Dict[str, Any]]:
        """List available commands"""
        try:
            return await self.command_registry.list_commands(
                community_id=community_id,
                category=category,
                enabled_only=enabled_only
            )
        except Exception as e:
            logger.error(f"Failed to list commands: {e}")
            return []

    async def _check_and_trigger_workflows(self, command: str, entity_id: str, user_id: str, message: str, session_id: str, event_data: Dict[str, Any]):
        """Check and trigger workflows for this command/event"""
        try:
            if not Config.WORKFLOW_CORE_URL:
                return

            # Query workflows for this trigger
            workflows = await self._get_workflows_for_trigger(entity_id, command, event_data)

            if not workflows:
                return

            # Trigger workflows in parallel (fire-and-forget)
            for workflow in workflows:
                asyncio.create_task(
                    self._trigger_workflow(workflow, event_data, session_id)
                )
        except Exception as e:
            logger.warning(f"Failed to check workflows: {e}")

    async def _get_workflows_for_trigger(self, entity_id: str, command: str, event_data: Dict[str, Any]) -> List[Dict]:
        """Get active workflows for this trigger"""
        try:
            message_type = event_data.get('message_type', 'chatMessage')

            # Query workflows table for matching triggers
            query = """
                SELECT workflow_id, trigger_config
                FROM workflows
                WHERE entity_id = %s
                AND is_active = true
                AND status = 'published'
                AND (
                    (trigger_type = 'command' AND trigger_config->>'command' = %s)
                    OR (trigger_type = 'event' AND trigger_config->>'event_type' = %s)
                )
            """
            result = self.dal.executesql(query, [entity_id, command, message_type])

            workflows = []
            for row in result:
                workflows.append({
                    'workflow_id': row[0],
                    'trigger_config': row[1]
                })

            return workflows
        except Exception as e:
            logger.error(f"Failed to query workflows: {e}")
            return []

    async def _trigger_workflow(self, workflow: Dict, event_data: Dict[str, Any], session_id: str):
        """Trigger workflow execution"""
        try:
            # Try gRPC first if enabled
            if self._grpc_manager and Config.GRPC_ENABLED:
                try:
                    # Import proto files dynamically
                    self._setup_proto_path()
                    from workflow_pb2 import TriggerWorkflowRequest
                    from workflow_pb2_grpc import WorkflowServiceStub

                    channel = await self._grpc_manager.get_channel('workflow')
                    stub = WorkflowServiceStub(channel)

                    # Serialize trigger_data to JSON string for proto
                    import json
                    trigger_data_json = json.dumps(event_data)

                    request = TriggerWorkflowRequest(
                        token=self._grpc_manager.generate_token(),
                        workflow_id=str(workflow['workflow_id']),
                        trigger_source=event_data.get('message_type', 'command'),
                        trigger_data=trigger_data_json,
                        session_id=session_id,
                        entity_id=event_data.get('entity_id', ''),
                        user_id=0,  # Will be resolved by workflow module if needed
                        platform=event_data.get('platform', 'unknown')
                    )

                    await self._grpc_manager.call_with_retry(stub.TriggerWorkflow, request, timeout=2.0)
                    logger.info(f"Triggered workflow {workflow['workflow_id']} via gRPC for session {session_id}")
                    return
                except Exception as e:
                    logger.warning(f"gRPC call failed for workflow trigger, falling back to REST: {e}")

            # Fallback to REST
            if not Config.WORKFLOW_CORE_URL:
                return

            session = await self._get_http_session()
            payload = {
                'trigger_source': event_data.get('message_type', 'command'),
                'trigger_data': event_data,
                'session_id': session_id,
                'entity_id': event_data.get('entity_id'),
                'user_id': event_data.get('user_id'),
                'platform': event_data.get('platform', 'unknown')
            }

            headers = {}
            if Config.SERVICE_API_KEY:
                headers['X-Service-Key'] = Config.SERVICE_API_KEY

            await session.post(
                f"{Config.WORKFLOW_CORE_URL}/api/v1/workflows/{workflow['workflow_id']}/execute",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=2)
            )
            logger.info(f"Triggered workflow {workflow['workflow_id']} via REST for session {session_id}")
        except Exception as e:
            logger.warning(f"Failed to trigger workflow: {e}")

    async def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        return {
            "requests_processed": 0,
            "avg_response_time_ms": 0,
            "cache_hit_rate": 0
        }

    async def _get_translate_stub(self):
        """Lazy-initialise gRPC channel and stub for translate_interaction_module."""
        if self._translate_stub is None:
            from proto_clients.translate_interaction_pb2_grpc import TranslateInteractionStub
            from flask_core.grpc_tls import secure_channel
            self._translate_channel = secure_channel(Config.TRANSLATE_GRPC_HOST)
            self._translate_stub = TranslateInteractionStub(self._translate_channel)
        return self._translate_stub

    async def _translate_message(
        self,
        event_data: Dict[str, Any],
        entity_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Translate message via translate_interaction_module gRPC (hot-path).

        The translation logic (provider fallback, emote preservation, caching)
        now lives in translate_interaction_module; the router just proxies the call.
        """
        try:
            # Get community ID from entity
            community_id = await self._get_community_for_entity(entity_id)
            if not community_id:
                return None

            # Quick check: is translation enabled for this community?
            config = await self._get_translation_config(community_id)
            if not config or not config.get('enabled', False):
                return None

            from proto_clients import translate_interaction_pb2

            message = event_data.get('message', '')
            target_lang = config.get('default_language', 'en')
            platform = event_data.get('platform', 'unknown')
            channel_id = event_data.get('channel_id', entity_id)

            token = self._grpc_manager.generate_token() if self._grpc_manager else ''

            stub = await self._get_translate_stub()
            req = translate_interaction_pb2.TranslateRequest(
                text=message,
                target_lang=target_lang,
                community_id=str(community_id),
                platform=platform,
                channel_id=str(channel_id),
                token=token,
            )
            resp = await stub.Translate(req, timeout=10)

            if not resp.success:
                logger.warning(f"Translation failed: {resp.error}")
                return None
            if resp.skipped:
                return None
            return {
                'translated_text': resp.translated_text,
                'original_text': resp.original_text,
                'detected_lang': resp.detected_lang,
                'target_lang': resp.target_lang,
                'confidence': resp.confidence,
                'provider': resp.provider,
                'cached': resp.cached,
                'tokens_preserved': resp.tokens_preserved,
            }

        except Exception as e:
            logger.warning(f"Translate gRPC call failed: {e}")
            return None

    async def _check_content_filter(
        self,
        community_id: int,
        message: str,
        platform: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Check translated message against security-core content filter.

        Fail-open: if security-core is unreachable, allow the message through
        so translation isn't broken by a filter outage.

        Returns:
            Filter result dict with 'allowed' key, or None on error.
        """
        try:
            if not Config.SECURITY_CORE_URL:
                return None

            session = await self._get_http_session()
            payload = {
                'community_id': community_id,
                'platform': platform,
                'platform_user_id': user_id,
                'message': message,
            }

            headers = {}
            if Config.SERVICE_API_KEY:
                headers['X-Service-Key'] = Config.SERVICE_API_KEY

            async with session.post(
                f"{Config.SECURITY_CORE_URL}/api/v1/internal/check",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get('data', result)
                else:
                    logger.warning(
                        f"Content filter returned status {response.status}"
                    )
                    return None

        except Exception as e:
            # Fail-open: don't block translations if filter is unavailable
            logger.warning(f"Content filter check failed (fail-open): {e}")
            return None

    async def _get_translation_config(
        self,
        community_id: int
    ) -> Dict[str, Any]:
        """Get translation config from community.config JSONB"""
        cache_key = f"translation:config:{community_id}"
        cached = await self.cache.get(cache_key)
        if cached:
            return json.loads(cached)

        result = self.dal.executesql(
            "SELECT config->'translation' as translation_config FROM communities WHERE id = %s",
            [community_id]
        )

        if result and result[0] and result[0][0]:
            config = result[0][0]
            await self.cache.set(cache_key, json.dumps(config), ttl=600)
            return config

        return {}

    async def _send_caption_event(
        self,
        event_data: Dict[str, Any],
        translation_result: Dict[str, Any],
        entity_id: str
    ):
        """Send caption to browser source overlay via internal API"""
        try:
            community_id = await self._get_community_for_entity(entity_id)
            if not community_id:
                return

            # Try gRPC first if enabled
            if self._grpc_manager and Config.GRPC_ENABLED:
                try:
                    # Import proto files dynamically
                    self._setup_proto_path()
                    from browser_source_pb2 import SendCaptionRequest
                    from browser_source_pb2_grpc import BrowserSourceServiceStub

                    channel = await self._grpc_manager.get_channel('browser_source')
                    stub = BrowserSourceServiceStub(channel)

                    request = SendCaptionRequest(
                        token=self._grpc_manager.generate_token(),
                        community_id=community_id,
                        platform=event_data.get('platform', ''),
                        username=event_data.get('username', ''),
                        original_message=event_data.get('metadata', {}).get('original_message', event_data.get('message', '')),
                        translated_message=translation_result.get('translated_text', ''),
                        detected_language=translation_result.get('detected_lang', ''),
                        target_language=translation_result.get('target_lang', ''),
                        confidence=translation_result.get('confidence', 0.0)
                    )

                    await self._grpc_manager.call_with_retry(stub.SendCaption, request, timeout=2.0)
                    logger.debug(f"Sent caption via gRPC for community {community_id}")
                    return
                except Exception as e:
                    logger.warning(f"gRPC call failed for caption event, falling back to REST: {e}")

            # Fallback to REST
            if not hasattr(Config, 'BROWSER_SOURCE_URL') or not Config.BROWSER_SOURCE_URL:
                return

            session = await self._get_http_session()

            payload = {
                'community_id': community_id,
                'platform': event_data.get('platform', ''),
                'username': event_data.get('username', ''),
                'original_message': event_data.get('metadata', {}).get('original_message', event_data.get('message', '')),
                'translated_message': translation_result.get('translated_text'),
                'detected_language': translation_result.get('detected_lang'),
                'target_language': translation_result.get('target_lang'),
                'confidence': translation_result.get('confidence', 0.0)
            }

            headers = {}
            if hasattr(Config, 'SERVICE_API_KEY') and Config.SERVICE_API_KEY:
                headers['X-Service-Key'] = Config.SERVICE_API_KEY

            await session.post(
                f"{Config.BROWSER_SOURCE_URL}/api/v1/internal/captions",
                json=payload,
                headers=headers,
                timeout=2
            )
            logger.debug(f"Sent caption via REST for community {community_id}")
        except Exception as e:
            logger.warning(f"Failed to send caption event: {e}")

    async def publish_to_stream(self, stream_name: str, event_data: dict) -> bool:
        """
        Publish event to Redis stream if stream pipeline is available.

        Args:
            stream_name: Name of the stream (e.g., STREAM_COMMANDS, STREAM_ACTIONS)
            event_data: Event data to publish

        Returns:
            True if published successfully, False otherwise
        """
        if not Config.STREAM_PIPELINE_ENABLED or not self.stream_pipeline:
            return False

        try:
            # Serialize event data to JSON
            event_json = json.dumps(event_data)

            # Publish to stream
            message_id = await self.stream_pipeline.publish(stream_name, event_json)

            logger.debug(f"Published event to stream {stream_name} with message ID {message_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to publish to stream {stream_name}: {e}")
            return False

    async def process_stream_event(self, event: dict) -> Dict[str, Any]:
        """
        Process event from stream (stream mode).

        Similar to process_event but designed for stream consumption.

        Args:
            event: Event data from stream

        Returns:
            Processing result
        """
        try:
            # Handle the event through standard process_event
            result = await self.process_event(event)

            # If stream mode is enabled and event was successfully processed,
            # publish result to actions stream
            if result.get('success') and Config.STREAM_PIPELINE_ENABLED:
                await self.publish_to_stream(Config.STREAM_ACTIONS, result)

            return result
        except Exception as e:
            logger.error(f"Failed to process stream event: {e}")
            return {"success": False, "error": str(e)}

    async def _maybe_ai_chatter_respond(self, event_data: dict) -> None:
        """
        Fire-and-forget: check if community has AIChatter enabled,
        and if so, POST to ai_interaction_module proactive-chat endpoint.
        Never raises — logs failures silently.
        """
        try:
            community_id = event_data.get('community_id')
            if not community_id:
                return

            # Check config cache (fast Redis lookup)
            if not self.ai_chatter_config_cache:
                return

            config = await self.ai_chatter_config_cache.get(int(community_id))
            if not config or not config.get('enabled', False):
                return

            # POST to ai_interaction_module proactive-chat endpoint
            ai_interaction_url = self._get_ai_interaction_url()
            if not ai_interaction_url:
                return

            payload = {
                'session_id': event_data.get('session_id', ''),
                'community_id': community_id,
                'user_id': event_data.get('user_id', ''),
                'platform_user_id': event_data.get('platform_user_id', ''),
                'message': event_data.get('message_content', event_data.get('message', '')),
                'platform': event_data.get('platform', 'unknown'),
                'channel_id': event_data.get('entity_id', event_data.get('channel_id', '')),
            }

            session = await self._get_http_session()
            async with session.post(
                f"{ai_interaction_url}/api/v1/ai/proactive-chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status not in (200, 202):
                    logger.warning(
                        f"Proactive chat returned {resp.status}",
                        extra={'community_id': community_id}
                    )

        except Exception as e:
            # Fire-and-forget: log and swallow
            logger.warning(f"AIChatter proactive response failed (non-critical): {e}")

    def _get_ai_interaction_url(self) -> str:
        """Get the ai_interaction_module base URL from config."""
        import os
        return os.getenv('AI_INTERACTION_API_URL', 'http://ai-interaction:8005')
