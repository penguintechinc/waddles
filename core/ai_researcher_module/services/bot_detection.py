"""
AI Researcher Bot Detection Service
====================================

Analyzes user behavior patterns to detect potential bots.

Features:
- Multi-signal bot detection analysis
- Premium vs free tier feature gating
- Behavioral pattern analysis
- Confidence scoring and recommendations
- Historical tracking and review workflow

Detection Signals:
- timing_regularity: Standard deviation of message intervals (lower = more bot-like)
- response_latency_avg: How fast they respond after others
- emote_text_ratio: Emotes vs text balance
- copy_paste_frequency: Same messages from multiple users
- vocabulary_diversity: Unique words / total words
- account_age_days: Platform account age (if available)
"""

import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import statistics
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'libs'))
from flask_core import describe_db_error, setup_aaa_logging  # noqa: E402

logger = setup_aaa_logging('ai_researcher_bot_detection', '1.0.0')


@dataclass
class BotDetectionResult:
    """
    Result of bot detection analysis for a user.
    """
    user_id: str
    username: str
    confidence_score: float  # 0-100
    signals: Dict[str, Any] = field(default_factory=dict)
    recommended_action: str = "none"  # none/monitor/warn/timeout/ban

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'user_id': self.user_id,
            'username': self.username,
            'confidence_score': round(self.confidence_score, 2),
            'signals': self.signals,
            'recommended_action': self.recommended_action
        }


