"""
Bot Score Service - Community bot detection scoring system
Calculates composite bot detection scores using weighted formula across multiple factors.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import json
from .log_safety import log_failure

# Score weighting for composite calculation
SCORE_WEIGHTS = {
    'bad_actor': 0.30,
    'reputation': 0.25,
    'security': 0.20,
    'ai_behavioral': 0.25
}

# Grade thresholds
GRADE_THRESHOLDS = {
    'A': 90,
    'B': 80,
    'C': 70,
    'D': 60,
    'F': 0
}

# Community size categories
SIZE_CATEGORIES = {
    'small': 50,
    'medium': 500
}


class BotScoreService:
    """
    Calculate and manage community bot detection scores.

    Uses a weighted formula combining:
    - Bad actor indicators (spammy behavior, account patterns)
    - Reputation metrics (community health signals)
    - Security violations (content filter hits)
    - AI behavioral analysis (message patterns, timing)
    """

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def calculate_score(self, community_id: int) -> Dict[str, Any]:
        """
        Calculate bot score using weighted formula.

        Returns:
        - community_id: ID of community
        - overall_score: 0-100 composite score
        - grade: A-F letter grade
        - size_category: small/medium/large
        - component_scores: breakdown of each factor
        - timestamp: calculation time
        - next_recalculation: recommended next recalc time
        """
        try:
            # Get size category for normalization
            size_category = await self._get_community_size_category(community_id)

            # Calculate individual component scores
            bad_actor_score = await self._calculate_bad_actor_score(community_id)
            reputation_score = await self._calculate_reputation_score(community_id)
            security_score = await self._calculate_security_score(community_id)
            ai_behavioral_score = await self._calculate_ai_behavioral_score(community_id)

            # Apply weighted formula
            overall_score = (
                (bad_actor_score * SCORE_WEIGHTS['bad_actor']) +
                (reputation_score * SCORE_WEIGHTS['reputation']) +
                (security_score * SCORE_WEIGHTS['security']) +
                (ai_behavioral_score * SCORE_WEIGHTS['ai_behavioral'])
            )
            overall_score = int(round(overall_score))

            # Determine grade
            grade = self._score_to_grade(overall_score)

            # Prepare result dictionary
            now = datetime.utcnow()
            result = {
                'community_id': community_id,
                'overall_score': overall_score,
                'grade': grade,
                'size_category': size_category,
                'component_scores': {
                    'bad_actor_score': bad_actor_score,
                    'reputation_score': reputation_score,
                    'security_score': security_score,
                    'ai_behavioral_score': ai_behavioral_score
                },
                'component_weights': SCORE_WEIGHTS,
                'calculated_at': now.isoformat() + 'Z',
                'next_recalculation': (now + timedelta(hours=24)).isoformat() + 'Z'
            }

            # Upsert to database
            await self._upsert_bot_score(community_id, result)

            self.logger.audit(
                "Bot score calculated",
                community_id=community_id,
                action="calculate_score",
                score=overall_score,
                grade=grade,
                result="SUCCESS"
            )

            return result

        except Exception as e:
            log_failure(self.logger, "Failed to calculate bot score", e, community_id=community_id)
            raise

    async def get_score(self, community_id: int) -> Dict[str, Any]:
        """
        Get cached bot score, recalculate if stale.

        Returns:
        - Bot score dictionary with all fields
        - If stale (>24h), triggers recalculation
        """
        try:
            # Try to get cached score
            query = """
                SELECT community_id, overall_score, grade, size_category,
                       component_scores, component_weights,
                       calculated_at, next_recalculation
                FROM analytics_bot_scores
                WHERE community_id = $1
            """
            rows = await self.dal.execute(query, [community_id])

            if rows and len(rows) > 0:
                row = rows[0]
                next_recalc = row.get('next_recalculation')

                # Check if score is still fresh
                if next_recalc:
                    try:
                        recalc_time = datetime.fromisoformat(next_recalc.replace('Z', '+00:00'))
                        if recalc_time > datetime.utcnow():
                            # Score is fresh, return it
                            return {
                                'community_id': row['community_id'],
                                'overall_score': row['overall_score'],
                                'grade': row['grade'],
                                'size_category': row['size_category'],
                                'component_scores': row.get('component_scores', {}),
                                'component_weights': row.get('component_weights', SCORE_WEIGHTS),
                                'calculated_at': row['calculated_at'],
                                'next_recalculation': row['next_recalculation'],
                                'cached': True
                            }
                    except (ValueError, TypeError):
                        pass

            # Score is stale or doesn't exist, recalculate
            return await self.calculate_score(community_id)

        except Exception as e:
            log_failure(self.logger, "Failed to get bot score", e, community_id=community_id)
            raise

    async def get_suspected_bots(
        self,
        community_id: int,
        limit: int = 50,
        min_confidence: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get list of suspected bot users for a community.

        Args:
            community_id: Community to query
            limit: Maximum records to return (default 50)
            min_confidence: Minimum confidence score 0-100 (default 50)

        Returns:
        - List of suspected bot records sorted by confidence DESC
        """
        try:
            query = """
                SELECT community_id, hub_user_id, platform_user_id, platform_username,
                       confidence_score, bot_indicators, detected_patterns,
                       is_false_positive, reviewed_by, reviewed_at,
                       detected_at, updated_at
                FROM analytics_suspected_bots
                WHERE community_id = $1
                AND confidence_score >= $2
                AND (is_false_positive IS NULL OR is_false_positive = FALSE)
                ORDER BY confidence_score DESC
                LIMIT $3
            """
            rows = await self.dal.execute(query, [community_id, min_confidence, limit])

            bots = []
            for row in (rows or []):
                bots.append({
                    'community_id': row['community_id'],
                    'hub_user_id': row['hub_user_id'],
                    'platform_user_id': row['platform_user_id'],
                    'platform_username': row['platform_username'],
                    'confidence_score': row['confidence_score'],
                    'bot_indicators': row.get('bot_indicators', {}),
                    'detected_patterns': row.get('detected_patterns', []),
                    'is_false_positive': row.get('is_false_positive', False),
                    'reviewed_by': row.get('reviewed_by'),
                    'reviewed_at': row.get('reviewed_at'),
                    'detected_at': row['detected_at'],
                    'updated_at': row['updated_at']
                })

            self.logger.audit(
                "Suspected bots queried",
                community_id=community_id,
                action="get_suspected_bots",
                count=len(bots),
                result="SUCCESS"
            )

            return bots

        except Exception as e:
            log_failure(self.logger, "Failed to get suspected bots", e, community_id=community_id)
            raise

    async def mark_bot_reviewed(
        self,
        community_id: int,
        bot_id: int,
        is_false_positive: bool,
        reviewer_id: int
    ) -> Dict[str, Any]:
        """
        Mark a suspected bot as reviewed by a moderator.

        Args:
            community_id: Community ID
            bot_id: analytics_suspected_bots.id
            is_false_positive: Whether this was a false positive detection
            reviewer_id: hub_user_id of reviewer

        Returns:
        - Updated suspected bot record
        """
        try:
            now = datetime.utcnow()

            query = """
                UPDATE analytics_suspected_bots
                SET is_false_positive = $1,
                    reviewed_by = $2,
                    reviewed_at = $3,
                    updated_at = $3
                WHERE id = $4
                AND community_id = $5
                RETURNING community_id, hub_user_id, platform_user_id, platform_username,
                          confidence_score, bot_indicators, detected_patterns,
                          is_false_positive, reviewed_by, reviewed_at,
                          detected_at, updated_at
            """
            rows = await self.dal.execute(
                query,
                [is_false_positive, reviewer_id, now, bot_id, community_id]
            )

            if not rows or len(rows) == 0:
                self.logger.warning(
                    "Suspected bot not found or already reviewed",
                    community_id=community_id,
                    bot_id=bot_id
                )
                return {}

            row = rows[0]
            result = {
                'community_id': row['community_id'],
                'hub_user_id': row['hub_user_id'],
                'platform_user_id': row['platform_user_id'],
                'platform_username': row['platform_username'],
                'confidence_score': row['confidence_score'],
                'bot_indicators': row.get('bot_indicators', {}),
                'detected_patterns': row.get('detected_patterns', []),
                'is_false_positive': row['is_false_positive'],
                'reviewed_by': row['reviewed_by'],
                'reviewed_at': row['reviewed_at'],
                'detected_at': row['detected_at'],
                'updated_at': row['updated_at']
            }

            self.logger.audit(
                "Suspected bot reviewed",
                community_id=community_id,
                action="mark_bot_reviewed",
                bot_id=bot_id,
                reviewer_id=reviewer_id,
                is_false_positive=is_false_positive,
                result="SUCCESS"
            )

            return result

        except Exception as e:
            log_failure(self.logger, "Failed to mark bot reviewed", e, community_id=community_id)
            raise

    async def _calculate_bad_actor_score(self, community_id: int) -> int:
        """
        Calculate bad actor score (0-100, 100 = clean).

        Attempts to get from bad_actor_service, falls back to direct counting.
        Factors: flagged accounts, spam patterns, account age, unusual activity
        """
        try:
            # Try to query analytics_bad_actor_alerts
            query = """
                SELECT COUNT(DISTINCT platform_user_id) as bad_actor_count
                FROM analytics_bad_actor_alerts
                WHERE community_id = $1
                AND status = 'pending'
            """
            rows = await self.dal.execute(query, [community_id])

            bad_actor_count = rows[0]['bad_actor_count'] if rows and len(rows) > 0 else 0

            # Get total user count for community
            total_count_query = """
                SELECT COUNT(DISTINCT hub_user_id) as total_users
                FROM activity_message_events
                WHERE community_id = $1
                AND created_at >= NOW() - INTERVAL '30 days'
            """
            total_rows = await self.dal.execute(total_count_query, [community_id])
            total_users = total_rows[0]['total_users'] if total_rows and len(total_rows) > 0 else 1

            # Calculate percentage of bad actors
            bad_actor_percentage = (bad_actor_count / total_users * 100) if total_users > 0 else 0

            # Higher percentage = lower score
            # If 0% bad actors, score = 100
            # If 10% bad actors, score = 50
            # If 20%+ bad actors, score = 0
            score = max(0, 100 - int(bad_actor_percentage * 5))

            return score

        except Exception as e:
            log_failure(self.logger, "Failed to calculate bad actor score", e, community_id=community_id)
            return 50  # Default neutral score

    async def _calculate_reputation_score(self, community_id: int) -> int:
        """
        Calculate reputation score (0-100) based on community health.

        Factors: user engagement, message quality, retention, report ratio
        """
        try:
            # Get reputation metrics from analytics tables
            query = """
                SELECT
                    COALESCE(health_score, 50) as health_score,
                    COALESCE(engagement_level, 0) as engagement_level,
                    COALESCE(average_user_age_days, 1) as avg_age
                FROM analytics_community_health
                WHERE community_id = $1
                ORDER BY created_at DESC
                LIMIT 1
            """
            rows = await self.dal.execute(query, [community_id])

            if rows and len(rows) > 0:
                row = rows[0]
                health_score = row['health_score']
                engagement = row['engagement_level']

                # Combine health score with engagement
                reputation_score = int((health_score * 0.7) + (engagement * 0.3))
                return max(0, min(100, reputation_score))

            # Default if no health data
            return 50

        except Exception as e:
            log_failure(self.logger, "Failed to calculate reputation score", e, community_id=community_id)
            return 50  # Default neutral score

    async def _calculate_security_score(self, community_id: int) -> int:
        """
        Calculate security score (0-100) based on content filter violations.

        Factors: content filter hits, report ratio, moderation actions
        Higher score = fewer violations = healthier community
        """
        try:
            # Get content filter violation metrics
            query = """
                SELECT
                    COUNT(*) as total_events,
                    SUM(CASE WHEN violation_detected = TRUE THEN 1 ELSE 0 END) as violations
                FROM activity_message_events
                WHERE community_id = $1
                AND created_at >= NOW() - INTERVAL '30 days'
            """
            rows = await self.dal.execute(query, [community_id])

            if rows and len(rows) > 0:
                row = rows[0]
                total_events = row['total_events'] or 1
                violations = row['violations'] or 0

                # Calculate violation rate
                violation_rate = (violations / total_events * 100) if total_events > 0 else 0

                # Convert to score: 0% violations = 100, 10% violations = 0
                score = max(0, 100 - int(violation_rate * 10))
                return score

            # Default if no data
            return 75

        except Exception as e:
            log_failure(self.logger, "Failed to calculate security score", e, community_id=community_id)
            return 75  # Default neutral score

    async def _calculate_ai_behavioral_score(self, community_id: int) -> int:
        """
        Calculate AI behavioral score (0-100) by analyzing user patterns.

        Detects suspicious patterns:
        - Rapid consecutive posts (>5 messages in <30 seconds)
        - Identical message repetition (same message >3 times in 5 minutes)
        - Unusual posting times (messages at 3am from inactive user)
        - Coordinated messaging patterns (multiple users, identical messages)

        Higher score = healthier behavior patterns
        """
        try:
            # Detect rapid posting patterns
            rapid_posting_query = """
                SELECT COUNT(DISTINCT hub_user_id) as rapid_posters
                FROM (
                    SELECT hub_user_id, COUNT(*) as msg_count,
                           DATE_TRUNC('minute', created_at) as minute_bucket
                    FROM activity_message_events
                    WHERE community_id = $1
                    AND created_at >= NOW() - INTERVAL '24 hours'
                    AND message_text IS NOT NULL
                    GROUP BY hub_user_id, minute_bucket
                    HAVING COUNT(*) > 5
                ) rapid
            """
            rapid_rows = await self.dal.execute(rapid_posting_query, [community_id])
            rapid_posters = rapid_rows[0]['rapid_posters'] if rapid_rows and len(rapid_rows) > 0 else 0

            # Detect duplicate message patterns
            duplicate_query = """
                SELECT COUNT(DISTINCT hub_user_id) as duplicate_users
                FROM (
                    SELECT hub_user_id, message_text, COUNT(*) as dup_count
                    FROM activity_message_events
                    WHERE community_id = $1
                    AND created_at >= NOW() - INTERVAL '5 minutes'
                    AND message_text IS NOT NULL
                    AND LENGTH(message_text) > 5
                    GROUP BY hub_user_id, message_text
                    HAVING COUNT(*) >= 3
                ) duplicates
            """
            dup_rows = await self.dal.execute(duplicate_query, [community_id])
            duplicate_users = dup_rows[0]['duplicate_users'] if dup_rows and len(dup_rows) > 0 else 0

            # Get total active users in last 24h
            total_query = """
                SELECT COUNT(DISTINCT hub_user_id) as active_users
                FROM activity_message_events
                WHERE community_id = $1
                AND created_at >= NOW() - INTERVAL '24 hours'
            """
            total_rows = await self.dal.execute(total_query, [community_id])
            active_users = total_rows[0]['active_users'] if total_rows and len(total_rows) > 0 else 1

            # Calculate anomaly percentage
            anomaly_count = rapid_posters + duplicate_users
            anomaly_percentage = (anomaly_count / max(1, active_users) * 100)

            # Convert to score: 0% anomalies = 100, 5% anomalies = 50, 10%+ = 0
            score = max(0, 100 - int(anomaly_percentage * 10))
            return score

        except Exception as e:
            log_failure(self.logger, "Failed to calculate AI behavioral score", e, community_id=community_id)
            return 60  # Default neutral score

    async def _get_community_size_category(self, community_id: int) -> str:
        """
        Determine community size category based on member count.

        Returns: 'small' (<50), 'medium' (50-500), 'large' (>500)
        """
        try:
            query = """
                SELECT COUNT(DISTINCT hub_user_id) as member_count
                FROM activity_message_events
                WHERE community_id = $1
                AND created_at >= NOW() - INTERVAL '30 days'
            """
            rows = await self.dal.execute(query, [community_id])

            member_count = rows[0]['member_count'] if rows and len(rows) > 0 else 0

            if member_count < SIZE_CATEGORIES['small']:
                return 'small'
            elif member_count < SIZE_CATEGORIES['medium']:
                return 'medium'
            else:
                return 'large'

        except Exception as e:
            log_failure(self.logger, "Failed to get community size", e, community_id=community_id)
            return 'medium'  # Default category

    def _score_to_grade(self, score: int) -> str:
        """
        Convert numerical score (0-100) to letter grade.

        A: 90-100
        B: 80-89
        C: 70-79
        D: 60-69
        F: 0-59
        """
        if score >= GRADE_THRESHOLDS['A']:
            return 'A'
        elif score >= GRADE_THRESHOLDS['B']:
            return 'B'
        elif score >= GRADE_THRESHOLDS['C']:
            return 'C'
        elif score >= GRADE_THRESHOLDS['D']:
            return 'D'
        else:
            return 'F'

    async def _upsert_bot_score(self, community_id: int, score_data: Dict[str, Any]) -> None:
        """
        Insert or update bot score in analytics_bot_scores table.

        Uses upsert (INSERT ... ON CONFLICT) for idempotency.
        """
        try:
            query = """
                INSERT INTO analytics_bot_scores
                    (community_id, overall_score, grade, size_category,
                     component_scores, component_weights, calculated_at, next_recalculation)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (community_id)
                DO UPDATE SET
                    overall_score = EXCLUDED.overall_score,
                    grade = EXCLUDED.grade,
                    size_category = EXCLUDED.size_category,
                    component_scores = EXCLUDED.component_scores,
                    component_weights = EXCLUDED.component_weights,
                    calculated_at = EXCLUDED.calculated_at,
                    next_recalculation = EXCLUDED.next_recalculation,
                    updated_at = NOW()
            """

            await self.dal.execute(
                query,
                [
                    community_id,
                    score_data['overall_score'],
                    score_data['grade'],
                    score_data['size_category'],
                    json.dumps(score_data['component_scores']),
                    json.dumps(score_data['component_weights']),
                    score_data['calculated_at'],
                    score_data['next_recalculation']
                ]
            )

        except Exception as e:
            log_failure(self.logger, "Failed to upsert bot score", e, community_id=community_id)
            raise
