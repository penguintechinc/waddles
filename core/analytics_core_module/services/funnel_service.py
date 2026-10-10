"""
Funnel Service - Engagement funnel tracking and conversion analysis
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, date
from decimal import Decimal
from .log_safety import log_failure


class FunnelService:
    """Track user engagement through defined funnels."""

    # Define engagement funnel steps
    FUNNEL_STEPS = {
        'new_user_activation': [
            {'number': 1, 'name': 'joined', 'description': 'User joined community'},
            {'number': 2, 'name': 'first_message', 'description': 'Sent first message'},
            {'number': 3, 'name': 'five_messages', 'description': 'Sent 5+ messages'},
            {'number': 4, 'name': 'regular_active', 'description': 'Weekly active user'}
        ]
    }

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def track_engagement_funnel(self, community_id: int) -> Dict[str, Any]:
        """
        Track engagement funnel progression.

        Returns:
        - funnel_name: 'new_user_activation'
        - steps: list of funnel steps with user counts and conversion rates
        - summary: overall funnel metrics
        - period_start: start date of analysis
        - period_end: end date of analysis

        Funnel flow:
        1. Joined: User is member
        2. First Message: User sent any message
        3. 5+ Messages: User sent 5+ messages
        4. Regular Active: User had activity in past 7 days
        """
        try:
            # Check if community is premium
            config = await self._get_config(community_id)
            if not config.get('is_premium', False):
                return {
                    'error': 'Premium feature',
                    'message': 'Engagement funnel tracking requires premium'
                }

            period_end = datetime.utcnow().date()
            period_start = period_end - timedelta(days=30)  # Last 30 days

            # Get funnel data for new_user_activation
            funnel_data = await self._calculate_funnel_steps(
                community_id,
                'new_user_activation',
                period_start,
                period_end
            )

            # Generate summary
            summary = self._summarize_funnel(funnel_data)

            # Store funnel data
            await self._store_funnel_data(community_id, 'new_user_activation', funnel_data, period_start, period_end)

            self.logger.audit(
                "Engagement funnel tracked",
                community_id=community_id,
                action="track_engagement_funnel",
                funnel_name="new_user_activation",
                result="SUCCESS"
            )

            return {
                'community_id': community_id,
                'funnel_name': 'new_user_activation',
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat(),
                'steps': funnel_data,
                'summary': summary
            }

        except Exception as e:
            log_failure(self.logger, "Failed to track engagement funnel", e, community_id=community_id)
            raise

    async def _calculate_funnel_steps(
        self,
        community_id: int,
        funnel_name: str,
        period_start: date,
        period_end: date
    ) -> List[Dict[str, Any]]:
        """Calculate user progression through each funnel step."""
        try:
            funnel_steps = self.FUNNEL_STEPS.get(funnel_name, [])
            if not funnel_steps:
                return []

            results = []

            # Step 1: Joined (community members)
            joined_result = self.dal.executesql(
                """SELECT COUNT(*) FROM community_members
                   WHERE community_id = %s
                   AND joined_at >= %s
                   AND joined_at <= %s""",
                [community_id, period_start, period_end]
            )
            step_1_count = joined_result[0][0] if joined_result else 0

            step_1 = {
                'step_number': 1,
                'step_name': 'joined',
                'step_description': 'User joined community',
                'users_at_step': int(step_1_count),
                'conversion_rate': 100.0,  # Baseline
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat()
            }
            results.append(step_1)

            if step_1_count == 0:
                return results  # Can't progress further with no users

            # Step 2: First Message (users who sent at least 1 message)
            first_message_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT cm.user_id)
                   FROM community_members cm
                   INNER JOIN activity_message_events ame
                       ON cm.user_id = (
                           SELECT hub_user_id FROM activity_message_events
                           WHERE community_id = %s LIMIT 1
                       )
                   WHERE cm.community_id = %s
                   AND cm.joined_at >= %s
                   AND cm.joined_at <= %s
                   AND ame.community_id = %s
                   AND ame.created_at >= %s""",
                [community_id, community_id, period_start, period_end, community_id, period_start]
            )
            step_2_count = first_message_result[0][0] if first_message_result else 0

            step_2_conversion = (step_2_count / step_1_count * 100) if step_1_count > 0 else 0
            step_2 = {
                'step_number': 2,
                'step_name': 'first_message',
                'step_description': 'Sent first message',
                'users_at_step': int(step_2_count),
                'conversion_rate': round(step_2_conversion, 2),
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat()
            }
            results.append(step_2)

            if step_2_count == 0:
                return results

            # Step 3: 5+ Messages (users who sent 5+ messages)
            five_messages_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= %s
                   AND created_at <= %s
                   AND hub_user_id IN (
                       SELECT DISTINCT hub_user_id FROM activity_message_events
                       WHERE community_id = %s
                       AND created_at >= %s
                       GROUP BY hub_user_id
                       HAVING COUNT(*) >= 5
                   )""",
                [community_id, period_start, period_end, community_id, period_start]
            )
            step_3_count = five_messages_result[0][0] if five_messages_result else 0

            step_3_conversion = (step_3_count / step_2_count * 100) if step_2_count > 0 else 0
            step_3 = {
                'step_number': 3,
                'step_name': 'five_messages',
                'step_description': 'Sent 5+ messages',
                'users_at_step': int(step_3_count),
                'conversion_rate': round(step_3_conversion, 2),
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat()
            }
            results.append(step_3)

            if step_3_count == 0:
                return results

            # Step 4: Regular Active (weekly active - had activity in past 7 days from period end)
            regular_active_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= %s
                   AND created_at <= %s
                   AND hub_user_id IN (
                       SELECT DISTINCT hub_user_id FROM activity_message_events
                       WHERE community_id = %s
                       AND created_at >= %s
                       GROUP BY hub_user_id
                       HAVING COUNT(*) >= 5
                   )
                   AND created_at >= (%s::date - INTERVAL '7 days')""",
                [community_id, period_start, period_end, community_id, period_start, period_end]
            )
            step_4_count = regular_active_result[0][0] if regular_active_result else 0

            step_4_conversion = (step_4_count / step_3_count * 100) if step_3_count > 0 else 0
            step_4 = {
                'step_number': 4,
                'step_name': 'regular_active',
                'step_description': 'Weekly active user',
                'users_at_step': int(step_4_count),
                'conversion_rate': round(step_4_conversion, 2),
                'period_start': period_start.isoformat(),
                'period_end': period_end.isoformat()
            }
            results.append(step_4)

            return results

        except Exception as e:
            log_failure(self.logger, "Failed to calculate funnel steps", e, community_id=community_id)
            return []

    async def _store_funnel_data(
        self,
        community_id: int,
        funnel_name: str,
        funnel_steps: List[Dict[str, Any]],
        period_start: date,
        period_end: date
    ) -> None:
        """Store funnel data in database."""
        try:
            for step in funnel_steps:
                self.dal.executesql(
                    """INSERT INTO analytics_engagement_funnels
                       (community_id, funnel_name, step_number, step_name, step_description,
                        users_at_step, conversion_rate, period_start, period_end)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (community_id, funnel_name, step_number, period_start)
                       DO UPDATE SET
                           users_at_step = EXCLUDED.users_at_step,
                           conversion_rate = EXCLUDED.conversion_rate""",
                    [
                        community_id,
                        funnel_name,
                        step['step_number'],
                        step['step_name'],
                        step.get('step_description'),
                        step['users_at_step'],
                        Decimal(str(step['conversion_rate'] / 100)),
                        period_start,
                        period_end
                    ]
                )

            self.dal.commit()

        except Exception as e:
            log_failure(self.logger, "Failed to store funnel data", e, community_id=community_id)

    def _summarize_funnel(self, funnel_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate funnel summary metrics."""
        try:
            if not funnel_steps:
                return {
                    'total_funnel_steps': 0,
                    'overall_conversion': 0,
                    'drop_off_step': None
                }

            # Overall conversion from step 1 to last step
            first_step_users = funnel_steps[0]['users_at_step'] if funnel_steps else 0
            last_step_users = funnel_steps[-1]['users_at_step'] if funnel_steps else 0

            overall_conversion = (last_step_users / first_step_users * 100) if first_step_users > 0 else 0

            # Find biggest drop-off
            biggest_drop = 0
            drop_off_step = None
            for i in range(len(funnel_steps) - 1):
                drop = funnel_steps[i]['conversion_rate'] - funnel_steps[i + 1]['conversion_rate']
                if drop > biggest_drop:
                    biggest_drop = drop
                    drop_off_step = funnel_steps[i + 1]['step_name']

            return {
                'total_steps': len(funnel_steps),
                'overall_conversion': round(overall_conversion, 2),
                'biggest_drop_off_step': drop_off_step,
                'biggest_drop_off_rate': round(biggest_drop, 2),
                'recommendations': self._generate_recommendations(funnel_steps)
            }

        except Exception as e:
            log_failure(self.logger, "Failed to summarize funnel", e)
            return {}

    def _generate_recommendations(self, funnel_steps: List[Dict[str, Any]]) -> List[str]:
        """Generate improvement recommendations based on funnel metrics."""
        recommendations = []

        if not funnel_steps:
            return recommendations

        # Check each conversion rate
        for step in funnel_steps:
            conversion = step['conversion_rate']
            step_name = step['step_name']

            if step_name == 'first_message' and conversion < 50:
                recommendations.append(
                    "Low first message rate. Create welcome messages or onboarding content to encourage participation."
                )
            elif step_name == 'five_messages' and conversion < 50:
                recommendations.append(
                    "Low engagement progression. Foster discussions and make it easier for users to contribute."
                )
            elif step_name == 'regular_active' and conversion < 40:
                recommendations.append(
                    "Low retention to weekly active. Improve community content and engagement to maintain participation."
                )

        if not recommendations:
            recommendations.append("Funnel metrics look healthy. Continue current engagement strategies.")

        return recommendations

    async def _get_config(self, community_id: int) -> Dict[str, Any]:
        """Get analytics config for community."""
        try:
            result = self.dal.executesql(
                """SELECT is_premium FROM analytics_config
                   WHERE community_id = %s""",
                [community_id]
            )
            if result:
                return {'is_premium': result[0][0]}
            return {'is_premium': False}
        except Exception as e:
            log_failure(self.logger, "Failed to get config", e, community_id=community_id)
            return {'is_premium': False}
