"""
Event Processor - Handles incoming events from the router and processes reputation changes.
Maps platform events to reputation adjustments.
"""
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from enum import Enum


class EventCategory(str, Enum):
    """Categories of events for reputation processing."""
    CHAT = 'chat'
    ENGAGEMENT = 'engagement'
    MONETIZATION = 'monetization'
    MODERATION = 'moderation'
    COMMAND = 'command'


@dataclass
class ProcessResult:
    """Result of processing a single event."""
    success: bool
    community_id: int
    user_id: Optional[int]
    event_type: str
    score_change: float = 0.0
    score_before: int = 0
    score_after: int = 0
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


@dataclass
class BatchResult:
    """Result of batch event processing."""
    total: int
    processed: int
    skipped: int
    failed: int
    results: List[ProcessResult]


class EventProcessor:
    """
    Processes events from the router and applies reputation changes.

    Maps platform-specific events to standardized reputation event types.
    Handles batch processing for efficiency.
    """

    # Event type mapping from platform events to reputation events
    EVENT_TYPE_MAP = {
        # Chat events
        'chatMessage': 'chat_message',
        'chat_message': 'chat_message',
        'message': 'chat_message',

        # Command events
        'slashCommand': 'command_usage',
        'slash_command': 'command_usage',
        'prefixCommand': 'command_usage',
        'prefix_command': 'command_usage',
        'command': 'command_usage',

        # Giveaway events (heavier penalty)
        'giveaway': 'giveaway_entry',
        'giveaway_entry': 'giveaway_entry',
        'raffle': 'giveaway_entry',
        'raffle_entry': 'giveaway_entry',

        # Engagement events
        'follow': 'follow',
        'raid': 'raid',
        'raid_incoming': 'raid',
        'host': 'raid',  # Treat hosts like raids

        # Subscription events
        'subscription': 'subscription',
        'sub': 'subscription',
        'subscribe': 'subscription',
        'subscription_tier2': 'subscription_tier2',
        'subscription_tier3': 'subscription_tier3',
        'gift_subscription': 'gift_subscription',
        'gift_sub': 'gift_subscription',
        'subgift': 'gift_subscription',

        # Monetization events
        'donation': 'donation',
        'tip': 'donation',
        'cheer': 'cheer',
        'bits': 'cheer',

        # Discord/Slack specific
        'boost': 'boost',
        'server_boost': 'boost',
        'nitro_boost': 'boost',

        # Moderation events (negative impact)
        'warn': 'warn',
        'warning': 'warn',
        'timeout': 'timeout',
        'mute': 'timeout',
        'kick': 'kick',
        'ban': 'ban',
        'permaban': 'ban',
    }

    # Events that require amount multiplier
    SCALED_EVENTS = {
        'donation': 'amount',  # Dollars
        'cheer': 'bits',       # Per 100 bits
        'gift_subscription': 'count',  # Number of gifts
    }

    def __init__(self, reputation_service, weight_manager, policy_enforcer, logger):
        self.reputation_service = reputation_service
        self.weight_manager = weight_manager
        self.policy_enforcer = policy_enforcer
        self.logger = logger
        self._processing_lock = asyncio.Lock()

    def _map_event_type(self, raw_type: str) -> str:
        """Map raw event type to standardized reputation event type."""
        return self.EVENT_TYPE_MAP.get(raw_type.lower(), raw_type.lower())

    def _get_subscription_tier(self, event_data: Dict[str, Any]) -> str:
        """Determine subscription tier from event data."""
        tier = event_data.get('tier', 1)
        metadata = event_data.get('metadata', {})

        # Check various tier indicators
        if tier == 3 or metadata.get('tier') == 3 or metadata.get('tier') == 'tier3':
            return 'subscription_tier3'
        elif tier == 2 or metadata.get('tier') == 2 or metadata.get('tier') == 'tier2':
            return 'subscription_tier2'
        return 'subscription'

    def _calculate_multiplier(self, event_type: str, event_data: Dict[str, Any]) -> float:
        """Calculate multiplier for scaled events (donations, bits, etc.)."""
        if event_type not in self.SCALED_EVENTS:
            return 1.0

        field = self.SCALED_EVENTS[event_type]
        metadata = event_data.get('metadata', {})
        raw_value = event_data.get(field) or metadata.get(field, 0)

        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            return 1.0

        # Apply scaling
        if event_type == 'cheer':
            # Bits: 1 point per 100 bits
            return value / 100.0
        elif event_type == 'donation':
            # Dollars: 1 point per dollar
            return value
        elif event_type == 'gift_subscription':
            # Per gift
            return max(1.0, value)

        return 1.0

    async def process_event(self, event_data: Dict[str, Any]) -> ProcessResult:
        """
        Process a single event for reputation adjustment.

        Expected event_data format:
        {
            'community_id': int,
            'user_id': int (optional - hub_user_id if linked),
            'platform': str,
            'platform_user_id': str,
            'event_type': str,
            'metadata': dict (optional)
        }
        """
        community_id = event_data.get('community_id')
        user_id = event_data.get('user_id')
        platform = event_data.get('platform', 'unknown')
        platform_user_id = event_data.get('platform_user_id', '')
        raw_event_type = event_data.get('event_type', '')
        metadata = event_data.get('metadata', {})

        # Validate required fields
        if not community_id:
            return ProcessResult(
                success=False,
                community_id=0,
                user_id=user_id,
                event_type=raw_event_type,
                error="Missing community_id"
            )

        if not platform_user_id:
            return ProcessResult(
                success=False,
                community_id=community_id,
                user_id=user_id,
                event_type=raw_event_type,
                error="Missing platform_user_id"
            )

        # Map event type
        event_type = self._map_event_type(raw_event_type)

        # Handle subscription tier detection
        if event_type == 'subscription':
            event_type = self._get_subscription_tier(event_data)

        # Calculate multiplier for scaled events
        multiplier = self._calculate_multiplier(event_type, event_data)

        # Build reason string
        reason = metadata.get('reason') or f"{platform} {raw_event_type}"

        try:
            # Apply the reputation adjustment
            result = await self.reputation_service.adjust(
                community_id=community_id,
                user_id=user_id,
                event_type=event_type,
                platform=platform,
                platform_user_id=platform_user_id,
                metadata=metadata,
                reason=reason,
                amount_multiplier=multiplier
            )

            if result.success:
                # Check for policy enforcement (auto-ban, etc.)
                if self.policy_enforcer:
                    await self.policy_enforcer.check_thresholds(
                        community_id=community_id,
                        user_id=user_id,
                        platform=platform,
                        platform_user_id=platform_user_id,
                        current_score=result.score_after
                    )

                return ProcessResult(
                    success=True,
                    community_id=community_id,
                    user_id=user_id,
                    event_type=event_type,
                    score_change=result.score_change,
                    score_before=result.score_before,
                    score_after=result.score_after
                )
            else:
                return ProcessResult(
                    success=False,
                    community_id=community_id,
                    user_id=user_id,
                    event_type=event_type,
                    error=result.error
                )

        except Exception as e:
            self.logger.error(f"Event processing error: {e}")
            return ProcessResult(
                success=False,
                community_id=community_id,
                user_id=user_id,
                event_type=event_type,
                error=str(e)
            )

    async def process_batch(self, events: List[Dict[str, Any]]) -> BatchResult:
        """
        Process a batch of events for reputation adjustments.

        Processes events concurrently for efficiency.
        """
        if not events:
            return BatchResult(
                total=0,
                processed=0,
                skipped=0,
                failed=0,
                results=[]
            )

        # Process events concurrently (with limit to prevent overwhelming DB)
        semaphore = asyncio.Semaphore(10)

        async def process_with_limit(event: Dict[str, Any]) -> ProcessResult:
            async with semaphore:
                return await self.process_event(event)

        results = await asyncio.gather(
            *[process_with_limit(event) for event in events],
            return_exceptions=True
        )

        # Count results
        processed = 0
        skipped = 0
        failed = 0
        final_results = []

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failed += 1
                final_results.append(ProcessResult(
                    success=False,
                    community_id=events[i].get('community_id', 0),
                    user_id=events[i].get('user_id'),
                    event_type=events[i].get('event_type', ''),
                    error=str(result)
                ))
            elif result.skipped:
                skipped += 1
                final_results.append(result)
            elif result.success:
                processed += 1
                final_results.append(result)
            else:
                failed += 1
                final_results.append(result)

        return BatchResult(
            total=len(events),
            processed=processed,
            skipped=skipped,
            failed=failed,
            results=final_results
        )

    async def process_moderation_action(
        self,
        community_id: int,
        moderator_id: int,
        target_user_id: int,
        action: str,
        platform: str,
        platform_user_id: str,
        reason: Optional[str] = None,
        duration: Optional[int] = None
    ) -> ProcessResult:
        """
        Process a moderation action (warn, timeout, kick, ban).

        These are special events that can have significant negative impact.
        """
        metadata = {
            'moderator_id': moderator_id,
            'duration': duration,
            'mod_reason': reason
        }

        event_data = {
            'community_id': community_id,
            'user_id': target_user_id,
            'platform': platform,
            'platform_user_id': platform_user_id,
            'event_type': action,
            'metadata': metadata
        }

        result = await self.process_event(event_data)

        # Log moderation action
        self.logger.audit(
            action="process_moderation_action",
            user=str(moderator_id),
            community=str(community_id),
            result="SUCCESS" if result.success else "FAILED",
            detail=f"Moderation action processed: {action}",
            target_user_id=target_user_id,
            platform=platform,
            score_change=result.score_change if result.success else 0
        )

        return result

    def get_supported_events(self) -> Dict[str, str]:
        """Return mapping of supported event types."""
        return self.EVENT_TYPE_MAP.copy()

    def is_moderation_event(self, event_type: str) -> bool:
        """Check if event type is a moderation action."""
        mod_events = {'warn', 'warning', 'timeout', 'mute', 'kick', 'ban', 'permaban'}
        return event_type.lower() in mod_events
