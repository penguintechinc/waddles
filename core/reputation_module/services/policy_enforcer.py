"""
Policy Enforcer - Handles automatic actions based on reputation thresholds.
Implements auto-ban and other policy enforcement features.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any
from enum import Enum
import aiohttp

from config import Config


class PolicyAction(str, Enum):
    """Types of policy enforcement actions."""
    NONE = 'none'
    WARN = 'warn'
    AUTO_BAN = 'auto_ban'
    NOTIFY_MODS = 'notify_mods'


@dataclass
class PolicyResult:
    """Result of policy enforcement check."""
    action_taken: PolicyAction
    success: bool
    community_id: int
    user_id: Optional[int]
    score: int
    threshold: int
    message: Optional[str] = None
    error: Optional[str] = None


class PolicyEnforcer:
    """
    Enforces reputation-based policies for communities.

    Features:
    - Auto-ban when score drops below threshold
    - Notification to moderators on significant score drops
    - Configurable per-community thresholds
    """

    def __init__(self, weight_manager, dal, logger):
        self.weight_manager = weight_manager
        self.dal = dal
        self.logger = logger
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session for API calls."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._http_session

    async def close(self):
        """Clean up resources."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def check_thresholds(
        self,
        community_id: int,
        user_id: Optional[int],
        platform: str,
        platform_user_id: str,
        current_score: int
    ) -> PolicyResult:
        """
        Check if current score triggers any policy actions.

        Returns the action taken (if any).
        """
        try:
            # Get community weight/policy config
            weights = await self.weight_manager.get_weights(community_id)

            # Check auto-ban threshold
            if weights.auto_ban_enabled:
                if current_score <= weights.auto_ban_threshold:
                    return await self._execute_auto_ban(
                        community_id=community_id,
                        user_id=user_id,
                        platform=platform,
                        platform_user_id=platform_user_id,
                        current_score=current_score,
                        threshold=weights.auto_ban_threshold
                    )

            # Check for warning threshold (score in "poor" territory)
            if current_score <= 450 and current_score > weights.auto_ban_threshold:
                # Notify but don't auto-ban
                return await self._notify_low_score(
                    community_id=community_id,
                    user_id=user_id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                    current_score=current_score
                )

            return PolicyResult(
                action_taken=PolicyAction.NONE,
                success=True,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=weights.auto_ban_threshold
            )

        except Exception as e:
            self.logger.error(f"Policy check failed: {e}")
            return PolicyResult(
                action_taken=PolicyAction.NONE,
                success=False,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=0,
                error=str(e)
            )

    async def _execute_auto_ban(
        self,
        community_id: int,
        user_id: Optional[int],
        platform: str,
        platform_user_id: str,
        current_score: int,
        threshold: int
    ) -> PolicyResult:
        """Execute automatic ban for user who fell below threshold."""
        try:
            # Record the auto-ban action
            self.dal.executesql(
                """INSERT INTO reputation_events
                   (community_id, hub_user_id, platform, platform_user_id,
                    event_type, score_change, score_before, score_after,
                    reason, metadata)
                   VALUES (%s, %s, %s, %s, 'auto_ban', 0, %s, %s, %s, %s)""",
                [community_id, user_id, platform, platform_user_id,
                 current_score, current_score,
                 f"Auto-ban: Score {current_score} below threshold {threshold}",
                 '{"action": "auto_ban", "triggered_by": "policy_enforcer"}']
            )

            # Update member status to banned
            self.dal.executesql(
                """UPDATE community_members
                   SET is_active = false, role = 'banned', updated_at = NOW()
                   WHERE community_id = %s AND hub_user_id = %s""",
                [community_id, user_id]
            )

            self.dal.commit()

            # Send ban request to appropriate platform module
            await self._send_platform_ban(
                community_id=community_id,
                platform=platform,
                platform_user_id=platform_user_id,
                reason=f"Reputation auto-ban: Score dropped to {current_score}"
            )

            self.logger.audit(
                action="auto_ban",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                detail="Auto-ban executed",
                user_id=user_id,
                platform=platform,
                platform_user_id=platform_user_id,
                score=current_score,
                threshold=threshold
            )

            return PolicyResult(
                action_taken=PolicyAction.AUTO_BAN,
                success=True,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=threshold,
                message=f"User auto-banned: score {current_score} < threshold {threshold}"
            )

        except Exception as e:
            self.logger.error(f"Auto-ban execution failed: {e}")
            return PolicyResult(
                action_taken=PolicyAction.AUTO_BAN,
                success=False,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=threshold,
                error=str(e)
            )

    async def _notify_low_score(
        self,
        community_id: int,
        user_id: Optional[int],
        platform: str,
        platform_user_id: str,
        current_score: int
    ) -> PolicyResult:
        """Notify moderators about a user with dangerously low score."""
        try:
            # Check if we recently notified about this user (prevent spam)
            recent_check = self.dal.executesql(
                """SELECT id FROM reputation_events
                   WHERE community_id = %s AND hub_user_id = %s
                   AND event_type = 'low_score_warning'
                   AND created_at > NOW() - INTERVAL '24 hours'""",
                [community_id, user_id]
            )

            if recent_check and len(recent_check) > 0:
                # Already notified recently
                return PolicyResult(
                    action_taken=PolicyAction.NONE,
                    success=True,
                    community_id=community_id,
                    user_id=user_id,
                    score=current_score,
                    threshold=0,
                    message="Low score notification already sent in last 24h"
                )

            # Record the warning
            self.dal.executesql(
                """INSERT INTO reputation_events
                   (community_id, hub_user_id, platform, platform_user_id,
                    event_type, score_change, score_before, score_after, reason)
                   VALUES (%s, %s, %s, %s, 'low_score_warning', 0, %s, %s, %s)""",
                [community_id, user_id, platform, platform_user_id,
                 current_score, current_score,
                 f"Low score warning: {current_score}"]
            )
            self.dal.commit()

            self.logger.audit(
                action="low_score_warning",
                user="system",
                community=str(community_id),
                result="WARNED",
                detail="Low score warning",
                user_id=user_id,
                score=current_score
            )

            return PolicyResult(
                action_taken=PolicyAction.NOTIFY_MODS,
                success=True,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=450,
                message=f"Low score warning recorded: {current_score}"
            )

        except Exception as e:
            self.logger.error(f"Low score notification failed: {e}")
            return PolicyResult(
                action_taken=PolicyAction.NONE,
                success=False,
                community_id=community_id,
                user_id=user_id,
                score=current_score,
                threshold=0,
                error=str(e)
            )

    async def _send_platform_ban(
        self,
        community_id: int,
        platform: str,
        platform_user_id: str,
        reason: str
    ) -> bool:
        """
        Send ban request to the appropriate platform action module.

        Routes through the router to reach platform-specific modules.
        """
        if not Config.ROUTER_API_URL:
            self.logger.warning("ROUTER_API_URL not configured, skipping platform ban")
            return False

        try:
            # Get server info for this community and platform
            server_result = self.dal.executesql(
                """SELECT cs.platform_server_id
                   FROM community_servers cs
                   WHERE cs.community_id = %s
                     AND cs.platform = %s
                     AND cs.status = 'approved'
                   LIMIT 1""",
                [community_id, platform]
            )

            if not server_result:
                self.logger.warning(
                    f"No server found for community {community_id} platform {platform}"
                )
                return False

            server_id = server_result[0][0]

            session = await self._get_http_session()

            # Send moderation action to router
            action_data = {
                'action': 'ban',
                'platform': platform,
                'server_id': server_id,
                'target_user_id': platform_user_id,
                'reason': reason,
                'source': 'reputation_auto_ban'
            }

            headers = {}
            if Config.SERVICE_API_KEY:
                headers['X-Service-Key'] = Config.SERVICE_API_KEY

            async with session.post(
                f"{Config.ROUTER_API_URL}/moderation/action",
                json=action_data,
                headers=headers
            ) as response:
                if response.status == 200:
                    self.logger.info(
                        f"Platform ban sent for {platform_user_id} on {platform}"
                    )
                    return True
                else:
                    body = await response.text()
                    self.logger.warning(
                        f"Platform ban failed: {response.status} - {body}"
                    )
                    return False

        except Exception as e:
            self.logger.error(f"Failed to send platform ban: {e}")
            return False

    async def get_at_risk_users(
        self,
        community_id: int,
        threshold_buffer: int = 50
    ) -> list:
        """
        Get users who are close to the auto-ban threshold.

        Returns users whose score is within threshold_buffer of the auto-ban point.
        Useful for moderator dashboards.
        """
        try:
            weights = await self.weight_manager.get_weights(community_id)
            warning_threshold = weights.auto_ban_threshold + threshold_buffer

            result = self.dal.executesql(
                """SELECT cm.hub_user_id, hu.username, cm.reputation,
                          %s - cm.reputation as points_until_ban
                   FROM community_members cm
                   JOIN hub_users hu ON hu.id = cm.hub_user_id
                   WHERE cm.community_id = %s
                   AND cm.is_active = true
                   AND cm.reputation <= %s
                   ORDER BY cm.reputation ASC
                   LIMIT 50""",
                [weights.auto_ban_threshold, community_id, warning_threshold]
            )

            at_risk = []
            for row in result:
                at_risk.append({
                    'user_id': row[0],
                    'username': row[1],
                    'score': row[2],
                    'points_until_ban': row[3],
                    'threshold': weights.auto_ban_threshold
                })

            return at_risk

        except Exception as e:
            self.logger.error(f"Failed to get at-risk users: {e}")
            return []

    async def disable_auto_ban(self, community_id: int, admin_id: int) -> bool:
        """Disable auto-ban for a community."""
        try:
            self.dal.executesql(
                """UPDATE community_reputation_config
                   SET auto_ban_enabled = false, updated_at = NOW()
                   WHERE community_id = %s""",
                [community_id]
            )
            self.dal.commit()
            self.weight_manager.invalidate_cache(community_id)

            self.logger.audit(
                action="disable_auto_ban",
                user=str(admin_id),
                community=str(community_id),
                result="SUCCESS",
                detail="Auto-ban disabled"
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to disable auto-ban: {e}")
            return False

    async def enable_auto_ban(
        self,
        community_id: int,
        admin_id: int,
        threshold: int = 450
    ) -> bool:
        """Enable auto-ban for a community with specified threshold."""
        try:
            # Validate threshold
            if threshold < Config.REPUTATION_MIN or threshold > Config.REPUTATION_MAX:
                self.logger.warning(f"Invalid threshold {threshold}")
                return False

            self.dal.executesql(
                """UPDATE community_reputation_config
                   SET auto_ban_enabled = true,
                       auto_ban_threshold = %s,
                       updated_at = NOW()
                   WHERE community_id = %s""",
                [threshold, community_id]
            )
            self.dal.commit()
            self.weight_manager.invalidate_cache(community_id)

            self.logger.audit(
                action="enable_auto_ban",
                user=str(admin_id),
                community=str(community_id),
                result="SUCCESS",
                detail="Auto-ban enabled",
                threshold=threshold
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to enable auto-ban: {e}")
            return False
