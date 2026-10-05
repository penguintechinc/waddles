"""
Weight Manager - Manages per-community reputation weights
Customization is a PREMIUM feature; non-premium communities use defaults
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

from config import Config


@dataclass
class CommunityWeights:
    """Weight configuration for a community."""
    community_id: int
    is_premium: bool = False
    # Activity weights -- whole, positive integers (gh-310): community_members
    # .reputation / reputation_tenant.score are INTEGER columns with no
    # remainder-tracking column, so a magnitude < 0.5 per-event weight never
    # moves an integer-scored table across repeated events (see
    # ReputationService._clamp_score's docstring for the full rule). Farming
    # via rapid chat/command spam is bounded by svc-process's own per-user
    # cooldowns (60s/10s), not by using a fractional weight here.
    chat_message: float = 1.0
    command_usage: float = 1.0
    giveaway_entry: float = -1.0  # Larger penalty to dissuade giveaway bots
    follow: float = 1.0
    subscription: float = 5.0
    subscription_tier2: float = 10.0
    subscription_tier3: float = 20.0
    gift_subscription: float = 3.0
    donation_per_dollar: float = 1.0
    cheer_per_100bits: float = 1.0
    raid: float = 2.0
    boost: float = 5.0
    # Moderation weights
    warn: float = -25.0
    timeout: float = -50.0
    kick: float = -75.0
    ban: float = -200.0
    # Policy settings
    auto_ban_enabled: bool = False
    auto_ban_threshold: int = 450
    starting_score: int = 600
    min_score: int = 300
    max_score: int = 850

    def get_weight(self, event_type: str) -> float:
        """Get weight for an event type."""
        weight_map = {
            'chatMessage': self.chat_message,
            'chat_message': self.chat_message,
            'slashCommand': self.command_usage,
            'command': self.command_usage,
            'command_usage': self.command_usage,
            'giveaway': self.giveaway_entry,
            'giveaway_entry': self.giveaway_entry,
            'follow': self.follow,
            'subscription': self.subscription,
            'subscription_tier2': self.subscription_tier2,
            'subscription_tier3': self.subscription_tier3,
            'gift_subscription': self.gift_subscription,
            'donation': self.donation_per_dollar,
            'cheer': self.cheer_per_100bits,
            'raid': self.raid,
            'boost': self.boost,
            'warn': self.warn,
            'timeout': self.timeout,
            'kick': self.kick,
            'ban': self.ban,
        }
        return weight_map.get(event_type, 0.0)


@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    weights: CommunityWeights
    expires_at: float


class WeightManager:
    """
    Manages reputation weight configurations for communities.

    - Non-premium communities always use default weights
    - Premium communities can customize weights
    - Weights are cached with configurable TTL
    """

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger
        self._cache: Dict[int, CacheEntry] = {}
        self._default_weights = self._build_default_weights()

    def _build_default_weights(self) -> CommunityWeights:
        """Build default weights from config."""
        defaults = Config.DEFAULT_WEIGHTS
        return CommunityWeights(
            community_id=0,
            is_premium=False,
            chat_message=defaults.get('chat_message', 1.0),
            command_usage=defaults.get('command_usage', 1.0),
            giveaway_entry=defaults.get('giveaway_entry', -1.0),
            follow=defaults.get('follow', 1.0),
            subscription=defaults.get('subscription', 5.0),
            subscription_tier2=defaults.get('subscription_tier2', 10.0),
            subscription_tier3=defaults.get('subscription_tier3', 20.0),
            gift_subscription=defaults.get('gift_subscription', 3.0),
            donation_per_dollar=defaults.get('donation_per_dollar', 1.0),
            cheer_per_100bits=defaults.get('cheer_per_100bits', 1.0),
            raid=defaults.get('raid', 2.0),
            boost=defaults.get('boost', 5.0),
            warn=defaults.get('warn', -25.0),
            timeout=defaults.get('timeout', -50.0),
            kick=defaults.get('kick', -75.0),
            ban=defaults.get('ban', -200.0),
            auto_ban_enabled=False,
            auto_ban_threshold=Config.REPUTATION_AUTO_BAN_THRESHOLD,
            starting_score=Config.REPUTATION_DEFAULT,
            min_score=Config.REPUTATION_MIN,
            max_score=Config.REPUTATION_MAX,
        )

    async def get_weights(self, community_id: int) -> CommunityWeights:
        """
        Get weight configuration for a community.

        Non-premium communities always return default weights.
        Premium communities return their custom weights.
        """
        # Check cache first
        cached = self._cache.get(community_id)
        if cached and cached.expires_at > time.time():
            return cached.weights

        # Query database for community config
        try:
            result = self.dal.executesql(
                """SELECT is_premium, chat_message, command_usage, giveaway_entry,
                          follow, subscription, subscription_tier2, subscription_tier3,
                          gift_subscription, donation_per_dollar, cheer_per_100bits,
                          raid, boost, warn, timeout, kick, ban,
                          auto_ban_enabled, auto_ban_threshold, starting_score,
                          min_score, max_score
                   FROM community_reputation_config
                   WHERE community_id = %s""",
                [community_id]
            )

            if result and len(result) > 0:
                row = result[0]
                is_premium = row[0]

                # Non-premium: always use defaults (but keep policy settings)
                if not is_premium:
                    weights = CommunityWeights(
                        community_id=community_id,
                        is_premium=False,
                        # Use defaults for weights
                        chat_message=self._default_weights.chat_message,
                        command_usage=self._default_weights.command_usage,
                        giveaway_entry=self._default_weights.giveaway_entry,
                        follow=self._default_weights.follow,
                        subscription=self._default_weights.subscription,
                        subscription_tier2=self._default_weights.subscription_tier2,
                        subscription_tier3=self._default_weights.subscription_tier3,
                        gift_subscription=self._default_weights.gift_subscription,
                        donation_per_dollar=self._default_weights.donation_per_dollar,
                        cheer_per_100bits=self._default_weights.cheer_per_100bits,
                        raid=self._default_weights.raid,
                        boost=self._default_weights.boost,
                        warn=self._default_weights.warn,
                        timeout=self._default_weights.timeout,
                        kick=self._default_weights.kick,
                        ban=self._default_weights.ban,
                        # But use community's policy settings
                        auto_ban_enabled=row[17] if row[17] is not None else False,
                        auto_ban_threshold=row[18] if row[18] else 450,
                        starting_score=row[19] if row[19] else 600,
                        min_score=row[20] if row[20] else 300,
                        max_score=row[21] if row[21] else 850,
                    )
                else:
                    # Premium: use custom weights
                    weights = CommunityWeights(
                        community_id=community_id,
                        is_premium=True,
                        chat_message=float(row[1]) if row[1] else 1.0,
                        command_usage=float(row[2]) if row[2] else 1.0,
                        giveaway_entry=float(row[3]) if row[3] else -1.0,
                        follow=float(row[4]) if row[4] else 1.0,
                        subscription=float(row[5]) if row[5] else 5.0,
                        subscription_tier2=float(row[6]) if row[6] else 10.0,
                        subscription_tier3=float(row[7]) if row[7] else 20.0,
                        gift_subscription=float(row[8]) if row[8] else 3.0,
                        donation_per_dollar=float(row[9]) if row[9] else 1.0,
                        cheer_per_100bits=float(row[10]) if row[10] else 1.0,
                        raid=float(row[11]) if row[11] else 2.0,
                        boost=float(row[12]) if row[12] else 5.0,
                        warn=float(row[13]) if row[13] else -25.0,
                        timeout=float(row[14]) if row[14] else -50.0,
                        kick=float(row[15]) if row[15] else -75.0,
                        ban=float(row[16]) if row[16] else -200.0,
                        auto_ban_enabled=row[17] if row[17] is not None else False,
                        auto_ban_threshold=row[18] if row[18] else 450,
                        starting_score=row[19] if row[19] else 600,
                        min_score=row[20] if row[20] else 300,
                        max_score=row[21] if row[21] else 850,
                    )
            else:
                # No config exists - use defaults
                weights = CommunityWeights(
                    community_id=community_id,
                    is_premium=False,
                    chat_message=self._default_weights.chat_message,
                    command_usage=self._default_weights.command_usage,
                    giveaway_entry=self._default_weights.giveaway_entry,
                    follow=self._default_weights.follow,
                    subscription=self._default_weights.subscription,
                    subscription_tier2=self._default_weights.subscription_tier2,
                    subscription_tier3=self._default_weights.subscription_tier3,
                    gift_subscription=self._default_weights.gift_subscription,
                    donation_per_dollar=self._default_weights.donation_per_dollar,
                    cheer_per_100bits=self._default_weights.cheer_per_100bits,
                    raid=self._default_weights.raid,
                    boost=self._default_weights.boost,
                    warn=self._default_weights.warn,
                    timeout=self._default_weights.timeout,
                    kick=self._default_weights.kick,
                    ban=self._default_weights.ban,
                )

            # Cache the result
            self._cache[community_id] = CacheEntry(
                weights=weights,
                expires_at=time.time() + Config.WEIGHT_CACHE_TTL
            )
            return weights

        except Exception as e:
            self.logger.error(f"Failed to get weights for community {community_id}: {e}")
            return self._default_weights

    async def update_weights(
        self,
        community_id: int,
        weights: Dict[str, Any],
        admin_id: int
    ) -> bool:
        """
        Update weight configuration for a community (PREMIUM only).

        Returns True if successful, False otherwise.
        """
        try:
            # Check if community is premium
            result = self.dal.executesql(
                "SELECT is_premium FROM community_reputation_config WHERE community_id = %s",
                [community_id]
            )

            if not result or not result[0][0]:
                self.logger.warning(
                    f"Attempt to update weights for non-premium community {community_id}"
                )
                return False

            # Build update query
            update_fields = []
            values = []
            for field_name in [
                'chat_message', 'command_usage', 'giveaway_entry', 'follow',
                'subscription', 'subscription_tier2', 'subscription_tier3',
                'gift_subscription', 'donation_per_dollar', 'cheer_per_100bits',
                'raid', 'boost', 'warn', 'timeout', 'kick', 'ban',
                'auto_ban_enabled', 'auto_ban_threshold', 'starting_score'
            ]:
                if field_name in weights:
                    update_fields.append(f"{field_name} = %s")
                    values.append(weights[field_name])

            if not update_fields:
                return True  # Nothing to update

            update_fields.append("updated_at = NOW()")
            values.append(community_id)

            self.dal.executesql(
                f"""UPDATE community_reputation_config
                    SET {', '.join(update_fields)}
                    WHERE community_id = %s""",
                values
            )
            self.dal.commit()

            # Invalidate cache
            self.invalidate_cache(community_id)

            self.logger.audit(
                "Reputation weights updated",
                community_id=community_id,
                admin_id=admin_id,
                updated_fields=list(weights.keys())
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to update weights: {e}")
            return False

    async def create_config(self, community_id: int, is_premium: bool = False) -> bool:
        """Create a new reputation config for a community."""
        try:
            self.dal.executesql(
                """INSERT INTO community_reputation_config (community_id, is_premium)
                   VALUES (%s, %s)
                   ON CONFLICT (community_id) DO NOTHING""",
                [community_id, is_premium]
            )
            self.dal.commit()
            return True
        except Exception as e:
            self.logger.error(f"Failed to create config for community {community_id}: {e}")
            return False

    async def set_premium(self, community_id: int, is_premium: bool) -> bool:
        """Set premium status for a community."""
        try:
            self.dal.executesql(
                """UPDATE community_reputation_config
                   SET is_premium = %s, updated_at = NOW()
                   WHERE community_id = %s""",
                [is_premium, community_id]
            )
            self.dal.commit()
            self.invalidate_cache(community_id)
            return True
        except Exception as e:
            self.logger.error(f"Failed to set premium for community {community_id}: {e}")
            return False

    def invalidate_cache(self, community_id: int) -> None:
        """Remove cached weights for a community."""
        if community_id in self._cache:
            del self._cache[community_id]

    def get_default_weights(self) -> CommunityWeights:
        """Return default weights configuration."""
        return self._default_weights
