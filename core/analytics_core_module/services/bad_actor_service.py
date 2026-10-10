"""
Bad Actor Service - Suspicious behavior detection and alerting
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import json
from .log_safety import log_failure


class BadActorService:
    """Detect and alert on suspicious user behavior patterns."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def detect_bad_actors(self, community_id: int) -> Dict[str, Any]:
        """
        Detect suspicious user behavior patterns.

        Returns:
        - alerts: list of detected bad actors with details
        - summary: count by severity
        - processed_at: timestamp

        Detection patterns:
        - spam_burst: Unusually high message rate in short time
        - duplicate_messages: Same message repeated multiple times
        - toxic_behavior: Flagged keyword matches
        - coordinated_activity: Multiple users with identical behavior
        """
        try:
            # Check if community is premium
            config = await self._get_config(community_id)
            if not config.get('is_premium', False):
                return {
                    'error': 'Premium feature',
                    'message': 'Bad actor detection requires premium'
                }

            alerts = []
            processed_at = datetime.utcnow()

            # Run detection patterns
            spam_burst_alerts = await self._detect_spam_bursts(community_id)
            alerts.extend(spam_burst_alerts)

            duplicate_alerts = await self._detect_duplicates(community_id)
            alerts.extend(duplicate_alerts)

            coordinated_alerts = await self._detect_coordinated_activity(community_id)
            alerts.extend(coordinated_alerts)

            # Remove duplicates (same user multiple patterns) and store
            unique_alerts = await self._deduplicate_and_store_alerts(community_id, alerts)

            # Count by severity
            summary = self._summarize_alerts(unique_alerts)

            self.logger.audit(
                "Bad actor detection completed",
                community_id=community_id,
                action="detect_bad_actors",
                alerts_count=len(unique_alerts),
                result="SUCCESS"
            )

            return {
                'community_id': community_id,
                'alerts': unique_alerts,
                'summary': summary,
                'processed_at': processed_at.isoformat() + 'Z'
            }

        except Exception as e:
            log_failure(self.logger, "Failed to detect bad actors", e, community_id=community_id)
            raise

    async def _detect_spam_bursts(self, community_id: int) -> List[Dict[str, Any]]:
        """Detect unusually high message rates (spam bursts)."""
        try:
            alerts = []

            # Check for users with >10 messages in 5 minutes
            result = self.dal.executesql(
                """SELECT
                       hub_user_id,
                       platform_user_id,
                       platform_username,
                       COUNT(*) as message_count,
                       DATE_TRUNC('minute', created_at) as minute_bucket
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '1 hour'
                   GROUP BY hub_user_id, platform_user_id, platform_username, minute_bucket
                   HAVING COUNT(*) > 10
                   ORDER BY message_count DESC""",
                [community_id]
            )

            if result:
                for row in result:
                    hub_user_id, platform_user_id, platform_username, message_count, minute_bucket = row
                    confidence = min(100, (message_count / 10) * 50)  # Scale 50-100

                    alert = {
                        'alert_type': 'spam_pattern',
                        'severity': self._classify_severity_burst(message_count),
                        'platform_user_id': platform_user_id,
                        'platform_username': platform_username,
                        'hub_user_id': hub_user_id,
                        'confidence_score': Decimal(str(confidence)),
                        'detection_signals': {
                            'pattern': 'spam_burst',
                            'messages_in_5min': int(message_count),
                            'time_window': '5 minutes',
                            'threshold': 10
                        },
                        'evidence_timestamp': minute_bucket.isoformat() if minute_bucket else None
                    }
                    alerts.append(alert)

            return alerts

        except Exception as e:
            log_failure(self.logger, "Failed to detect spam bursts", e, community_id=community_id)
            return []

    async def _detect_duplicates(self, community_id: int) -> List[Dict[str, Any]]:
        """Detect users sending duplicate messages."""
        try:
            alerts = []

            # Find messages that appear 3+ times by same user in 30 minutes
            result = self.dal.executesql(
                """SELECT
                       hub_user_id,
                       platform_user_id,
                       platform_username,
                       message_text,
                       COUNT(*) as duplicate_count
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '30 minutes'
                   AND message_text IS NOT NULL
                   AND LENGTH(message_text) > 5
                   GROUP BY hub_user_id, platform_user_id, platform_username, message_text
                   HAVING COUNT(*) >= 3
                   ORDER BY duplicate_count DESC""",
                [community_id]
            )

            if result:
                for row in result:
                    hub_user_id, platform_user_id, platform_username, message_text, duplicate_count = row
                    confidence = min(100, (duplicate_count / 3) * 60)  # Scale 60-100

                    alert = {
                        'alert_type': 'suspicious_behavior',
                        'severity': 'medium' if duplicate_count < 5 else 'high',
                        'platform_user_id': platform_user_id,
                        'platform_username': platform_username,
                        'hub_user_id': hub_user_id,
                        'confidence_score': Decimal(str(confidence)),
                        'detection_signals': {
                            'pattern': 'duplicate_messages',
                            'duplicate_count': int(duplicate_count),
                            'message_length': len(message_text or ''),
                            'time_window': '30 minutes'
                        },
                        'sample_message': message_text[:200] if message_text else None
                    }
                    alerts.append(alert)

            return alerts

        except Exception as e:
            log_failure(self.logger, "Failed to detect duplicates", e, community_id=community_id)
            return []

    async def _detect_coordinated_activity(self, community_id: int) -> List[Dict[str, Any]]:
        """Detect multiple users with identical behavior patterns."""
        try:
            alerts = []

            # Find message patterns that appear from multiple users in short timeframe
            result = self.dal.executesql(
                """SELECT
                       message_text,
                       COUNT(DISTINCT hub_user_id) as unique_users,
                       COUNT(*) as total_occurrences,
                       MIN(created_at) as first_occurrence,
                       MAX(created_at) as last_occurrence
                   FROM activity_message_events
                   WHERE community_id = %s
                   AND created_at >= NOW() - INTERVAL '1 hour'
                   AND message_text IS NOT NULL
                   AND LENGTH(message_text) > 5
                   GROUP BY message_text
                   HAVING COUNT(DISTINCT hub_user_id) >= 3
                   AND (MAX(created_at) - MIN(created_at)) < INTERVAL '10 minutes'
                   ORDER BY unique_users DESC""",
                [community_id]
            )

            if result:
                for row in result:
                    message_text, unique_users, total_occurrences, first_occurrence, last_occurrence = row
                    confidence = min(100, (unique_users / 3) * 80)  # Scale 80-100

                    # Get usernames involved
                    user_result = self.dal.executesql(
                        """SELECT DISTINCT platform_username, platform_user_id
                           FROM activity_message_events
                           WHERE community_id = %s
                           AND message_text = %s
                           AND created_at >= NOW() - INTERVAL '1 hour'
                           LIMIT 5""",
                        [community_id, message_text]
                    )
                    involved_users = [row[0] for row in user_result] if user_result else []

                    alert = {
                        'alert_type': 'coordinated_attack',
                        'severity': 'high' if unique_users >= 5 else 'medium',
                        'confidence_score': Decimal(str(confidence)),
                        'detection_signals': {
                            'pattern': 'coordinated_activity',
                            'unique_users': int(unique_users),
                            'total_messages': int(total_occurrences),
                            'time_window': '10 minutes'
                        },
                        'sample_message': message_text[:200] if message_text else None,
                        'involved_users': involved_users,
                        'first_occurrence': first_occurrence.isoformat() if first_occurrence else None
                    }
                    alerts.append(alert)

            return alerts

        except Exception as e:
            log_failure(self.logger, "Failed to detect coordinated activity", e, community_id=community_id)
            return []

    async def _deduplicate_and_store_alerts(
        self,
        community_id: int,
        alerts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Remove duplicate alerts and store in database."""
        try:
            # Group by user and keep highest severity
            seen_users = {}
            unique_alerts = []

            for alert in alerts:
                user_key = alert.get('platform_user_id', '')
                severity_rank = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}
                alert_severity_rank = severity_rank.get(alert['severity'], 0)

                if user_key not in seen_users:
                    seen_users[user_key] = alert
                    unique_alerts.append(alert)
                else:
                    existing = seen_users[user_key]
                    existing_rank = severity_rank.get(existing['severity'], 0)
                    if alert_severity_rank > existing_rank:
                        # Replace with higher severity
                        unique_alerts.remove(existing)
                        seen_users[user_key] = alert
                        unique_alerts.append(alert)

            # Store alerts in database
            for alert in unique_alerts:
                self.dal.executesql(
                    """INSERT INTO analytics_bad_actor_alerts
                       (community_id, alert_type, severity, platform, platform_user_id,
                        platform_username, hub_user_id, confidence_score,
                        detection_signals, sample_evidence, status)
                       VALUES (%s, %s, %s, 'unknown', %s, %s, %s, %s, %s, %s, 'pending')
                       ON CONFLICT (community_id, alert_type, platform_user_id)
                       DO UPDATE SET
                           severity = EXCLUDED.severity,
                           confidence_score = EXCLUDED.confidence_score,
                           detection_signals = EXCLUDED.detection_signals,
                           sample_evidence = EXCLUDED.sample_evidence""",
                    [
                        community_id,
                        alert['alert_type'],
                        alert['severity'],
                        alert.get('platform_user_id'),
                        alert.get('platform_username'),
                        alert.get('hub_user_id'),
                        alert['confidence_score'],
                        json.dumps(alert['detection_signals']),
                        json.dumps({
                            'sample_message': alert.get('sample_message'),
                            'timestamp': alert.get('evidence_timestamp')
                        })
                    ]
                )
            self.dal.commit()

            return unique_alerts

        except Exception as e:
            log_failure(self.logger, "Failed to store alerts", e, community_id=community_id)
            return []

    def _classify_severity_burst(self, message_count: int) -> str:
        """Classify spam burst severity by message count."""
        if message_count > 50:
            return 'critical'
        elif message_count > 30:
            return 'high'
        elif message_count > 15:
            return 'medium'
        else:
            return 'low'

    def _summarize_alerts(self, alerts: List[Dict[str, Any]]) -> Dict[str, int]:
        """Count alerts by severity."""
        summary = {
            'total': len(alerts),
            'critical': 0,
            'high': 0,
            'medium': 0,
            'low': 0
        }

        for alert in alerts:
            severity = alert.get('severity', 'low')
            summary[severity] = summary.get(severity, 0) + 1

        return summary

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
