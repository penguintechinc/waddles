"""
Community Insights Service for AI Researcher Module
===================================================

Generates AI-powered insights about community activity, trending topics, and sentiment.

Features:
- Analyze activity patterns and trends
- Identify trending topics from messages
- Generate sentiment reports over time
- Store insights in database for historical tracking
- Multi-timeframe analysis (daily, weekly, monthly)
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
class InsightResult:
    """Result from an insight generation operation"""
    success: bool
    insight_id: Optional[int]
    content: str
    insight_type: str
    tokens_used: int
    processing_time_ms: int
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'success': self.success,
            'insight_id': self.insight_id,
            'content': self.content,
            'insight_type': self.insight_type,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'error': self.error
        }


class InsightsService:
    """
    Service for generating AI-powered community insights.

    Integrates with:
    - AIProviderService: For LLM calls to analyze data
    - Database: For storing and retrieving insights
    - Mem0Service: For accessing community memories
    """

    SYSTEM_PROMPTS = {
        'activity': (
            "You are a community analyst. Analyze the provided activity data and "
            "generate insights about patterns, trends, and community engagement. "
            "Focus on actionable observations and interesting patterns. "
            "Be objective and data-driven."
        ),
        'trending': (
            "You are a trend analyst. Based on the provided message data, identify "
            "and describe the top trending topics in the community. "
            "For each topic, explain why it's trending and its impact. "
            "Be concise and highlight the most interesting trends."
        ),
        'sentiment': (
            "You are a sentiment analyst. Analyze the sentiment of community messages "
            "and provide insights about the overall mood, sentiment trends, and any "
            "significant shifts. Include specific examples and explain patterns."
        )
    }

    def __init__(
        self,
        ai_provider,  # AIProviderService
        dal,  # Database connection
        mem0_service=None  # Optional Mem0Service
    ):
        """
        Initialize insights service.

        Args:
            ai_provider: AI provider service for LLM calls
            dal: Database connection (AsyncDAL)
            mem0_service: Optional mem0 service for accessing memories
        """
        self.ai_provider = ai_provider
        self.dal = dal
        self.mem0_service = mem0_service

        logger.info("InsightsService initialized")

    async def generate_community_insights(
        self,
        community_id: int,
        timeframe: str = '7d',
        insight_types: Optional[List[str]] = None
    ) -> InsightResult:
        """
        Generate comprehensive AI-powered community insights.

        Args:
            community_id: Community identifier
            timeframe: Analysis timeframe ('1d', '7d', '30d', '90d')
            insight_types: Specific insight types to generate
                          (default: ['activity', 'trending', 'sentiment'])

        Returns:
            InsightResult with generated insights
        """
        start_time = time.time()

        if insight_types is None:
            insight_types = ['activity', 'trending', 'sentiment']

        try:
            logger.info(
                f"Generating community insights",
                extra={
                    'community_id': community_id,
                    'timeframe': timeframe,
                    'insight_types': insight_types
                }
            )

            # Calculate time window
            days = self._parse_timeframe(timeframe)
            period_start = datetime.utcnow() - timedelta(days=days)
            period_end = datetime.utcnow()

            # Gather data for analysis
            message_stats = await self._get_message_statistics(
                community_id,
                period_start,
                period_end
            )

            if not message_stats:
                logger.warning(
                    "No messages found for insight generation",
                    extra={'community_id': community_id}
                )
                return InsightResult(
                    success=True,
                    insight_id=None,
                    content="No messages found in the specified timeframe.",
                    insight_type='activity',
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000)
                )

            # Generate insights for each type
            insights_content = []
            total_tokens = 0

            for insight_type in insight_types:
                insight_data = await self._generate_single_insight(
                    community_id,
                    insight_type,
                    message_stats,
                    period_start,
                    period_end
                )

                if insight_data:
                    insights_content.append(insight_data['content'])
                    total_tokens += insight_data.get('tokens_used', 0)

            # Combine insights
            combined_content = "\n\n---\n\n".join(insights_content)

            # Store insights in database
            insight_id = await self._store_insights(
                community_id,
                combined_content,
                'comprehensive',
                period_start,
                period_end,
                {
                    'types_generated': insight_types,
                    'message_count': message_stats.get('total_messages', 0),
                    'unique_users': message_stats.get('unique_users', 0)
                }
            )

            processing_time = int((time.time() - start_time) * 1000)
            logger.info(
                f"Insights generated successfully",
                extra={
                    'community_id': community_id,
                    'insight_id': insight_id,
                    'tokens_used': total_tokens,
                    'processing_time_ms': processing_time
                }
            )

            return InsightResult(
                success=True,
                insight_id=insight_id,
                content=combined_content,
                insight_type='comprehensive',
                tokens_used=total_tokens,
                processing_time_ms=processing_time
            )

        except Exception as e:
            logger.error(
                f"Insight generation error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return InsightResult(
                success=False,
                insight_id=None,
                content="",
                insight_type='error',
                tokens_used=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                error=describe_db_error(e)
            )

    async def _generate_single_insight(
        self,
        community_id: int,
        insight_type: str,
        message_stats: Dict[str, Any],
        period_start: datetime,
        period_end: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a single type of insight.

        Args:
            community_id: Community identifier
            insight_type: Type of insight ('activity', 'trending', 'sentiment')
            message_stats: Pre-gathered message statistics
            period_start: Analysis period start
            period_end: Analysis period end

        Returns:
            Dict with insight content and tokens used, or None on error
        """
        try:
            system_prompt = self.SYSTEM_PROMPTS.get(
                insight_type,
                self.SYSTEM_PROMPTS['activity']
            )

            # Build data context for the insight type
            context_data = await self._build_insight_context(
                community_id,
                insight_type,
                message_stats,
                period_start,
                period_end
            )

            user_prompt = f"Generate {insight_type} insights based on this data:\n\n{context_data}"

            # Generate with AI
            response = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.7,
                max_tokens=800
            )

            return {
                'content': response.content,
                'tokens_used': response.tokens_used
            }

        except Exception as e:
            logger.error(
                f"Single insight generation error: {describe_db_error(e)}",
                extra={'community_id': community_id, 'insight_type': insight_type}
            )
            return None

    async def _build_insight_context(
        self,
        community_id: int,
        insight_type: str,
        message_stats: Dict[str, Any],
        period_start: datetime,
        period_end: datetime
    ) -> str:
        """
        Build context data for insight generation.

        Args:
            community_id: Community identifier
            insight_type: Type of insight being generated
            message_stats: Pre-gathered statistics
            period_start: Analysis period start
            period_end: Analysis period end

        Returns:
            Formatted context string for AI analysis
        """
        try:
            context_parts = [
                f"Analysis Period: {period_start.date()} to {period_end.date()}",
                f"Total Messages: {message_stats.get('total_messages', 0)}",
                f"Unique Users: {message_stats.get('unique_users', 0)}",
                f"Messages per Day: {message_stats.get('messages_per_day', 0):.1f}"
            ]

            if insight_type == 'trending':
                # Add top topics
                topics = await self._extract_topics(community_id, period_start, period_end)
                if topics:
                    context_parts.append("\nTop Topics:")
                    for idx, (topic, count) in enumerate(topics[:10], 1):
                        context_parts.append(f"  {idx}. {topic} ({count} mentions)")

            elif insight_type == 'sentiment':
                # Add sentiment distribution
                sentiment_data = await self._analyze_sentiment_distribution(
                    community_id,
                    period_start,
                    period_end
                )
                if sentiment_data:
                    context_parts.append(f"\nSentiment Summary: {sentiment_data}")

            elif insight_type == 'activity':
                # Add activity patterns
                activity_pattern = message_stats.get('activity_pattern', '')
                if activity_pattern:
                    context_parts.append(f"\nActivity Pattern: {activity_pattern}")

                # Add peak hours
                peak_hours = message_stats.get('peak_hours', [])
                if peak_hours:
                    context_parts.append(f"Peak Hours: {', '.join(map(str, peak_hours))}")

            return "\n".join(context_parts)

        except Exception as e:
            logger.error(f"Context building error: {describe_db_error(e)}")
            return str(message_stats)

    async def _get_message_statistics(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Get message statistics for a period.

        Args:
            community_id: Community identifier
            period_start: Period start timestamp
            period_end: Period end timestamp

        Returns:
            Dict with statistics or None
        """
        try:
            query = """
                SELECT
                    COUNT(*) as total_messages,
                    COUNT(DISTINCT platform_user_id) as unique_users,
                    AVG(LENGTH(message_content)) as avg_message_length
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= $2
                  AND created_at <= $3
            """
            rows = await self.dal.execute(query, [community_id, period_start, period_end])

            if not rows:
                return None

            row = rows[0]
            total_messages = row.get('total_messages', 0)
            days = (period_end - period_start).days or 1

            return {
                'total_messages': total_messages,
                'unique_users': row.get('unique_users', 0),
                'avg_message_length': float(row.get('avg_message_length', 0)) if row.get('avg_message_length') else 0,
                'messages_per_day': total_messages / days,
                'period_days': days
            }

        except Exception as e:
            logger.error(f"Message statistics error: {describe_db_error(e)}")
            return None

    async def _extract_topics(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime,
        limit: int = 10
    ) -> Optional[List[tuple]]:
        """
        Extract trending topics from messages.

        Args:
            community_id: Community identifier
            period_start: Period start
            period_end: Period end
            limit: Number of topics to return

        Returns:
            List of (topic, count) tuples or None
        """
        try:
            # Simple word frequency analysis
            query = """
                SELECT message_content
                FROM ai_context_messages
                WHERE community_id = $1
                  AND created_at >= $2
                  AND created_at <= $3
                ORDER BY created_at DESC
                LIMIT 1000
            """
            rows = await self.dal.execute(query, [community_id, period_start, period_end])

            if not rows:
                return None

            # Extract common words (simple approach)
            word_freq = {}
            stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at',
                         'to', 'for', 'of', 'is', 'are', 'was', 'be', 'been',
                         'has', 'have', 'do', 'does', 'did', 'will', 'would',
                         'could', 'should', 'i', 'you', 'he', 'she', 'it',
                         'we', 'they', 'what', 'which', 'who', 'when', 'where',
                         'why', 'how', 'so', 'if', 'just', 'as', 'not', 'no',
                         'yes', 'it'}

            for row in rows:
                content = (row.get('message_content') or '').lower()
                words = content.split()

                for word in words:
                    word = word.strip('.,!?;:').lower()
                    if word and len(word) > 3 and word not in stop_words:
                        word_freq[word] = word_freq.get(word, 0) + 1

            # Get top words
            topics = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:limit]
            return topics

        except Exception as e:
            logger.error(f"Topic extraction error: {describe_db_error(e)}")
            return None

    async def _analyze_sentiment_distribution(
        self,
        community_id: int,
        period_start: datetime,
        period_end: datetime
    ) -> str:
        """
        Analyze sentiment distribution in a period.

        Args:
            community_id: Community identifier
            period_start: Period start
            period_end: Period end

        Returns:
            Formatted sentiment summary string
        """
        try:
            # TODO: Implement actual sentiment analysis
            # For now, return a placeholder
            return "Sentiment analysis pending - implement with NLP model"
        except Exception as e:
            logger.error(f"Sentiment analysis error: {describe_db_error(e)}")
            return ""

    async def _store_insights(
        self,
        community_id: int,
        content: str,
        insight_type: str,
        period_start: datetime,
        period_end: datetime,
        metadata: Dict[str, Any]
    ) -> Optional[int]:
        """
        Store generated insights in database.

        Args:
            community_id: Community identifier
            content: Insight content
            insight_type: Type of insight
            period_start: Analysis period start
            period_end: Analysis period end
            metadata: Additional metadata

        Returns:
            Insight ID or None on error
        """
        try:
            query = """
                INSERT INTO ai_community_insights (
                    community_id, insight_type, content,
                    period_start, period_end, metadata,
                    created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
                RETURNING id
            """

            rows = await self.dal.execute(query, [
                community_id,
                insight_type,
                content,
                period_start,
                period_end,
                json.dumps(metadata)
            ])

            if rows:
                return rows[0].get('id')

            return None

        except Exception as e:
            logger.error(f"Insight storage error: {describe_db_error(e)}")
            return None

    def _parse_timeframe(self, timeframe: str) -> int:
        """
        Parse timeframe string to days.

        Args:
            timeframe: Timeframe string ('1d', '7d', '30d', '90d')

        Returns:
            Number of days
        """
        mapping = {
            '1d': 1,
            '7d': 7,
            '30d': 30,
            '90d': 90
        }
        return mapping.get(timeframe, 7)
