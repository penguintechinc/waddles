"""
Sentiment Analysis Service for AI Researcher Module
====================================================

Analyzes community sentiment and emotional trends over time.

Features:
- Message-level sentiment classification
- Sentiment trend analysis
- Emotional state tracking
- Community mood prediction
- Sentiment-based anomaly detection
"""

import logging
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SentimentResult:
    """Result from sentiment analysis"""
    success: bool
    overall_sentiment: str
    sentiment_score: float
    message_count: int
    sentiment_distribution: Dict[str, int]
    trends: List[Dict[str, Any]]
    processing_time_ms: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'success': self.success,
            'overall_sentiment': self.overall_sentiment,
            'sentiment_score': self.sentiment_score,
            'message_count': self.message_count,
            'sentiment_distribution': self.sentiment_distribution,
            'trends': self.trends,
            'processing_time_ms': self.processing_time_ms,
            'error': self.error
        }


class SentimentAnalyzer:
    """
    Service for analyzing community sentiment.

    Analyzes:
    - Individual message sentiment
    - Temporal sentiment trends
    - Community mood and emotional state
    - Sentiment shifts and anomalies
    """

    SENTIMENT_KEYWORDS = {
        'positive': [
            'love', 'great', 'awesome', 'amazing', 'excellent', 'wonderful',
            'fantastic', 'brilliant', 'good', 'nice', 'cool', 'happy', 'glad',
            'thanks', 'appreciate', 'grateful', 'perfect', 'best', 'beautiful'
        ],
        'negative': [
            'hate', 'terrible', 'awful', 'horrible', 'bad', 'worse', 'worst',
            'stupid', 'dumb', 'annoying', 'disappointed', 'upset', 'angry',
            'frustrated', 'sad', 'depressed', 'ugly', 'boring', 'waste'
        ],
        'neutral': [
            'ok', 'okay', 'fine', 'meh', 'whatever', 'so', 'like', 'just'
        ]
    }

    def __init__(self, dal, ai_provider=None):
        """
        Initialize sentiment analyzer.

        Args:
            dal: Database connection (AsyncDAL)
            ai_provider: Optional AI provider for advanced sentiment analysis
        """
        self.dal = dal
        self.ai_provider = ai_provider
        logger.info("SentimentAnalyzer initialized")

    async def analyze_sentiment(
        self,
        community_id: int,
        timeframe: str = '7d'
    ) -> SentimentResult:
        """
        Analyze community sentiment over a timeframe.

        Args:
            community_id: Community identifier
            timeframe: Analysis period ('1d', '7d', '30d', '90d')

        Returns:
            SentimentResult with analysis
        """
        start_time = time.time()

        try:
            logger.info(
                f"Analyzing community sentiment",
                extra={
                    'community_id': community_id,
                    'timeframe': timeframe
                }
            )

            # Calculate time window
            days = self._parse_timeframe(timeframe)
            period_start = datetime.utcnow() - timedelta(days=days)
            period_end = datetime.utcnow()

            # Get messages for analysis
            messages = await self._get_messages_for_period(
                community_id,
                period_start,
                period_end
            )

            if not messages:
                logger.warning(
                    "No messages found for sentiment analysis",
                    extra={'community_id': community_id}
                )
                return SentimentResult(
                    success=True,
                    overall_sentiment='neutral',
                    sentiment_score=0.5,
                    message_count=0,
                    sentiment_distribution={'positive': 0, 'negative': 0, 'neutral': 0},
                    trends=[],
                    processing_time_ms=int((time.time() - start_time) * 1000)
                )

            # Analyze sentiment for each message
            sentiments = []
            for msg in messages:
                sentiment = await self._classify_message_sentiment(msg)
                sentiments.append(sentiment)

            # Calculate overall sentiment
            overall_sentiment = self._calculate_overall_sentiment(sentiments)
            sentiment_score = self._calculate_sentiment_score(sentiments)

            # Get sentiment distribution
            distribution = self._get_sentiment_distribution(sentiments)

            # Analyze trends
            trends = await self._analyze_sentiment_trends(
                community_id,
                period_start,
                period_end,
                days
            )

            # Store sentiment analysis
            await self._store_sentiment_analysis(
                community_id,
                overall_sentiment,
                sentiment_score,
                distribution,
                timeframe
            )

            processing_time = int((time.time() - start_time) * 1000)

            logger.info(
                f"Sentiment analysis completed",
                extra={
                    'community_id': community_id,
                    'overall_sentiment': overall_sentiment,
                    'message_count': len(messages),
                    'processing_time_ms': processing_time
                }
            )

            return SentimentResult(
                success=True,
                overall_sentiment=overall_sentiment,
                sentiment_score=sentiment_score,
                message_count=len(messages),
                sentiment_distribution=distribution,
                trends=trends,
                processing_time_ms=processing_time
            )

        except Exception as e:
            logger.error(
                f"Sentiment analysis error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return SentimentResult(
                success=False,
                overall_sentiment='error',
                sentiment_score=0.0,
                message_count=0,
                sentiment_distribution={},
                trends=[],
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=describe_db_error(e)
            )

    async def _get_messages_for_period(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime,
        limit: int = 10000
    ) -> List[Dict[str, Any]]:
        """
        Get messages for a time period.

        Args:
            community_id: Community identifier
            period_start: Period start
            period_end: Period end
            limit: Maximum messages to retrieve

        Returns:
            List of message dicts
        """
        try:
            query = """
                SELECT id, message_content, platform_user_id,
                       platform_username, created_at
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= $2
                  AND created_at <= $3
                ORDER BY created_at DESC
                LIMIT $4
            """

            rows = await self.dal.execute(query, [
                community_id,
                period_start,
                period_end,
                limit
            ])

            if not rows:
                return []

            messages = []
            for row in rows:
                messages.append({
                    'id': row['id'],
                    'content': row['message_content'] or '',
                    'user_id': row['platform_user_id'],
                    'username': row['platform_username'],
                    'timestamp': row['created_at']
                })

            return messages

        except Exception as e:
            logger.error(f"Get messages error: {describe_db_error(e)}")
            return []

    async def _classify_message_sentiment(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Classify sentiment of a single message.

        Args:
            message: Message dict with 'content' key

        Returns:
            Dict with sentiment classification
        """
        try:
            content = (message.get('content') or '').lower()

            # Keyword-based sentiment (simple approach)
            positive_count = sum(1 for word in self.SENTIMENT_KEYWORDS['positive']
                                if word in content)
            negative_count = sum(1 for word in self.SENTIMENT_KEYWORDS['negative']
                                if word in content)

            if positive_count > negative_count:
                sentiment = 'positive'
            elif negative_count > positive_count:
                sentiment = 'negative'
            else:
                sentiment = 'neutral'

            # Calculate confidence score (simple)
            total_sentiment_words = positive_count + negative_count
            confidence = min(total_sentiment_words / 2.0, 1.0) if total_sentiment_words > 0 else 0.5

            return {
                'sentiment': sentiment,
                'confidence': confidence,
                'positive_words': positive_count,
                'negative_words': negative_count
            }

        except Exception as e:
            logger.error(f"Message sentiment classification error: {describe_db_error(e)}")
            return {'sentiment': 'neutral', 'confidence': 0.0}

    def _calculate_overall_sentiment(self, sentiments: List[Dict[str, Any]]) -> str:
        """
        Calculate overall community sentiment.

        Args:
            sentiments: List of sentiment classifications

        Returns:
            Overall sentiment classification
        """
        if not sentiments:
            return 'neutral'

        positive = sum(1 for s in sentiments if s.get('sentiment') == 'positive')
        negative = sum(1 for s in sentiments if s.get('sentiment') == 'negative')
        total = len(sentiments)

        positive_ratio = positive / total if total > 0 else 0
        negative_ratio = negative / total if total > 0 else 0

        if positive_ratio > 0.4:
            return 'positive'
        elif negative_ratio > 0.4:
            return 'negative'
        else:
            return 'neutral'

    def _calculate_sentiment_score(self, sentiments: List[Dict[str, Any]]) -> float:
        """
        Calculate numeric sentiment score (0.0 - 1.0).

        Args:
            sentiments: List of sentiment classifications

        Returns:
            Score between 0.0 (negative) and 1.0 (positive)
        """
        if not sentiments:
            return 0.5

        scores = []
        for s in sentiments:
            if s.get('sentiment') == 'positive':
                scores.append(0.7 + (0.3 * s.get('confidence', 0)))
            elif s.get('sentiment') == 'negative':
                scores.append(0.3 * (1 - s.get('confidence', 0)))
            else:
                scores.append(0.5)

        return sum(scores) / len(scores) if scores else 0.5

    def _get_sentiment_distribution(self, sentiments: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Get distribution of sentiments.

        Args:
            sentiments: List of sentiment classifications

        Returns:
            Dict with counts per sentiment
        """
        distribution = {'positive': 0, 'negative': 0, 'neutral': 0}

        for s in sentiments:
            sentiment = s.get('sentiment', 'neutral')
            if sentiment in distribution:
                distribution[sentiment] += 1

        return distribution

    async def _analyze_sentiment_trends(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime,
        days: int
    ) -> List[Dict[str, Any]]:
        """
        Analyze sentiment trends over time.

        Args:
            community_id: Community identifier
            period_start: Period start
            period_end: Period end
            days: Number of days in period

        Returns:
            List of trend data points
        """
        try:
            # Get daily sentiment data
            query = """
                SELECT
                    DATE(created_at) as day,
                    message_content,
                    COUNT(*) as message_count
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= $2
                  AND created_at <= $3
                GROUP BY DATE(created_at), message_content
                ORDER BY day DESC
            """

            rows = await self.dal.execute(query, [community_id, period_start, period_end])

            if not rows:
                return []

            # Group by day and analyze
            daily_sentiments = {}
            for row in rows:
                day = row.get('day')
                content = row.get('message_content', '')

                if day not in daily_sentiments:
                    daily_sentiments[day] = []

                sentiment = self._simple_sentiment_classify(content)
                daily_sentiments[day].append(sentiment)

            # Build trend data
            trends = []
            for day in sorted(daily_sentiments.keys()):
                sentiments = daily_sentiments[day]
                distribution = self._get_sentiment_distribution(
                    [{'sentiment': s} for s in sentiments]
                )

                trends.append({
                    'date': str(day),
                    'sentiment_distribution': distribution,
                    'message_count': len(sentiments)
                })

            return trends

        except Exception as e:
            logger.error(f"Sentiment trend analysis error: {describe_db_error(e)}")
            return []

    def _simple_sentiment_classify(self, text: str) -> str:
        """Quick sentiment classification for a text snippet"""
        text_lower = text.lower()

        positive_count = sum(1 for word in self.SENTIMENT_KEYWORDS['positive']
                            if word in text_lower)
        negative_count = sum(1 for word in self.SENTIMENT_KEYWORDS['negative']
                            if word in text_lower)

        if positive_count > negative_count:
            return 'positive'
        elif negative_count > positive_count:
            return 'negative'
        else:
            return 'neutral'

    async def _store_sentiment_analysis(
        self,
        community_id: int,
        overall_sentiment: str,
        sentiment_score: float,
        distribution: Dict[str, int],
        timeframe: str
    ) -> Optional[int]:
        """
        Store sentiment analysis results.

        Args:
            community_id: Community identifier
            overall_sentiment: Overall sentiment classification
            sentiment_score: Numeric sentiment score
            distribution: Sentiment distribution dict
            timeframe: Analysis timeframe

        Returns:
            Record ID or None
        """
        try:
            query = """
                INSERT INTO ai_sentiment_analysis (
                    community_id, overall_sentiment, sentiment_score,
                    sentiment_distribution, timeframe, created_at
                ) VALUES ($1, $2, $3, $4, $5, NOW())
                RETURNING id
            """

            rows = await self.dal.execute(query, [
                community_id,
                overall_sentiment,
                sentiment_score,
                json.dumps(distribution),
                timeframe
            ])

            if rows:
                return rows[0].get('id')

            return None

        except Exception as e:
            logger.error(f"Sentiment analysis storage error: {describe_db_error(e)}")
            return None

    def _parse_timeframe(self, timeframe: str) -> int:
        """Parse timeframe string to days."""
        mapping = {
            '1d': 1,
            '7d': 7,
            '30d': 30,
            '90d': 90
        }
        return mapping.get(timeframe, 7)