class BotDetectionService:
    """
    Service for detecting bot-like behavior in chat users.
    """

    # Thresholds for bot detection signals
    THRESHOLDS = {
        'timing_regularity_high': 5.0,      # Std dev < 5 seconds = very regular
        'timing_regularity_medium': 15.0,   # Std dev < 15 seconds = regular
        'response_latency_fast': 1000,      # < 1 second = very fast
        'response_latency_medium': 3000,    # < 3 seconds = fast
        'emote_ratio_high': 0.8,            # > 80% emotes = suspicious
        'vocabulary_diversity_low': 0.3,    # < 30% unique words = low diversity
        'copy_paste_min_users': 3,          # Same message from 3+ users
        'min_messages': 10                  # Minimum messages for analysis
    }

    # Action thresholds based on confidence score
    ACTION_THRESHOLDS = {
        'monitor': 50,   # 50-69: monitor
        'warn': 70,      # 70-84: warn
        'timeout': 85,   # 85-94: timeout
        'ban': 95        # 95+: ban
    }

    def __init__(self, db_connection, is_premium: bool = False):
        """
        Initialize bot detection service.

        Args:
            db_connection: Database connection (AsyncDAL)
            is_premium: Whether community has premium tier (enables detailed signals)
        """
        self.db = db_connection
        self.is_premium = is_premium
        logger.system(
            "BotDetectionService initialized",
            result="SUCCESS",
            is_premium=is_premium
        )

    async def analyze_users(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime
    ) -> List[BotDetectionResult]:
        """
        Analyze all users in a community for bot-like behavior.

        Args:
            community_id: Community ID
            period_start: Start of analysis period
            period_end: End of analysis period

        Returns:
            List of BotDetectionResult objects
        """
        try:
            logger.system(
                "Starting bot detection analysis",
                action="analyze_users",
                community=community_id
            )

            # Get all messages for the period
            messages = await self._get_messages_for_period(
                community_id,
                period_start,
                period_end
            )

            if not messages:
                logger.system(
                    "No messages found for analysis period",
                    community=community_id,
                    result="EMPTY"
                )
                return []

            # Group messages by user
            user_messages = self._group_messages_by_user(messages)

            # Analyze each user
            results = []
            for user_id, user_msgs in user_messages.items():
                # Skip users with too few messages
                if len(user_msgs) < self.THRESHOLDS['min_messages']:
                    continue

                result = await self.analyze_user_messages(
                    user_id,
                    user_msgs,
                    all_messages=messages
                )
                results.append(result)

            # Sort by confidence score (highest first)
            results.sort(key=lambda x: x.confidence_score, reverse=True)

            logger.audit(
                action="bot_detection_analysis_complete",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                users_analyzed=len(user_messages),
                flagged_users=len([r for r in results if r.confidence_score >= 50])
            )

            return results

        except Exception as e:
            logger.error(
                f"Failed to analyze users: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    async def analyze_user(
        self,
        community_id: int,
        user_id: str
    ) -> BotDetectionResult:
        """
        Analyze a specific user for bot-like behavior.

        Args:
            community_id: Community ID
            user_id: Platform user ID

        Returns:
            BotDetectionResult for the user
        """
        try:
            logger.system(
                "Analyzing specific user",
                action="analyze_user",
                community=community_id,
                user=user_id
            )

            # Get messages for last 30 days
            period_end = datetime.utcnow()
            period_start = period_end - timedelta(days=30)

            # Get user's messages
            user_messages = await self._get_user_messages(
                community_id,
                user_id,
                period_start,
                period_end
            )

            # Get all messages for context
            all_messages = await self._get_messages_for_period(
                community_id,
                period_start,
                period_end
            )

            if not user_messages:
                logger.system(
                    "No messages found for user",
                    community=community_id,
                    user=user_id,
                    result="EMPTY"
                )
                return BotDetectionResult(
                    user_id=user_id,
                    username="Unknown",
                    confidence_score=0,
                    signals={},
                    recommended_action="none"
                )

            result = await self.analyze_user_messages(
                user_id,
                user_messages,
                all_messages
            )

            logger.audit(
                action="user_analyzed",
                user=user_id,
                community=str(community_id),
                result="SUCCESS",
                confidence_score=result.confidence_score
            )

            return result

        except Exception as e:
            logger.error(
                f"Failed to analyze user: {describe_db_error(e)}",
                community=community_id,
                user=user_id,
                error=describe_db_error(e)
            )
            raise

    async def analyze_user_messages(
        self,
        user_id: str,
        user_messages: List[dict],
        all_messages: List[dict]
    ) -> BotDetectionResult:
        """
        Analyze messages for a single user.

        Args:
            user_id: Platform user ID
            user_messages: List of messages from this user
            all_messages: All messages in the period (for context)

        Returns:
            BotDetectionResult
        """
        username = user_messages[0].get('platform_username', 'Unknown') if user_messages else 'Unknown'

        # Calculate all signals
        signals = await self.calculate_signals(user_messages, all_messages)

        # Calculate confidence score
        confidence_score = self._calculate_confidence_score(signals)

        # Determine recommended action
        recommended_action = self._determine_action(confidence_score)

        return BotDetectionResult(
            user_id=user_id,
            username=username,
            confidence_score=confidence_score,
            signals=signals if self.is_premium else {'summary_score': confidence_score},
            recommended_action=recommended_action
        )

    async def calculate_signals(
        self,
        messages: List[dict],
        all_messages: Optional[List[dict]] = None
    ) -> dict:
        """
        Calculate bot detection signals from message history.

        Args:
            messages: User's messages
            all_messages: All messages in period (for context)

        Returns:
            Dict of signal values
        """
        signals = {}

        if not self.is_premium:
            # Free tier: only return summary score
            return signals

        # Premium tier: calculate detailed signals

        # 1. Timing regularity: Std dev of message intervals
        signals['timing_regularity'] = self._calculate_timing_regularity(messages)

        # 2. Response latency: How fast they respond to others
        signals['response_latency_avg'] = self._calculate_response_latency(
            messages,
            all_messages or []
        )

        # 3. Emote vs text ratio
        signals['emote_text_ratio'] = self._calculate_emote_ratio(messages)

        # 4. Copy-paste frequency
        signals['copy_paste_frequency'] = self._calculate_copy_paste_frequency(
            messages,
            all_messages or []
        )

        # 5. Vocabulary diversity
        signals['vocabulary_diversity'] = self._calculate_vocabulary_diversity(messages)

        # 6. Account age (if available in metadata)
        signals['account_age_days'] = self._extract_account_age(messages)

        return signals

    async def save_results(
        self,
        community_id: int,
        insight_id: int,
        results: List[BotDetectionResult]
    ) -> None:
        """
        Save bot detection results to database.

        Args:
            community_id: Community ID
            insight_id: Related insight ID
            results: List of BotDetectionResult objects
        """
        try:
            for result in results:
                # Extract signals for premium users
                behavioral_patterns = result.signals if self.is_premium else {}
                timing_regularity = result.signals.get('timing_regularity')
                response_latency_avg = result.signals.get('response_latency_avg')
                emote_text_ratio = result.signals.get('emote_text_ratio')
                copy_paste_frequency = result.signals.get('copy_paste_frequency', 0)
                account_age_days = result.signals.get('account_age_days')

                query = """
                    INSERT INTO ai_bot_detection_results (
                        community_id, insight_id, platform, platform_user_id,
                        platform_username, confidence_score, behavioral_patterns,
                        timing_regularity, response_latency_avg, emote_text_ratio,
                        copy_paste_frequency, account_age_days, recommended_action
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """

                await self.db.execute(
                    query,
                    [
                        community_id,
                        insight_id,
                        'unknown',  # Platform would be extracted from messages
                        result.user_id,
                        result.username,
                        result.confidence_score,
                        json.dumps(behavioral_patterns),
                        timing_regularity,
                        response_latency_avg,
                        emote_text_ratio,
                        copy_paste_frequency,
                        account_age_days,
                        result.recommended_action
                    ]
                )

            logger.audit(
                action="bot_detection_results_saved",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                results_count=len(results),
                insight_id=insight_id
            )

        except Exception as e:
            logger.error(
                f"Failed to save bot detection results: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    async def get_at_risk_users(
        self,
        community_id: int,
        threshold: int = 85
    ) -> List[BotDetectionResult]:
        """
        Get users at risk of being bots (above threshold).

        Args:
            community_id: Community ID
            threshold: Minimum confidence score (default: 85)

        Returns:
            List of BotDetectionResult objects
        """
        try:
            query = """
                SELECT platform_user_id, platform_username, confidence_score,
                       behavioral_patterns, timing_regularity, response_latency_avg,
                       emote_text_ratio, copy_paste_frequency, account_age_days,
                       recommended_action
                FROM ai_bot_detection_results
                WHERE community_id = $1
                  AND confidence_score >= $2
                  AND is_reviewed = FALSE
                ORDER BY confidence_score DESC
            """

            rows = await self.db.execute(query, [community_id, threshold])

            results = []
            for row in rows:
                signals = json.loads(row['behavioral_patterns']) if row['behavioral_patterns'] else {}
                if self.is_premium:
                    signals.update({
                        'timing_regularity': row['timing_regularity'],
                        'response_latency_avg': row['response_latency_avg'],
                        'emote_text_ratio': row['emote_text_ratio'],
                        'copy_paste_frequency': row['copy_paste_frequency'],
                        'account_age_days': row['account_age_days']
                    })

                results.append(BotDetectionResult(
                    user_id=row['platform_user_id'],
                    username=row['platform_username'],
                    confidence_score=float(row['confidence_score']),
                    signals=signals,
                    recommended_action=row['recommended_action']
                ))

            logger.system(
                f"Retrieved {len(results)} at-risk users",
                community=community_id,
                threshold=threshold,
                result="SUCCESS"
            )

            return results

        except Exception as e:
            logger.error(
                f"Failed to get at-risk users: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    # =========================================================================
    # SIGNAL CALCULATION METHODS
    # =========================================================================

    def _calculate_timing_regularity(self, messages: List[dict]) -> Optional[float]:
        """
        Calculate standard deviation of message intervals.
        Lower value = more regular = more bot-like.
        """
        if len(messages) < 2:
            return None

        timestamps = [msg['created_at'] for msg in messages]
        timestamps.sort()

        intervals = []
        for i in range(1, len(timestamps)):
            interval = (timestamps[i] - timestamps[i-1]).total_seconds()
            intervals.append(interval)

        if len(intervals) < 2:
            return None

        try:
            std_dev = statistics.stdev(intervals)
            return round(std_dev, 2)
        except statistics.StatisticsError:
            return None

    def _calculate_response_latency(
        self,
        user_messages: List[dict],
        all_messages: List[dict]
    ) -> Optional[float]:
        """
        Calculate average response latency (ms).
        How fast user responds after others post.
        """
        if not all_messages or len(user_messages) < 2:
            return None

        latencies = []

        for msg in user_messages:
            msg_time = msg['created_at']

            # Find the most recent message before this one (from another user)
            prev_messages = [
                m for m in all_messages
                if m['created_at'] < msg_time
                and m['platform_user_id'] != msg['platform_user_id']
            ]

            if prev_messages:
                prev_msg = max(prev_messages, key=lambda x: x['created_at'])
                latency = (msg_time - prev_msg['created_at']).total_seconds() * 1000
                latencies.append(latency)

        if not latencies:
            return None

        return round(sum(latencies) / len(latencies), 2)

    def _calculate_emote_ratio(self, messages: List[dict]) -> float:
        """
        Calculate ratio of emotes to text.
        Higher ratio = more emotes = potentially suspicious.
        """
        if not messages:
            return 0.0

        emote_count = 0
        text_count = 0

        for msg in messages:
            content = msg.get('message_content', '')

            # Simple heuristic: count words with : as emotes
            words = content.split()
            for word in words:
                if word.startswith(':') and word.endswith(':'):
                    emote_count += 1
                else:
                    text_count += 1

        total = emote_count + text_count
        if total == 0:
            return 0.0

        return round(emote_count / total, 2)

    def _calculate_copy_paste_frequency(
        self,
        user_messages: List[dict],
        all_messages: List[dict]
    ) -> int:
        """
        Count how many of user's messages appear to be copy-pasted.
        (Same message from multiple users = copy-paste)
        """
        if not all_messages:
            return 0

        copy_paste_count = 0

        for msg in user_messages:
            content = msg.get('message_content', '').strip().lower()
            if not content or len(content) < 10:
                continue

            # Count how many other users posted the same message
            same_message_users = set()
            for other_msg in all_messages:
                other_content = other_msg.get('message_content', '').strip().lower()
                other_user = other_msg.get('platform_user_id')

                if other_content == content and other_user != msg['platform_user_id']:
                    same_message_users.add(other_user)

            if len(same_message_users) >= self.THRESHOLDS['copy_paste_min_users']:
                copy_paste_count += 1

        return copy_paste_count

    def _calculate_vocabulary_diversity(self, messages: List[dict]) -> float:
        """
        Calculate vocabulary diversity (unique words / total words).
        Lower diversity = more repetitive = potentially bot-like.
        """
        if not messages:
            return 0.0

        all_words = []
        for msg in messages:
            content = msg.get('message_content', '').lower()
            words = content.split()
            all_words.extend(words)

        if not all_words:
            return 0.0

        unique_words = len(set(all_words))
        total_words = len(all_words)

        return round(unique_words / total_words, 2)

    def _extract_account_age(self, messages: List[dict]) -> Optional[int]:
        """
        Extract account age in days from metadata if available.
        """
        if not messages:
            return None

        # Check first message metadata for account_created_at
        metadata = messages[0].get('metadata', {})
        if isinstance(metadata, dict):
            account_created = metadata.get('account_created_at')
            if account_created:
                try:
                    created = datetime.fromisoformat(account_created)
                    age_days = (datetime.utcnow() - created).days
                    return age_days
                except (ValueError, TypeError):
                    pass

        return None

    # =========================================================================
    # SCORING AND ACTION METHODS
    # =========================================================================

    def _calculate_confidence_score(self, signals: dict) -> float:
        """
        Calculate overall bot confidence score (0-100).
        Weighted combination of all signals.
        """
        if not self.is_premium or not signals:
            # Free tier: basic heuristic
            return 0.0

        score = 0.0
        signal_count = 0

        # Timing regularity (30% weight)
        timing = signals.get('timing_regularity')
        if timing is not None:
            if timing < self.THRESHOLDS['timing_regularity_high']:
                score += 30
            elif timing < self.THRESHOLDS['timing_regularity_medium']:
                score += 15
            signal_count += 1

        # Response latency (20% weight)
        latency = signals.get('response_latency_avg')
        if latency is not None:
            if latency < self.THRESHOLDS['response_latency_fast']:
                score += 20
            elif latency < self.THRESHOLDS['response_latency_medium']:
                score += 10
            signal_count += 1

        # Emote ratio (15% weight)
        emote_ratio = signals.get('emote_text_ratio', 0)
        if emote_ratio > self.THRESHOLDS['emote_ratio_high']:
            score += 15
        signal_count += 1

        # Copy-paste frequency (20% weight)
        copy_paste = signals.get('copy_paste_frequency', 0)
        if copy_paste > 0:
            score += min(20, copy_paste * 5)  # Cap at 20
        signal_count += 1

        # Vocabulary diversity (15% weight)
        vocab_diversity = signals.get('vocabulary_diversity', 1.0)
        if vocab_diversity < self.THRESHOLDS['vocabulary_diversity_low']:
            score += 15
        signal_count += 1

        # Normalize score if fewer signals available
        if signal_count > 0:
            score = min(100, score)

        return round(score, 2)

    def _determine_action(self, confidence_score: float) -> str:
        """
        Determine recommended action based on confidence score.
        """
        if confidence_score >= self.ACTION_THRESHOLDS['ban']:
            return 'ban'
        elif confidence_score >= self.ACTION_THRESHOLDS['timeout']:
            return 'timeout'
        elif confidence_score >= self.ACTION_THRESHOLDS['warn']:
            return 'warn'
        elif confidence_score >= self.ACTION_THRESHOLDS['monitor']:
            return 'monitor'
        else:
            return 'none'

    # =========================================================================
    # DATABASE HELPER METHODS
    # =========================================================================

    async def _get_messages_for_period(
        self,
        community_id: int,
        start: datetime,
        end: datetime
    ) -> List[dict]:
        """Get all messages for a time period."""
        query = """
            SELECT id, platform, platform_user_id, platform_username,
                   message_content, message_type, metadata, created_at
            FROM ai_context_messages
            WHERE community_id = $1
              AND created_at >= $2
              AND created_at <= $3
            ORDER BY created_at ASC
        """

        rows = await self.db.execute(query, [community_id, start, end])
        return [dict(row) for row in rows]

    async def _get_user_messages(
        self,
        community_id: int,
        user_id: str,
        start: datetime,
        end: datetime
    ) -> List[dict]:
        """Get messages for a specific user."""
        query = """
            SELECT id, platform, platform_user_id, platform_username,
                   message_content, message_type, metadata, created_at
            FROM ai_context_messages
            WHERE community_id = $1
              AND platform_user_id = $2
              AND created_at >= $3
              AND created_at <= $4
            ORDER BY created_at ASC
        """

        rows = await self.db.execute(query, [community_id, user_id, start, end])
        return [dict(row) for row in rows]

    def _group_messages_by_user(self, messages: List[dict]) -> Dict[str, List[dict]]:
        """Group messages by user ID."""
        user_messages = {}
        for msg in messages:
            user_id = msg.get('platform_user_id')
            if user_id:
                if user_id not in user_messages:
                    user_messages[user_id] = []
                user_messages[user_id].append(msg)
        return user_messages
