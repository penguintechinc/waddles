"""
Retention Service - Cohort analysis and retention tracking
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta, date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from .log_safety import log_failure


class RetentionService:
    """Track user retention through cohort analysis."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def calculate_retention(
        self,
        community_id: int,
        cohort_period: str = 'week'
    ) -> Dict[str, Any]:
        """
        Calculate retention cohorts by join period.

        Args:
            community_id: Community ID
            cohort_period: 'week' or 'month' for cohort grouping

        Returns:
        - cohorts: list of cohort data with retention rates
        - summary: overall retention metrics
        - cohort_type: type of cohort analysis
        - analysis_date: date of analysis

        Cohort structure:
        - cohort_date: week/month user joined
        - original_count: users who joined in period
        - day_0, day_7, day_14, day_30, day_60, day_90: retention at each period
        - retention_rates: percentages at each period
        """
        try:
            # Check if community is premium
            config = await self._get_config(community_id)
            if not config.get('is_premium', False):
                return {
                    'error': 'Premium feature',
                    'message': 'Retention cohorts require premium'
                }

            analysis_date = datetime.utcnow().date()
            cohort_type = cohort_period

            # Calculate retention for each cohort
            cohorts = await self._calculate_cohorts(community_id, cohort_period)

            # Generate summary metrics
            summary = self._summarize_retention(cohorts)

            # Store cohort data
            await self._store_cohort_data(community_id, cohorts, cohort_period)

            self.logger.audit(
                "Retention cohorts calculated",
                community_id=community_id,
                action="calculate_retention",
                cohort_period=cohort_period,
                cohort_count=len(cohorts),
                result="SUCCESS"
            )

            return {
                'community_id': community_id,
                'analysis_date': analysis_date.isoformat(),
                'cohort_type': cohort_type,
                'cohorts': cohorts,
                'summary': summary
            }

        except Exception as e:
            log_failure(self.logger, "Failed to calculate retention", e, community_id=community_id)
            raise

    async def _calculate_cohorts(
        self,
        community_id: int,
        cohort_period: str
    ) -> List[Dict[str, Any]]:
        """Calculate retention for each cohort period."""
        try:
            cohorts = []

            # Determine date range and grouping
            if cohort_period == 'week':
                start_date = datetime.utcnow().date() - timedelta(days=180)  # 6 months
                date_format = "DATE_TRUNC('week', joined_at)::date"
            else:  # month
                start_date = datetime.utcnow().date() - relativedelta(months=12)  # 12 months
                date_format = "DATE_TRUNC('month', joined_at)::date"

            # Get all cohort periods with user counts
            result = self.dal.executesql(
                f"""SELECT {date_format} as cohort_date, COUNT(*) as cohort_size
                   FROM community_members
                   WHERE community_id = %s
                   AND joined_at >= %s
                   GROUP BY cohort_date
                   ORDER BY cohort_date DESC""",
                [community_id, start_date]
            )

            if not result:
                return []

            # For each cohort, calculate retention at different intervals
            for cohort_date, cohort_size in result:
                retention_data = {
                    'cohort_date': cohort_date.isoformat() if cohort_date else None,
                    'original_count': int(cohort_size),
                    'retention_by_day': {}
                }

                # Check retention at day 0, 7, 14, 30, 60, 90
                for days_offset in [0, 7, 14, 30, 60, 90]:
                    check_date = cohort_date + timedelta(days=days_offset) if cohort_date else None
                    if not check_date:
                        continue

                    # Count how many from this cohort were active on/after check_date
                    active_result = self.dal.executesql(
                        """SELECT COUNT(DISTINCT cm.user_id)
                           FROM community_members cm
                           LEFT JOIN activity_message_events ame
                               ON cm.user_id = (
                                   SELECT user_id FROM hub_user_identities hui
                                   WHERE hui.platform_user_id = ame.platform_user_id
                               )
                           WHERE cm.community_id = %s
                           AND DATE_TRUNC(%s, cm.joined_at)::date = %s
                           AND (ame.created_at >= %s OR ame.created_at IS NULL)""",
                        [community_id, f"'{cohort_period}'", cohort_date, check_date]
                    )

                    if active_result and active_result[0][0]:
                        active_count = int(active_result[0][0])
                    else:
                        # Fallback: simpler query
                        active_result = self.dal.executesql(
                            """SELECT COUNT(DISTINCT cm.user_id)
                               FROM community_members cm
                               WHERE cm.community_id = %s
                               AND DATE_TRUNC(%s, cm.joined_at)::date = %s
                               AND cm.joined_at < %s""",
                            [community_id, f"'{cohort_period}'", cohort_date, check_date + timedelta(days=1)]
                        )
                        active_count = active_result[0][0] if active_result else 0

                    retention_rate = (active_count / cohort_size * 100) if cohort_size > 0 else 0
                    retention_data['retention_by_day'][f'day_{days_offset}'] = {
                        'retained_count': int(active_count),
                        'retention_rate': round(retention_rate, 2)
                    }

                cohorts.append(retention_data)

            return cohorts

        except Exception as e:
            log_failure(self.logger, "Failed to calculate cohorts", e, community_id=community_id)
            return []

    async def _store_cohort_data(
        self,
        community_id: int,
        cohorts: List[Dict[str, Any]],
        cohort_period: str
    ) -> None:
        """Store cohort data in database."""
        try:
            for cohort in cohorts:
                cohort_date = cohort.get('cohort_date')
                if not cohort_date:
                    continue

                cohort_date_obj = datetime.fromisoformat(cohort_date).date()

                for day_key, retention_info in cohort.get('retention_by_day', {}).items():
                    # Extract day number from day_0, day_7, etc.
                    days_since_join = int(day_key.split('_')[1])

                    self.dal.executesql(
                        """INSERT INTO analytics_retention_cohorts
                           (community_id, cohort_date, cohort_type, days_since_join,
                            retained_count, original_count, retention_rate)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (community_id, cohort_date, cohort_type, days_since_join)
                           DO UPDATE SET
                               retained_count = EXCLUDED.retained_count,
                               retention_rate = EXCLUDED.retention_rate""",
                        [
                            community_id,
                            cohort_date_obj,
                            cohort_period,
                            days_since_join,
                            retention_info['retained_count'],
                            cohort['original_count'],
                            Decimal(str(retention_info['retention_rate'] / 100))
                        ]
                    )

            self.dal.commit()

        except Exception as e:
            log_failure(self.logger, "Failed to store cohort data", e, community_id=community_id)

    def _summarize_retention(self, cohorts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate summary retention metrics."""
        try:
            if not cohorts:
                return {
                    'avg_retention_day_7': 0,
                    'avg_retention_day_30': 0,
                    'avg_retention_day_90': 0,
                    'total_cohorts': 0
                }

            # Calculate averages
            day_7_rates = []
            day_30_rates = []
            day_90_rates = []

            for cohort in cohorts:
                retention_by_day = cohort.get('retention_by_day', {})
                if 'day_7' in retention_by_day:
                    day_7_rates.append(retention_by_day['day_7']['retention_rate'])
                if 'day_30' in retention_by_day:
                    day_30_rates.append(retention_by_day['day_30']['retention_rate'])
                if 'day_90' in retention_by_day:
                    day_90_rates.append(retention_by_day['day_90']['retention_rate'])

            return {
                'total_cohorts': len(cohorts),
                'avg_retention_day_7': round(sum(day_7_rates) / len(day_7_rates), 2) if day_7_rates else 0,
                'avg_retention_day_30': round(sum(day_30_rates) / len(day_30_rates), 2) if day_30_rates else 0,
                'avg_retention_day_90': round(sum(day_90_rates) / len(day_90_rates), 2) if day_90_rates else 0,
                'trend': self._calculate_trend(cohorts)
            }

        except Exception as e:
            log_failure(self.logger, "Failed to summarize retention", e)
            return {}

    def _calculate_trend(self, cohorts: List[Dict[str, Any]]) -> str:
        """Calculate if retention is improving or declining."""
        if len(cohorts) < 2:
            return 'insufficient_data'

        # Compare most recent cohorts
        recent = cohorts[0:1]
        previous = cohorts[1:2]

        if not recent or not previous:
            return 'insufficient_data'

        recent_day_7 = recent[0].get('retention_by_day', {}).get('day_7', {}).get('retention_rate', 0)
        previous_day_7 = previous[0].get('retention_by_day', {}).get('day_7', {}).get('retention_rate', 0)

        if recent_day_7 > previous_day_7:
            return 'improving'
        elif recent_day_7 < previous_day_7:
            return 'declining'
        else:
            return 'stable'

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
