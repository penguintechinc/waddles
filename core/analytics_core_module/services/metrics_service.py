"""
Metrics Service - Time-series metrics management
"""
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from .log_safety import log_failure


class MetricsService:
    """Manage time-series metrics with configurable bucket sizes."""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def get_timeseries(
        self,
        community_id: int,
        metric_type: str,
        bucket_size: str = '1d',
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get time-series metrics for a community.

        Args:
            community_id: Community ID
            metric_type: Type of metric (messages, viewers, engagement, growth)
            bucket_size: Bucket size (1h, 1d, 1w, 1m)
            start_date: Start date (ISO format)
            end_date: End date (ISO format)

        Returns:
            Time-series data with timestamps and values
        """
        try:
            # Parse dates
            if start_date:
                start_dt = date_parser.parse(start_date)
            else:
                start_dt = datetime.utcnow() - timedelta(days=30)

            if end_date:
                end_dt = date_parser.parse(end_date)
            else:
                end_dt = datetime.utcnow()

            # Query time-series data
            result = self.dal.executesql(
                """SELECT timestamp_bucket, value, metadata
                   FROM analytics_metrics_timeseries
                   WHERE community_id = %s
                   AND metric_type = %s
                   AND bucket_size = %s
                   AND timestamp_bucket >= %s
                   AND timestamp_bucket <= %s
                   ORDER BY timestamp_bucket ASC""",
                [community_id, metric_type, bucket_size, start_dt, end_dt]
            )

            timeseries = []
            if result:
                for row in result:
                    timeseries.append({
                        'timestamp': row[0].isoformat() + 'Z',
                        'value': float(row[1]),
                        'metadata': row[2] or {}
                    })

            return {
                'community_id': community_id,
                'metric_type': metric_type,
                'bucket_size': bucket_size,
                'start_date': start_dt.isoformat() + 'Z',
                'end_date': end_dt.isoformat() + 'Z',
                'data': timeseries,
                'count': len(timeseries)
            }

        except Exception as e:
            log_failure(
                self.logger,
                "Failed to get timeseries",
                e,
                community_id=community_id,
            )
            raise

    async def record_metric(
        self,
        community_id: int,
        metric_type: str,
        bucket_size: str,
        timestamp: datetime,
        value: float,
        metric_subtype: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Record a metric value.

        This method is used internally to store aggregated metrics.
        """
        try:
            self.dal.executesql(
                """INSERT INTO analytics_metrics_timeseries
                   (community_id, metric_type, metric_subtype, timestamp_bucket,
                    bucket_size, value, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (community_id, metric_type, metric_subtype, timestamp_bucket, bucket_size)
                   DO UPDATE SET value = EXCLUDED.value, metadata = EXCLUDED.metadata""",
                [community_id, metric_type, metric_subtype, timestamp,
                 bucket_size, value, metadata or {}]
            )
            self.dal.commit()
            return True

        except Exception as e:
            log_failure(self.logger, "Failed to record metric", e, community_id=community_id)
            raise
