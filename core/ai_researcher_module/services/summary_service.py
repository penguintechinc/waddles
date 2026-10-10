"""
AI Researcher Summary Service
==============================

Generates stream and weekly summaries with AI-powered insights.

Features:
- Stream summary generation from message history
- Weekly rollup summaries
- Insight storage with embeddings for RAG recall
- Topic analysis, sentiment tracking, viewer engagement metrics
"""

import sys
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'libs'))
from flask_core import describe_db_error, setup_aaa_logging  # noqa: E402

logger = setup_aaa_logging('ai_researcher_summary', '1.0.0')


class SummaryService:
    """
    Service for generating AI-powered stream and weekly summaries.
    """

    def __init__(self, ai_provider, mem0_service, db_connection):
        """
        Initialize summary service.

        Args:
            ai_provider: AI provider client (Ollama/WaddleAI)
            mem0_service: Mem0 client for vector embeddings
            db_connection: Database connection (AsyncDAL)
        """
        self.ai_provider = ai_provider
        self.mem0_service = mem0_service
        self.db = db_connection
        logger.system("SummaryService initialized", result="SUCCESS")

    async def generate_stream_summary(
        self,
        community_id: int,
        stream_start: datetime,
        stream_end: datetime
    ) -> dict:
        """
        Generate summary for a stream session.

        Args:
            community_id: Community ID
            stream_start: Stream start timestamp
            stream_end: Stream end timestamp

        Returns:
            dict with summary data and insight_id

        Example return:
        {
            "insight_id": 123,
            "title": "Stream Summary - Jan 15, 2025",
            "summary": "...",
            "key_topics": ["topic1", "topic2"],
            "notable_moments": ["moment1", "moment2"],
            "viewer_stats": {...},
            "sentiment": "positive"
        }
        """
        try:
            logger.system(
                "Generating stream summary",
                action="generate_stream_summary",
                community=community_id
            )

            # Get messages from the period
            messages = await self._get_messages_for_period(
                community_id,
                stream_start,
                stream_end
            )

            if not messages:
                logger.system(
                    "No messages found for stream period",
                    community=community_id,
                    result="EMPTY"
                )
                return {
                    "insight_id": None,
                    "title": f"Stream Summary - {stream_start.strftime('%b %d, %Y')}",
                    "summary": "No messages recorded during this stream.",
                    "key_topics": [],
                    "notable_moments": [],
                    "viewer_stats": {},
                    "sentiment": "neutral"
                }

            # Build context for AI
            context = self._build_stream_context(messages, stream_start, stream_end)

            # Generate summary with AI -- JSON prompt only for a JSON-capable
            # model, plain labeled text otherwise (the free/default path)
            json_mode = self._supports_json()
            prompt = self._build_stream_summary_prompt(context, json_mode)
            ai_response = await self.ai_provider.generate(
                prompt, want_json=json_mode
            )

            # Parse AI response in the mode the provider actually used
            summary_data = self._parse_stream_summary(
                ai_response.content, ai_response.json_mode
            )

            # Calculate viewer engagement metrics
            viewer_stats = self._calculate_viewer_stats(messages)
            summary_data['viewer_stats'] = viewer_stats

            # Save insight to database
            insight_id = await self.save_insight(
                community_id=community_id,
                insight_type='stream_summary',
                title=summary_data['title'],
                content=summary_data['summary'],
                metadata={
                    'key_topics': summary_data.get('key_topics', []),
                    'notable_moments': summary_data.get('notable_moments', []),
                    'viewer_stats': viewer_stats,
                    'sentiment': summary_data.get('sentiment', 'neutral'),
                    'period_start': stream_start.isoformat(),
                    'period_end': stream_end.isoformat(),
                    'message_count': len(messages)
                }
            )

            summary_data['insight_id'] = insight_id

            logger.audit(
                action="stream_summary_generated",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                insight_id=insight_id,
                message_count=len(messages)
            )

            return summary_data

        except Exception as e:
            logger.error(
                f"Failed to generate stream summary: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    async def generate_weekly_summary(self, community_id: int) -> dict:
        """
        Generate weekly rollup summary for a community.

        Aggregates stream summaries, top chatters, popular topics, sentiment trends.

        Args:
            community_id: Community ID

        Returns:
            dict with weekly summary data and insight_id

        Example return:
        {
            "insight_id": 124,
            "title": "Weekly Summary - Week of Jan 15, 2025",
            "summary": "...",
            "top_chatters": [{"user": "user1", "count": 100}, ...],
            "popular_topics": ["topic1", "topic2"],
            "sentiment_trend": "positive",
            "stream_count": 5,
            "total_messages": 10000
        }
        """
        try:
            logger.system(
                "Generating weekly summary",
                action="generate_weekly_summary",
                community=community_id
            )

            # Calculate week boundaries (last 7 days)
            week_end = datetime.utcnow()
            week_start = week_end - timedelta(days=7)

            # Get stream summaries from the week
            stream_summaries = await self._get_stream_summaries(
                community_id,
                week_start,
                week_end
            )

            # Get all messages from the week for aggregate stats
            messages = await self._get_messages_for_period(
                community_id,
                week_start,
                week_end
            )

            if not messages:
                logger.system(
                    "No messages found for weekly period",
                    community=community_id,
                    result="EMPTY"
                )
                return {
                    "insight_id": None,
                    "title": f"Weekly Summary - Week of {week_start.strftime('%b %d, %Y')}",
                    "summary": "No activity recorded this week.",
                    "top_chatters": [],
                    "popular_topics": [],
                    "sentiment_trend": "neutral",
                    "stream_count": 0,
                    "total_messages": 0
                }

            # Calculate weekly metrics
            top_chatters = self._calculate_top_chatters(messages)
            popular_topics = self._extract_popular_topics(stream_summaries, messages)
            sentiment_trend = self._calculate_sentiment_trend(stream_summaries)

            # Build context for AI
            context = self._build_weekly_context(
                stream_summaries,
                messages,
                top_chatters,
                popular_topics,
                sentiment_trend
            )

            # Generate summary with AI (JSON only for a JSON-capable model)
            json_mode = self._supports_json()
            prompt = self._build_weekly_summary_prompt(context, json_mode)
            ai_response = await self.ai_provider.generate(
                prompt, want_json=json_mode
            )

            # Parse AI response in the mode the provider actually used
            summary_data = self._parse_weekly_summary(
                ai_response.content, ai_response.json_mode
            )
            summary_data['top_chatters'] = top_chatters[:10]  # Top 10
            summary_data['popular_topics'] = popular_topics[:10]  # Top 10
            summary_data['sentiment_trend'] = sentiment_trend
            summary_data['stream_count'] = len(stream_summaries)
            summary_data['total_messages'] = len(messages)

            # Save insight to database
            insight_id = await self.save_insight(
                community_id=community_id,
                insight_type='weekly_rollup',
                title=summary_data['title'],
                content=summary_data['summary'],
                metadata={
                    'top_chatters': top_chatters[:10],
                    'popular_topics': popular_topics[:10],
                    'sentiment_trend': sentiment_trend,
                    'stream_count': len(stream_summaries),
                    'total_messages': len(messages),
                    'period_start': week_start.isoformat(),
                    'period_end': week_end.isoformat()
                }
            )

            summary_data['insight_id'] = insight_id

            logger.audit(
                action="weekly_summary_generated",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                insight_id=insight_id,
                stream_count=len(stream_summaries),
                message_count=len(messages)
            )

            return summary_data

        except Exception as e:
            logger.error(
                f"Failed to generate weekly summary: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    async def get_recent_summaries(
        self,
        community_id: int,
        limit: int = 10
    ) -> List[dict]:
        """
        Get recent summaries for a community.

        Args:
            community_id: Community ID
            limit: Maximum number of summaries to return

        Returns:
            List of summary insights
        """
        try:
            query = """
                SELECT id, insight_type, title, content, content_html,
                       metadata, period_start, period_end, created_at
                FROM ai_insights
                WHERE community_id = $1
                  AND insight_type IN ('stream_summary', 'weekly_rollup')
                ORDER BY created_at DESC
                LIMIT $2
            """

            rows = await self.db.execute(query, [community_id, limit])

            summaries = []
            for row in rows:
                summaries.append({
                    'id': row['id'],
                    'insight_type': row['insight_type'],
                    'title': row['title'],
                    'content': row['content'],
                    'content_html': row['content_html'],
                    'metadata': row['metadata'] or {},
                    'period_start': row['period_start'],
                    'period_end': row['period_end'],
                    'created_at': row['created_at']
                })

            logger.system(
                f"Retrieved {len(summaries)} recent summaries",
                community=community_id,
                result="SUCCESS"
            )

            return summaries

        except Exception as e:
            logger.error(
                f"Failed to retrieve recent summaries: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    async def save_insight(
        self,
        community_id: int,
        insight_type: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Save an AI insight to database.

        Args:
            community_id: Community ID
            insight_type: Type of insight (stream_summary, weekly_rollup, etc.)
            title: Insight title
            content: Insight content (markdown/text)
            metadata: Additional metadata (dict)

        Returns:
            insight_id: ID of saved insight
        """
        try:
            # Generate embedding for RAG recall
            embedding_vector = None
            if self.mem0_service:
                try:
                    embedding_vector = await self.mem0_service.generate_embedding(content)
                except Exception as e:
                    logger.error(f"Failed to generate embedding: {describe_db_error(e)}")

            # Extract period from metadata if present
            period_start = None
            period_end = None
            if metadata:
                if 'period_start' in metadata:
                    period_start = datetime.fromisoformat(metadata['period_start'])
                if 'period_end' in metadata:
                    period_end = datetime.fromisoformat(metadata['period_end'])

            query = """
                INSERT INTO ai_insights (
                    community_id, insight_type, title, content,
                    metadata, embedding_vector, period_start, period_end
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            """

            result = await self.db.execute(
                query,
                [
                    community_id,
                    insight_type,
                    title,
                    content,
                    json.dumps(metadata or {}),
                    embedding_vector,
                    period_start,
                    period_end
                ]
            )

            insight_id = result[0]['id']

            logger.audit(
                action="insight_saved",
                user="system",
                community=str(community_id),
                result="SUCCESS",
                insight_id=insight_id,
                insight_type=insight_type
            )

            return insight_id

        except Exception as e:
            logger.error(
                f"Failed to save insight: {describe_db_error(e)}",
                community=community_id,
                error=describe_db_error(e)
            )
            raise

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    async def _get_messages_for_period(
        self,
        community_id: int,
        start: datetime,
        end: datetime
    ) -> List[dict]:
        """Get messages for a time period."""
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

    async def _get_stream_summaries(
        self,
        community_id: int,
        start: datetime,
        end: datetime
    ) -> List[dict]:
        """Get stream summaries for a time period."""
        query = """
            SELECT id, title, content, metadata, created_at
            FROM ai_insights
            WHERE community_id = $1
              AND insight_type = 'stream_summary'
              AND created_at >= $2
              AND created_at <= $3
            ORDER BY created_at ASC
        """

        rows = await self.db.execute(query, [community_id, start, end])
        return [dict(row) for row in rows]

    def _build_stream_context(
        self,
        messages: List[dict],
        stream_start: datetime,
        stream_end: datetime
    ) -> dict:
        """Build context dict for stream summary."""
        return {
            'message_count': len(messages),
            'stream_start': stream_start.isoformat(),
            'stream_end': stream_end.isoformat(),
            'duration_minutes': int((stream_end - stream_start).total_seconds() / 60),
            'messages': messages[:500]  # Limit to avoid token overflow
        }

    def _build_weekly_context(
        self,
        stream_summaries: List[dict],
        messages: List[dict],
        top_chatters: List[dict],
        popular_topics: List[str],
        sentiment_trend: str
    ) -> dict:
        """Build context dict for weekly summary."""
        return {
            'stream_count': len(stream_summaries),
            'total_messages': len(messages),
            'top_chatters': top_chatters[:10],
            'popular_topics': popular_topics[:10],
            'sentiment_trend': sentiment_trend,
            'stream_summaries': stream_summaries
        }

    def _supports_json(self) -> bool:
        """True only if the configured model is declared JSON-capable (env)."""
        return bool(getattr(self.ai_provider, 'supports_json', False))

    def _build_stream_summary_prompt(
        self, context: dict, json_mode: bool = False
    ) -> str:
        """Build AI prompt for stream summary (JSON or plain-text labeled lines)."""
        details = f"""Stream Details:
- Duration: {context['duration_minutes']} minutes
- Total Messages: {context['message_count']}

Your task:
1. Identify 3-5 key topics discussed
2. Note 2-3 notable moments or highlights
3. Assess overall sentiment (positive/neutral/negative)
4. Write a 2-3 sentence summary
"""
        # default=str: DB rows carry datetimes, which json.dumps rejects outright
        sample = (
            f"Messages sample: "
            f"{json.dumps(context['messages'][:100], default=str)}"
        )

        if json_mode:
            return f"""
Analyze this stream session and provide a summary in JSON format.

{details}
Return JSON with this structure:
{{
    "title": "Stream Summary - [date]",
    "summary": "...",
    "key_topics": ["topic1", "topic2", ...],
    "notable_moments": ["moment1", "moment2", ...],
    "sentiment": "positive|neutral|negative"
}}

{sample}
"""

        return f"""
Analyze this stream session and write a summary as plain text.

{details}
Reply with exactly these labeled lines and nothing else:
TITLE: <one line, e.g. Stream Summary - [date]>
SUMMARY: <2-3 sentences>
KEY TOPICS: <comma-separated list>
NOTABLE MOMENTS: <semicolon-separated list>
SENTIMENT: <positive, neutral or negative>

{sample}
"""

    def _build_weekly_summary_prompt(
        self, context: dict, json_mode: bool = False
    ) -> str:
        """Build AI prompt for weekly summary (JSON or plain-text labeled lines)."""
        overview = f"""Week Overview:
- Streams: {context['stream_count']}
- Total Messages: {context['total_messages']}
- Top Chatters: {json.dumps(context['top_chatters'][:5])}
- Popular Topics: {json.dumps(context['popular_topics'][:10])}
- Sentiment Trend: {context['sentiment_trend']}

Your task:
1. Summarize the week's activity in 3-4 sentences
2. Highlight community engagement patterns
3. Note any trending topics or themes
"""
        if json_mode:
            return f"""
Generate a weekly community summary in JSON format.

{overview}
Return JSON with this structure:
{{
    "title": "Weekly Summary - Week of [date]",
    "summary": "..."
}}
"""

        return f"""
Write a weekly community summary as plain text.

{overview}
Reply with exactly these labeled lines and nothing else:
TITLE: <one line, e.g. Weekly Summary - Week of [date]>
SUMMARY: <3-4 sentences>
"""

    # Labels the plain-text prompts ask for -> result keys / value kind.
    _STREAM_TEXT_FIELDS = {
        'TITLE': ('title', 'str'),
        'SUMMARY': ('summary', 'str'),
        'KEY TOPICS': ('key_topics', 'list,'),
        'NOTABLE MOMENTS': ('notable_moments', 'list;'),
        'SENTIMENT': ('sentiment', 'str'),
    }
    _WEEKLY_TEXT_FIELDS = {
        'TITLE': ('title', 'str'),
        'SUMMARY': ('summary', 'str'),
    }

    @staticmethod
    def _parse_labeled_text(text: str, fields: dict) -> dict:
        """Parse `LABEL: value` lines (case-insensitive) into a dict; unknown lines ignored.

        A value may continue on following lines until the next known label.
        Returns only the fields that were actually present.
        """
        found: Dict[str, str] = {}
        current: Optional[str] = None
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip('*#-').strip()
            label, sep, rest = line.partition(':')
            normalized = label.strip().strip('*').strip().upper()
            if sep and normalized in fields:
                current = normalized
                found[current] = rest.strip().strip('*').strip()
            elif current is not None and line:
                found[current] = f"{found[current]} {line}".strip()

        result: dict = {}
        for label, value in found.items():
            key, kind = fields[label]
            if kind == 'str':
                result[key] = value
            else:
                sep_char = kind[-1]
                result[key] = [
                    item.strip() for item in value.split(sep_char) if item.strip()
                ]
        return result

    def _parse_stream_summary(self, ai_response: str, json_mode: bool = False) -> dict:
        """Parse AI response text into a stream summary dict."""
        return self._parse_summary(
            ai_response, json_mode,
            fields=self._STREAM_TEXT_FIELDS,
            default_title='Stream Summary',
            defaults={
                'key_topics': [],
                'notable_moments': [],
                'sentiment': 'neutral'
            }
        )

    def _parse_weekly_summary(self, ai_response: str, json_mode: bool = False) -> dict:
        """Parse AI response text into a weekly summary dict."""
        return self._parse_summary(
            ai_response, json_mode,
            fields=self._WEEKLY_TEXT_FIELDS,
            default_title='Weekly Summary',
            defaults={}
        )

    def _parse_summary(
        self,
        ai_response: str,
        json_mode: bool,
        fields: dict,
        default_title: str,
        defaults: dict
    ) -> dict:
        """Parse a summary in the mode the provider actually ran in.

        JSON mode: the provider already validated the JSON. Text mode: read the
        labeled lines. Either way `title` and `summary` are always present; if
        the model ignored the requested shape the whole reply becomes the
        summary and a warning is logged (degraded, never silent).
        """
        data: dict = {}
        if json_mode:
            try:
                parsed = json.loads(ai_response)
                data = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                data = {}
        else:
            data = self._parse_labeled_text(ai_response, fields)

        if not isinstance(data.get('summary'), str) or not data['summary'].strip():
            logger.warning(
                f"Summary reply did not follow the requested "
                f"{'JSON' if json_mode else 'labeled-text'} shape; "
                f"using the whole reply as the summary ({len(ai_response)} chars)"
            )
            data['summary'] = ai_response.strip()
        if not isinstance(data.get('title'), str) or not data['title'].strip():
            data['title'] = default_title
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data

    def _calculate_viewer_stats(self, messages: List[dict]) -> dict:
        """Calculate viewer engagement statistics."""
        if not messages:
            return {'unique_chatters': 0, 'avg_messages_per_user': 0}

        user_counts = {}
        for msg in messages:
            user_id = msg.get('platform_user_id')
            if user_id:
                user_counts[user_id] = user_counts.get(user_id, 0) + 1

        unique_chatters = len(user_counts)
        avg_messages = sum(user_counts.values()) / unique_chatters if unique_chatters > 0 else 0

        return {
            'unique_chatters': unique_chatters,
            'avg_messages_per_user': round(avg_messages, 2),
            'total_messages': len(messages)
        }

    def _calculate_top_chatters(self, messages: List[dict]) -> List[dict]:
        """Calculate top chatters from messages."""
        user_counts = {}
        user_names = {}

        for msg in messages:
            user_id = msg.get('platform_user_id')
            username = msg.get('platform_username', 'Unknown')
            if user_id:
                user_counts[user_id] = user_counts.get(user_id, 0) + 1
                user_names[user_id] = username

        # Sort by message count
        sorted_users = sorted(user_counts.items(), key=lambda x: x[1], reverse=True)

        return [
            {'user': user_names.get(user_id, user_id), 'count': count}
            for user_id, count in sorted_users
        ]

    def _extract_popular_topics(
        self,
        stream_summaries: List[dict],
        messages: List[dict]
    ) -> List[str]:
        """Extract popular topics from summaries and messages."""
        topics = []

        # Extract from stream summaries
        for summary in stream_summaries:
            metadata = summary.get('metadata', {})
            if isinstance(metadata, dict):
                summary_topics = metadata.get('key_topics', [])
                topics.extend(summary_topics)

        # Simple word frequency analysis from messages (fallback)
        # This is a basic implementation - could be enhanced with NLP
        if not topics and messages:
            word_freq = {}
            for msg in messages:
                content = msg.get('message_content', '').lower()
                words = content.split()
                for word in words:
                    if len(word) > 4:  # Filter short words
                        word_freq[word] = word_freq.get(word, 0) + 1

            # Get top words
            sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
            topics = [word for word, count in sorted_words[:10]]

        # Deduplicate and return
        return list(set(topics))

    def _calculate_sentiment_trend(self, stream_summaries: List[dict]) -> str:
        """Calculate overall sentiment trend from stream summaries."""
        if not stream_summaries:
            return 'neutral'

        sentiment_counts = {'positive': 0, 'neutral': 0, 'negative': 0}

        for summary in stream_summaries:
            metadata = summary.get('metadata', {})
            if isinstance(metadata, dict):
                sentiment = metadata.get('sentiment', 'neutral')
                if sentiment in sentiment_counts:
                    sentiment_counts[sentiment] += 1

        # Return most common sentiment
        return max(sentiment_counts.items(), key=lambda x: x[1])[0]
