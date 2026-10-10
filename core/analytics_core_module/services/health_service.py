"""
Health Service - Community health scoring and analysis
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from decimal import Decimal

from flask_core.feature_flags import feature_enabled
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from .log_safety import log_failure


class HealthService:
    """Calculate community health scores with grade-based metrics."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def calculate_health_score(self, community_id: int) -> Dict[str, Any]:
        """
        Calculate community health score with factors.

        Returns:
        - health_grade: A-F grade
        - overall_score: 0-100 numeric score
        - factors: detailed breakdown of scoring factors
        - snapshot_date: date of calculation
        - recommendations: list of improvement suggestions

        Factors analyzed:
        - activity_consistency: Message rate consistency week-over-week
        - engagement_rate: Active users vs total members
        - retention: User return rate
        - message_distribution: Message spread across users (diversity)
        """
        try:
            # v3 Feature gate (analytics.community_health / waddles.analytics.
            # community_health, professional tier). The other 13 Core/platform
            # Features (analytics.*, video_proxy.*, auth.*, compliance.*,
            # integrations.*, tenancy.*) follow this identical guard at their
            # own call sites as they're wired up -- see
            # core_platform_module.features for the registered contracts.
            if not await feature_enabled(
                "waddles.analytics.community_health",
                tenant=DEFAULT_TENANT_SLUG,
                community=community_id,
            ):
                return {
                    'error': 'Feature not available',
                    'message': 'Community health scoring is not enabled for this tenant'
                }

            # Check if community is premium
            config = await self._get_config(community_id)
            if not config.get('is_premium', False):
                return {
                    'error': 'Premium feature',
                    'message': 'Community health scoring requires premium'
                }

            snapshot_date = datetime.utcnow().date()

            # Calculate individual factors
            activity_consistency = await self._calculate_activity_consistency(community_id)
            engagement_rate = await self._calculate_engagement_rate(community_id)
            retention_rate = await self._calculate_retention(community_id)
            message_distribution = await self._calculate_message_distribution(community_id)

            # Weight factors: activity_consistency(25%), engagement(30%), retention(25%), distribution(20%)
            overall_score = (
                (activity_consistency * 0.25) +
                (engagement_rate * 0.30) +
                (retention_rate * 0.25) +
                (message_distribution * 0.20)
            )

            # Convert to letter grade
            health_grade = self._score_to_grade(overall_score)

            # Generate recommendations
            recommendations = self._generate_recommendations(
                activity_consistency,
                engagement_rate,
                retention_rate,
                message_distribution
            )

            # Store daily snapshot
            await self._store_health_snapshot(
                community_id,
                snapshot_date,
                overall_score,
                health_grade,
                activity_consistency,
                engagement_rate,
                retention_rate,
                message_distribution
            )

            self.logger.audit(
                "Community health calculated",
                community_id=community_id,
                action="calculate_health_score",
                result="SUCCESS"
            )

            return {
                'community_id': community_id,
                'health_grade': health_grade,
                'overall_score': round(overall_score, 2),
                'snapshot_date': snapshot_date.isoformat(),
                'factors': {
                    'activity_consistency': round(activity_consistency, 2),
                    'engagement_rate': round(engagement_rate, 2),
                    'retention': round(retention_rate, 2),
                    'message_distribution': round(message_distribution, 2)
                },
                'recommendations': recommendations
            }

        except Exception as e:
            log_failure(self.logger, "Failed to calculate health score", e, community_id=community_id)
            raise

    async def _calculate_activity_consistency(self, community_id: int) -> float:
        """Calculate message rate consistency over last 4 weeks."""
        try:
            # Get message counts for last 4 weeks
            result = self.dal.executesql(
                """SELECT DATE(created_at) as day, COUNT(*) as message_count
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '28 days'
                   GROUP BY DATE(created_at)
                   ORDER BY day ASC""",
                [community_id]
            )

            if not result or len(result) < 7:
                return 0.0  # Need at least a week of data

            message_counts = [row[1] for row in result]
            avg_messages = sum(message_counts) / len(message_counts)

            if avg_messages == 0:
                return 0.0

            # Calculate coefficient of variation (lower is more consistent)
            variance = sum((x - avg_messages) ** 2 for x in message_counts) / len(message_counts)
            std_dev = variance ** 0.5
            coefficient_of_variation = (std_dev / avg_messages) if avg_messages > 0 else 0

            # Convert CV to 0-100 score (lower CV = higher score)
            # CV of 0 = 100, CV of 2+ = 0
            consistency_score = max(0, 100 - (coefficient_of_variation * 50))
            return consistency_score

        except Exception as e:
            log_failure(self.logger, "Failed to calculate activity consistency", e, community_id=community_id)
            return 0.0

    async def _calculate_engagement_rate(self, community_id: int) -> float:
        """Calculate percentage of active members in last 7 days."""
        try:
            # Total active members in last 7 days
            active_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '7 days'""",
                [community_id]
            )
            active_members = active_result[0][0] if active_result else 0

            # Total community members
            total_result = self.dal.executesql(
                """SELECT COUNT(*) FROM community_members
                   WHERE community_id = %s AND is_active = true""",
                [community_id]
            )
            total_members = total_result[0][0] if total_result else 0

            if total_members == 0:
                return 0.0

            engagement_rate = (active_members / total_members) * 100
            return min(100.0, engagement_rate)

        except Exception as e:
            log_failure(self.logger, "Failed to calculate engagement rate", e, community_id=community_id)
            return 0.0

    async def _calculate_retention(self, community_id: int) -> float:
        """Calculate member retention rate (new vs returning)."""
        try:
            # Members who joined in last 30 days
            new_result = self.dal.executesql(
                """SELECT COUNT(*) FROM community_members
                   WHERE community_id = %s
                   AND joined_at >= NOW() - INTERVAL '30 days'""",
                [community_id]
            )
            new_members = new_result[0][0] if new_result else 0

            # Members who were active 30+ days ago and still active
            retention_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events ame1
                   WHERE community_id = %s
                   AND EXISTS (
                       SELECT 1 FROM activity_message_events ame2
                       WHERE ame2.hub_user_id = ame1.hub_user_id
                       AND ame2.community_id = %s
                       AND ame2.created_at >= NOW() - INTERVAL '30 days'
                   )
                   AND ame1.created_at < NOW() - INTERVAL '30 days'""",
                [community_id, community_id]
            )
            retained_members = retention_result[0][0] if retention_result else 0

            # Churn rate calculation
            churn_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at BETWEEN NOW() - INTERVAL '60 days' AND NOW() - INTERVAL '30 days'
                   AND hub_user_id NOT IN (
                       SELECT DISTINCT hub_user_id FROM activity_message_events
                       WHERE community_id = %s
                       AND created_at >= NOW() - INTERVAL '30 days'
                   )""",
                [community_id, community_id]
            )
            churned_members = churn_result[0][0] if churn_result else 0

            # Retention score: balance between retaining old members and gaining new members
            if retained_members + new_members > 0:
                retention_score = (retained_members / (retained_members + new_members + churned_members + 1)) * 100
            else:
                retention_score = 0.0

            return min(100.0, retention_score)

        except Exception as e:
            log_failure(self.logger, "Failed to calculate retention", e, community_id=community_id)
            return 0.0

    async def _calculate_message_distribution(self, community_id: int) -> float:
        """Calculate message diversity (distribution across users)."""
        try:
            # Get message counts per user (last 7 days)
            result = self.dal.executesql(
                """SELECT COUNT(*) as message_count
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '7 days'
                   GROUP BY hub_user_id""",
                [community_id]
            )

            if not result or len(result) < 2:
                return 0.0  # Need multiple active users

            message_counts = [row[0] for row in result]
            total_messages = sum(message_counts)

            if total_messages == 0:
                return 0.0

            # Herfindahl Index (concentration measure)
            # 0 = perfect distribution, 1 = concentrated in one user
            hhi = sum((count / total_messages) ** 2 for count in message_counts)

            # Convert HHI to 0-100 score (higher diversity = higher score)
            # HHI near 0 = 100, HHI near 1 = 0
            distribution_score = (1 - hhi) * 100
            return distribution_score

        except Exception as e:
            log_failure(self.logger, "Failed to calculate message distribution", e, community_id=community_id)
            return 0.0

    async def _store_health_snapshot(
        self,
        community_id: int,
        snapshot_date: Any,
        overall_score: float,
        health_grade: str,
        activity_consistency: float,
        engagement_rate: float,
        retention_rate: float,
        message_distribution: float
    ) -> None:
        """Store daily health snapshot in database."""
        try:
            # Get additional metrics for snapshot
            total_members_result = self.dal.executesql(
                """SELECT COUNT(*) FROM community_members
                   WHERE community_id = %s AND is_active = true""",
                [community_id]
            )
            total_members = total_members_result[0][0] if total_members_result else 0

            active_7d_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '7 days'""",
                [community_id]
            )
            active_7d = active_7d_result[0][0] if active_7d_result else 0

            active_30d_result = self.dal.executesql(
                """SELECT COUNT(DISTINCT hub_user_id)
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '30 days'""",
                [community_id]
            )
            active_30d = active_30d_result[0][0] if active_30d_result else 0

            new_7d_result = self.dal.executesql(
                """SELECT COUNT(*) FROM community_members
                   WHERE community_id = %s
                   AND joined_at >= NOW() - INTERVAL '7 days'""",
                [community_id]
            )
            new_7d = new_7d_result[0][0] if new_7d_result else 0

            # Insert or update snapshot
            self.dal.executesql(
                """INSERT INTO analytics_community_health
                   (community_id, snapshot_date, total_members, active_members_7d,
                    active_members_30d, new_members_7d, engagement_score,
                    member_growth_rate, engagement_trend)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (community_id, snapshot_date)
                   DO UPDATE SET
                       total_members = EXCLUDED.total_members,
                       active_members_7d = EXCLUDED.active_members_7d,
                       active_members_30d = EXCLUDED.active_members_30d,
                       new_members_7d = EXCLUDED.new_members_7d,
                       engagement_score = EXCLUDED.engagement_score""",
                [community_id, snapshot_date, total_members, active_7d, active_30d,
                 new_7d, Decimal(str(overall_score)), Decimal('0'), Decimal('0')]
            )
            self.dal.commit()

        except Exception as e:
            log_failure(self.logger, "Failed to store health snapshot", e, community_id=community_id)

    def _score_to_grade(self, score: float) -> str:
        """Convert 0-100 score to A-F grade."""
        if score >= 90:
            return 'A'
        elif score >= 80:
            return 'B'
        elif score >= 70:
            return 'C'
        elif score >= 60:
            return 'D'
        else:
            return 'F'

    def _generate_recommendations(
        self,
        activity: float,
        engagement: float,
        retention: float,
        distribution: float
    ) -> list:
        """Generate improvement recommendations based on weak factors."""
        recommendations = []

        if activity < 60:
            recommendations.append(
                "Activity is inconsistent. Schedule regular events or posts to maintain engagement."
            )

        if engagement < 50:
            recommendations.append(
                "Low engagement rate. Consider reaching out to inactive members or hosting community events."
            )

        if retention < 50:
            recommendations.append(
                "High churn rate. Focus on onboarding and community building to improve retention."
            )

        if distribution < 40:
            recommendations.append(
                "Messages concentrated among few users. Encourage broader participation and discussions."
            )

        if not recommendations:
            recommendations.append("Community health is strong! Continue current engagement strategies.")

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
