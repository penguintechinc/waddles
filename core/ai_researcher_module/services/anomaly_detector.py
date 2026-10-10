"""
Anomaly Detection Service for AI Researcher Module
==================================================

Detects unusual patterns in community activity, sentiment shifts, and user behavior.

Features:
- Activity spike detection
- Sentiment shift analysis
- Unusual user behavior patterns
- Automated alerting for admins
- Historical baseline establishment
"""

import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from statistics import mean, stdev

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AnomalyResult:
    """Result from anomaly detection"""
    success: bool
    anomalies: List[Dict[str, Any]]
    total_detected: int
    processing_time_ms: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'success': self.success,
            'anomalies': self.anomalies,
            'total_detected': self.total_detected,
            'processing_time_ms': self.processing_time_ms,
            'error': self.error
        }


class AnomalyDetector:
    """
    Service for detecting anomalies in community activity.

    Detects:
    - Activity spikes (unusual message volume)
    - Sentiment shifts (sudden mood changes)
    - User behavior anomalies (unusual patterns)
    - Bot-like activity patterns
    """

    def __init__(self, dal):
        """
        Initialize anomaly detector.

        Args:
            dal: Database connection (AsyncDAL)
        """
        self.dal = dal
        logger.info("AnomalyDetector initialized")

    async def detect_anomalies(
        self,
        community_id: int,
        check_types: Optional[List[str]] = None
    ) -> AnomalyResult:
        """
        Detect anomalies in community activity.

        Args:
            community_id: Community identifier
            check_types: Types of anomalies to check
                        (default: ['activity', 'sentiment', 'user_behavior'])

        Returns:
            AnomalyResult with detected anomalies
        """
        start_time = time.time()

        if check_types is None:
            check_types = ['activity', 'sentiment', 'user_behavior']

        try:
            logger.info(
                f"Detecting anomalies",
                extra={
                    'community_id': community_id,
                    'check_types': check_types
                }
            )

            anomalies = []

            # Run each anomaly check
            if 'activity' in check_types:
                activity_anomalies = await self._detect_activity_spikes(community_id)
                anomalies.extend(activity_anomalies)

            if 'sentiment' in check_types:
                sentiment_anomalies = await self._detect_sentiment_shifts(community_id)
                anomalies.extend(sentiment_anomalies)

            if 'user_behavior' in check_types:
                behavior_anomalies = await self._detect_user_behavior_anomalies(community_id)
                anomalies.extend(behavior_anomalies)

            processing_time = int((time.time() - start_time) * 1000)

            logger.info(
                f"Anomaly detection completed",
                extra={
                    'community_id': community_id,
                    'anomalies_found': len(anomalies),
                    'processing_time_ms': processing_time
                }
            )

            return AnomalyResult(
                success=True,
                anomalies=anomalies,
                total_detected=len(anomalies),
                processing_time_ms=processing_time
            )

        except Exception as e:
            logger.error(
                f"Anomaly detection error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return AnomalyResult(
                success=False,
                anomalies=[],
                total_detected=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=describe_db_error(e)
            )

    async def _detect_activity_spikes(self, community_id: int) -> List[Dict[str, Any]]:
        """
        Detect unusual spikes in message activity.

        Args:
            community_id: Community identifier

        Returns:
            List of anomaly dicts
        """
        try:
            # Get hourly message counts for the last 7 days
            query = """
                SELECT
                    DATE_TRUNC('hour', created_at) as hour,
                    COUNT(*) as message_count
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= NOW() - INTERVAL '7 days'
                GROUP BY DATE_TRUNC('hour', created_at)
                ORDER BY hour DESC
            """

            rows = await self.dal.execute(query, [community_id])

            if not rows or len(rows) < 10:
                return []

            # Extract message counts
            counts = [row.get('message_count', 0) for row in rows]

            # Calculate baseline (mean of last 7 days)
            baseline = mean(counts)
            std_dev = stdev(counts) if len(counts) > 1 else 0

            # Detect spikes (> 2 std devs above mean)
            anomalies = []
            spike_threshold = baseline + (2 * std_dev) if std_dev > 0 else baseline * 2

            for row in rows[:24]:  # Check last 24 hours
                hour = row.get('hour')
                count = row.get('message_count', 0)

                if count > spike_threshold:
                    anomalies.append({
                        'type': 'activity_spike',
                        'severity': 'high' if count > baseline + (3 * std_dev) else 'medium',
                        'timestamp': str(hour),
                        'value': count,
                        'baseline': baseline,
                        'deviation': count - baseline,
                        'message': f"Activity spike detected: {count} messages vs baseline {baseline:.0f}"
                    })

            return anomalies

        except Exception as e:
            logger.error(f"Activity spike detection error: {describe_db_error(e)}")
            return []

    async def _detect_sentiment_shifts(self, community_id: int) -> List[Dict[str, Any]]:
        """
        Detect significant sentiment shifts.

        Args:
            community_id: Community identifier

        Returns:
            List of anomaly dicts
        """
        try:
            # TODO: Implement sentiment analysis integration
            # For now, return empty list
            logger.debug("Sentiment shift detection not yet implemented")
            return []

        except Exception as e:
            logger.error(f"Sentiment shift detection error: {describe_db_error(e)}")
            return []

    async def _detect_user_behavior_anomalies(self, community_id: int) -> List[Dict[str, Any]]:
        """
        Detect unusual user behavior patterns.

        Args:
            community_id: Community identifier

        Returns:
            List of anomaly dicts
        """
        try:
            # Detect users with unusual posting patterns
            query = """
                SELECT
                    platform_user_id,
                    platform_username,
                    COUNT(*) as message_count,
                    COUNT(DISTINCT DATE(created_at)) as days_active,
                    AVG(LENGTH(message_content)) as avg_length,
                    COUNT(DISTINCT EXTRACT(HOUR FROM created_at)) as hours_active
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= NOW() - INTERVAL '1 day'
                GROUP BY platform_user_id, platform_username
                HAVING COUNT(*) > 10
                ORDER BY message_count DESC
                LIMIT 50
            """

            rows = await self.dal.execute(query, [community_id])

            if not rows:
                return []

            anomalies = []
            message_counts = [row.get('message_count', 0) for row in rows]

            # Get baseline stats
            baseline_count = mean(message_counts) if message_counts else 0
            std_dev = stdev(message_counts) if len(message_counts) > 1 else 0

            for row in rows:
                user_id = row.get('platform_user_id')
                username = row.get('platform_username', user_id)
                count = row.get('message_count', 0)
                hours_active = row.get('hours_active', 0)

                # Detect very high activity in short time span
                if count > baseline_count + (2 * std_dev) and hours_active <= 2:
                    anomalies.append({
                        'type': 'user_behavior_anomaly',
                        'severity': 'medium',
                        'user_id': user_id,
                        'username': username,
                        'message_count': count,
                        'hours_active': hours_active,
                        'avg_length': float(row.get('avg_length', 0)) if row.get('avg_length') else 0,
                        'message': f"Unusual activity from {username}: {count} messages in {hours_active} hours"
                    })

            return anomalies

        except Exception as e:
            logger.error(f"User behavior anomaly detection error: {describe_db_error(e)}")
            return []

    async def acknowledge_anomaly(
        self,
        community_id: int,
        anomaly_id: int,
        admin_id: int,
        notes: Optional[str] = None
    ) -> bool:
        """
        Mark an anomaly as acknowledged/reviewed.

        Args:
            community_id: Community identifier
            anomaly_id: Anomaly ID to acknowledge
            admin_id: Admin ID acknowledging
            notes: Optional notes

        Returns:
            True if successful, False otherwise
        """
        try:
            query = """
                UPDATE ai_anomaly_detections
                SET is_acknowledged = TRUE,
                    acknowledged_by = $1,
                    acknowledged_at = NOW(),
                    admin_notes = $2
                WHERE id = $3 AND community_id = $4
            """

            await self.dal.execute(query, [admin_id, notes, anomaly_id, community_id])

            logger.audit(
                action="anomaly_acknowledged",
                user=str(admin_id),
                community=str(community_id),
                result="SUCCESS"
            )

            return True

        except Exception as e:
            logger.error(f"Anomaly acknowledgment error: {describe_db_error(e)}")
            return False

    async def store_anomaly(
        self,
        community_id: int,
        anomaly_type: str,
        severity: str,
        content: Dict[str, Any]
    ) -> Optional[int]:
        """
        Store detected anomaly in database.

        Args:
            community_id: Community identifier
            anomaly_type: Type of anomaly
            severity: Severity level (low, medium, high)
            content: Anomaly details

        Returns:
            Anomaly ID or None on error
        """
        try:
            query = """
                INSERT INTO ai_anomaly_detections (
                    community_id, anomaly_type, severity,
                    content, is_acknowledged,
                    created_at
                ) VALUES ($1, $2, $3, $4, FALSE, NOW())
                RETURNING id
            """

            rows = await self.dal.execute(query, [
                community_id,
                anomaly_type,
                severity,
                json.dumps(content)
            ])

            if rows:
                return rows[0].get('id')

            return None

        except Exception as e:
            logger.error(f"Anomaly storage error: {describe_db_error(e)}")
            return None

    async def get_recent_anomalies(
        self,
        community_id: int,
        hours: int = 24,
        acknowledged: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get recent anomalies for a community.

        Args:
            community_id: Community identifier
            hours: How many hours back to look
            acknowledged: If False, only unacknowledged anomalies

        Returns:
            List of anomaly dicts
        """
        try:
            where_clause = f"AND is_acknowledged = {acknowledged}" if not acknowledged else ""

            query = f"""
                SELECT id, anomaly_type, severity, content,
                       is_acknowledged, created_at
                FROM ai_anomaly_detections
                WHERE community_id = $1
                  AND created_at >= NOW() - INTERVAL '{hours} hours'
                  {where_clause}
                ORDER BY created_at DESC
                LIMIT 100
            """

            rows = await self.dal.execute(query, [community_id])

            anomalies = []
            for row in (rows or []):
                anomalies.append({
                    'id': row['id'],
                    'type': row['anomaly_type'],
                    'severity': row['severity'],
                    'content': row['content'] if isinstance(row['content'], dict) else
                               json.loads(row['content']),
                    'is_acknowledged': row['is_acknowledged'],
                    'created_at': str(row['created_at'])
                })

            return anomalies

        except Exception as e:
            logger.error(f"Get anomalies error: {describe_db_error(e)}")
            return []
