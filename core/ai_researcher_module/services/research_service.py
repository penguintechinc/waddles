"""
Research Service for AI Researcher Module
==========================================

Handles all !or/* commands:
- !or/research: Perform research on a topic
- !or/ask: Ask questions with community context
- !or/recall: Semantic search through memories
- !or/summarize: Generate conversation summaries

Features:
- Rate limiting per user and global
- Safety layer for content moderation
- Redis caching with TTL
- mem0 semantic similarity caching
- Token usage tracking
- Processing time metrics
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResearchResult:
    """Result from a research operation"""
    success: bool
    content: str
    tokens_used: int
    processing_time_ms: int
    was_cached: bool
    blocked_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'success': self.success,
            'content': self.content,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason
        }


class ResearchService:
    """
    Service for handling AI research commands.

    Integrates with:
    - AIProviderService: For LLM calls
    - Mem0Service: For semantic memory
    - SafetyLayer: For content moderation
    - RateLimiter: For rate limiting
    - Redis: For caching
    """

    # System prompts for different command types
    SYSTEM_PROMPTS = {
        'research': (
            "You are a helpful research assistant. Provide accurate, concise "
            "information on the topic. Cite sources when possible. Focus on "
            "factual information and be objective. If you don't know something, "
            "say so clearly."
        ),
        'ask': (
            "You are a community assistant with knowledge of this chat's context. "
            "Answer questions helpfully and accurately using the provided context. "
            "If the context doesn't contain relevant information, say so. "
            "Be conversational but precise."
        ),
        'summarize': (
            "Summarize the recent conversation highlighting key topics, notable "
            "moments, and community sentiment. Be concise and objective. Focus on "
            "the most important or interesting aspects. Include any trending topics "
            "or recurring themes."
        ),
        'recall': (
            "You are retrieving memories from the community's knowledge base. "
            "Present the memories in a clear, organized way. If multiple memories "
            "are found, group them by theme or topic."
        )
    }

    def __init__(
        self,
        ai_provider,  # AIProviderService
        mem0_service,  # Mem0Service
        safety_layer,  # SafetyLayer
        rate_limiter,  # RateLimiter
        redis_client
    ):
        """
        Initialize research service.

        Args:
            ai_provider: AI provider service for LLM calls
            mem0_service: mem0 service for semantic memory
            safety_layer: Safety layer for content moderation
            rate_limiter: Rate limiter for API protection
            redis_client: Redis client for caching
        """
        self.ai_provider = ai_provider
        self.mem0_service = mem0_service
        self.safety_layer = safety_layer
        self.rate_limiter = rate_limiter
        self.redis = redis_client

        logger.info("ResearchService initialized")

    async def research(
        self,
        community_id: int,
        user_id: str,
        topic: str
    ) -> ResearchResult:
        """
        Perform research on a topic using AI.

        Args:
            community_id: Community identifier
            user_id: User identifier
            topic: Topic to research

        Returns:
            ResearchResult with success status and content
        """
        start_time = time.time()

        try:
            # Check rate limit
            rate_limit_key = f"research:{community_id}:{user_id}"
            if not await self._check_rate_limit(rate_limit_key, 'research'):
                logger.warning(
                    f"Rate limit exceeded for research",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="rate_limit"
                )

            # Check safety
            safety_check = await self._check_safety(topic, community_id)
            if not safety_check['safe']:
                logger.warning(
                    f"Safety check failed for research topic",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id,
                        'reason': safety_check.get('reason')
                    }
                )
                return ResearchResult(
                    success=False,
                    content="This topic cannot be researched due to safety concerns.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason=safety_check.get('reason', 'safety_violation')
                )

            # Check cache
            cache_key = self._get_cache_key('research', community_id, topic)
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                logger.info(
                    f"Research cache hit",
                    extra={
                        'community_id': community_id,
                        'topic': topic[:50]
                    }
                )
                processing_time = int((time.time() - start_time) * 1000)
                return ResearchResult(
                    success=True,
                    content=cached_result['content'],
                    tokens_used=0,  # No tokens used for cached response
                    processing_time_ms=processing_time,
                    was_cached=True
                )

            # Check mem0 for similar research
            if Config.ENABLE_SEMANTIC_CACHE:
                similar_result = await self._check_semantic_cache(
                    community_id,
                    topic,
                    'research'
                )
                if similar_result:
                    logger.info(
                        f"Research semantic cache hit",
                        extra={
                            'community_id': community_id,
                            'topic': topic[:50],
                            'similarity': similar_result.get('similarity')
                        }
                    )
                    processing_time = int((time.time() - start_time) * 1000)
                    # Cache this query too for faster future access
                    await self._save_to_cache(
                        cache_key,
                        {'content': similar_result['content']},
                        Config.CACHE_TTL_RESEARCH
                    )
                    return ResearchResult(
                        success=True,
                        content=similar_result['content'],
                        tokens_used=0,
                        processing_time_ms=processing_time,
                        was_cached=True
                    )

            # Generate response with AI
            response = await self._generate_ai_response(
                system_prompt=self.SYSTEM_PROMPTS['research'],
                user_prompt=f"Research the following topic: {topic}",
                community_id=community_id,
                user_id=user_id,
                context_type='research'
            )

            if not response['success']:
                logger.error(
                    f"AI generation failed for research",
                    extra={
                        'community_id': community_id,
                        'error': response.get('error')
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Failed to generate research response. Please try again.",
                    tokens_used=response.get('tokens_used', 0),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="generation_failed"
                )

            # Cache result
            await self._save_to_cache(
                cache_key,
                {'content': response['content']},
                Config.CACHE_TTL_RESEARCH
            )

            # Save to mem0 for future semantic lookups
            await self._save_to_mem0(
                community_id=community_id,
                content=response['content'],
                metadata={
                    'type': 'research',
                    'topic': topic,
                    'user_id': user_id
                }
            )

            processing_time = int((time.time() - start_time) * 1000)
            logger.info(
                f"Research completed",
                extra={
                    'community_id': community_id,
                    'tokens_used': response['tokens_used'],
                    'processing_time_ms': processing_time
                }
            )

            return ResearchResult(
                success=True,
                content=response['content'],
                tokens_used=response['tokens_used'],
                processing_time_ms=processing_time,
                was_cached=False
            )

        except Exception as e:
            logger.error(
                f"Research error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return ResearchResult(
                success=False,
                content="An error occurred during research. Please try again.",
                tokens_used=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                was_cached=False,
                blocked_reason="internal_error"
            )

    async def ask(
        self,
        community_id: int,
        user_id: str,
        question: str
    ) -> ResearchResult:
        """
        Ask a question with community context awareness.

        Args:
            community_id: Community identifier
            user_id: User identifier
            question: Question to ask

        Returns:
            ResearchResult with success status and answer
        """
        start_time = time.time()

        try:
            # Check rate limit
            rate_limit_key = f"ask:{community_id}:{user_id}"
            if not await self._check_rate_limit(rate_limit_key, 'research'):
                logger.warning(
                    f"Rate limit exceeded for ask",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="rate_limit"
                )

            # Check safety
            safety_check = await self._check_safety(question, community_id)
            if not safety_check['safe']:
                logger.warning(
                    f"Safety check failed for question",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id,
                        'reason': safety_check.get('reason')
                    }
                )
                return ResearchResult(
                    success=False,
                    content="This question cannot be answered due to safety concerns.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason=safety_check.get('reason', 'safety_violation')
                )

            # Check cache
            cache_key = self._get_cache_key('ask', community_id, question)
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                logger.info(
                    f"Ask cache hit",
                    extra={
                        'community_id': community_id,
                        'question': question[:50]
                    }
                )
                processing_time = int((time.time() - start_time) * 1000)
                return ResearchResult(
                    success=True,
                    content=cached_result['content'],
                    tokens_used=0,
                    processing_time_ms=processing_time,
                    was_cached=True
                )

            # Get community context from mem0
            context = await self._get_community_context(community_id, question)

            # Build prompt with context
            context_str = ""
            if context:
                context_str = "\n\nContext from community memory:\n"
                for idx, memory in enumerate(context, 1):
                    context_str += f"{idx}. {memory['content']}\n"

            user_prompt = f"Question: {question}{context_str}"

            # Generate response with AI
            response = await self._generate_ai_response(
                system_prompt=self.SYSTEM_PROMPTS['ask'],
                user_prompt=user_prompt,
                community_id=community_id,
                user_id=user_id,
                context_type='ask'
            )

            if not response['success']:
                logger.error(
                    f"AI generation failed for ask",
                    extra={
                        'community_id': community_id,
                        'error': response.get('error')
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Failed to generate answer. Please try again.",
                    tokens_used=response.get('tokens_used', 0),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="generation_failed"
                )

            # Cache result
            await self._save_to_cache(
                cache_key,
                {'content': response['content']},
                Config.CACHE_TTL_RESEARCH
            )

            processing_time = int((time.time() - start_time) * 1000)
            logger.info(
                f"Ask completed",
                extra={
                    'community_id': community_id,
                    'tokens_used': response['tokens_used'],
                    'processing_time_ms': processing_time
                }
            )

            return ResearchResult(
                success=True,
                content=response['content'],
                tokens_used=response['tokens_used'],
                processing_time_ms=processing_time,
                was_cached=False
            )

        except Exception as e:
            logger.error(
                f"Ask error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return ResearchResult(
                success=False,
                content="An error occurred while processing your question. Please try again.",
                tokens_used=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                was_cached=False,
                blocked_reason="internal_error"
            )

    async def recall(
        self,
        community_id: int,
        user_id: str,
        topic: str
    ) -> ResearchResult:
        """
        Recall memories from mem0 using semantic search.

        This method doesn't use the LLM - it's pure retrieval.

        Args:
            community_id: Community identifier
            user_id: User identifier
            topic: Topic to recall memories about

        Returns:
            ResearchResult with success status and recalled memories
        """
        start_time = time.time()

        try:
            # Check rate limit
            rate_limit_key = f"recall:{community_id}:{user_id}"
            if not await self._check_rate_limit(rate_limit_key, 'memory'):
                logger.warning(
                    f"Rate limit exceeded for recall",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="rate_limit"
                )

            # Check cache
            cache_key = self._get_cache_key('recall', community_id, topic)
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                logger.info(
                    f"Recall cache hit",
                    extra={
                        'community_id': community_id,
                        'topic': topic[:50]
                    }
                )
                processing_time = int((time.time() - start_time) * 1000)
                return ResearchResult(
                    success=True,
                    content=cached_result['content'],
                    tokens_used=0,
                    processing_time_ms=processing_time,
                    was_cached=True
                )

            # Search mem0 for memories
            memories = await self.mem0_service.search(
                query=topic,
                community_id=community_id,
                limit=Config.VECTOR_SEARCH_LIMIT
            )

            if not memories:
                content = f"No memories found related to '{topic}'."
                logger.info(
                    f"Recall found no memories",
                    extra={
                        'community_id': community_id,
                        'topic': topic[:50]
                    }
                )
            else:
                # Format memories for display
                content = f"Recalled {len(memories)} memories about '{topic}':\n\n"
                for idx, memory in enumerate(memories, 1):
                    score = memory.get('score', 0.0)
                    memory_content = memory.get('content', '')
                    metadata = memory.get('metadata', {})
                    timestamp = metadata.get('timestamp', 'unknown time')

                    content += f"{idx}. [{timestamp}] (relevance: {score:.2f})\n"
                    content += f"   {memory_content}\n\n"

                logger.info(
                    f"Recall completed",
                    extra={
                        'community_id': community_id,
                        'memories_found': len(memories)
                    }
                )

            # Cache result
            await self._save_to_cache(
                cache_key,
                {'content': content},
                Config.CACHE_TTL_MEMORY
            )

            processing_time = int((time.time() - start_time) * 1000)

            return ResearchResult(
                success=True,
                content=content,
                tokens_used=0,  # No LLM used for recall
                processing_time_ms=processing_time,
                was_cached=False
            )

        except Exception as e:
            logger.error(
                f"Recall error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return ResearchResult(
                success=False,
                content="An error occurred while recalling memories. Please try again.",
                tokens_used=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                was_cached=False,
                blocked_reason="internal_error"
            )

    async def summarize(
        self,
        community_id: int,
        user_id: str,
        duration_minutes: Optional[int] = None
    ) -> ResearchResult:
        """
        Summarize recent conversation or stream.

        Note: This is a premium feature check should be done by caller.

        Args:
            community_id: Community identifier
            user_id: User identifier
            duration_minutes: Duration to summarize (default: last hour)

        Returns:
            ResearchResult with success status and summary
        """
        start_time = time.time()

        try:
            # Check rate limit
            rate_limit_key = f"summarize:{community_id}:{user_id}"
            if not await self._check_rate_limit(rate_limit_key, 'research'):
                logger.warning(
                    f"Rate limit exceeded for summarize",
                    extra={
                        'community_id': community_id,
                        'user_id': user_id
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="rate_limit"
                )

            # Get recent context
            if duration_minutes is None:
                duration_minutes = 60  # Default to last hour

            context = await self._get_recent_context(
                community_id,
                duration_minutes
            )

            if not context:
                logger.info(
                    f"No context found for summarize",
                    extra={
                        'community_id': community_id,
                        'duration_minutes': duration_minutes
                    }
                )
                return ResearchResult(
                    success=True,
                    content=f"No messages found in the last {duration_minutes} minutes to summarize.",
                    tokens_used=0,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False
                )

            # Check cache (include duration in cache key)
            cache_key = self._get_cache_key(
                'summarize',
                community_id,
                f"{duration_minutes}min"
            )
            cached_result = await self._get_from_cache(cache_key)
            if cached_result:
                logger.info(
                    f"Summarize cache hit",
                    extra={
                        'community_id': community_id,
                        'duration_minutes': duration_minutes
                    }
                )
                processing_time = int((time.time() - start_time) * 1000)
                return ResearchResult(
                    success=True,
                    content=cached_result['content'],
                    tokens_used=0,
                    processing_time_ms=processing_time,
                    was_cached=True
                )

            # Build context string for summarization
            context_str = f"Messages from the last {duration_minutes} minutes:\n\n"
            for msg in context:
                user = msg.get('user', 'unknown')
                content = msg.get('content', '')
                timestamp = msg.get('timestamp', '')
                context_str += f"[{timestamp}] {user}: {content}\n"

            user_prompt = f"Summarize the following conversation:\n\n{context_str}"

            # Generate response with AI
            response = await self._generate_ai_response(
                system_prompt=self.SYSTEM_PROMPTS['summarize'],
                user_prompt=user_prompt,
                community_id=community_id,
                user_id=user_id,
                context_type='summarize'
            )

            if not response['success']:
                logger.error(
                    f"AI generation failed for summarize",
                    extra={
                        'community_id': community_id,
                        'error': response.get('error')
                    }
                )
                return ResearchResult(
                    success=False,
                    content="Failed to generate summary. Please try again.",
                    tokens_used=response.get('tokens_used', 0),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                    was_cached=False,
                    blocked_reason="generation_failed"
                )

            # Cache result (shorter TTL for summaries as they're time-sensitive)
            await self._save_to_cache(
                cache_key,
                {'content': response['content']},
                Config.CACHE_TTL_CONTEXT
            )

            # Save to mem0 as a memory
            await self._save_to_mem0(
                community_id=community_id,
                content=response['content'],
                metadata={
                    'type': 'summary',
                    'duration_minutes': duration_minutes,
                    'user_id': user_id
                }
            )

            processing_time = int((time.time() - start_time) * 1000)
            logger.info(
                f"Summarize completed",
                extra={
                    'community_id': community_id,
                    'tokens_used': response['tokens_used'],
                    'processing_time_ms': processing_time,
                    'messages_summarized': len(context)
                }
            )

            return ResearchResult(
                success=True,
                content=response['content'],
                tokens_used=response['tokens_used'],
                processing_time_ms=processing_time,
                was_cached=False
            )

        except Exception as e:
            logger.error(
                f"Summarize error: {describe_db_error(e)}",
                extra={'community_id': community_id}
            )
            return ResearchResult(
                success=False,
                content="An error occurred while generating summary. Please try again.",
                tokens_used=0,
                processing_time_ms=int((time.time() - start_time) * 1000),
                was_cached=False,
                blocked_reason="internal_error"
            )

    # =========================================================================
    # PRIVATE HELPER METHODS
    # =========================================================================

    async def _check_rate_limit(
        self,
        key: str,
        limit_type: str
    ) -> bool:
        """
        Check if rate limit is exceeded.

        Args:
            key: Rate limit key
            limit_type: Type of limit ('research' or 'memory')

        Returns:
            True if within limit, False if exceeded
        """
        try:
            return await self.rate_limiter.check_limit(key, limit_type)
        except Exception as e:
            logger.error(f"Rate limit check error: {describe_db_error(e)}")
            # Fail open - allow request if rate limiter fails
            return True

    async def _check_safety(
        self,
        content: str,
        community_id: int
    ) -> Dict[str, Any]:
        """
        Check content safety.

        Args:
            content: Content to check
            community_id: Community identifier

        Returns:
            Dict with 'safe' boolean and optional 'reason'
        """
        try:
            return await self.safety_layer.check(content, community_id)
        except Exception as e:
            logger.error(f"Safety check error: {describe_db_error(e)}")
            # Fail open - allow content if safety check fails
            return {'safe': True}

    def _get_cache_key(
        self,
        command_type: str,
        community_id: int,
        content: str
    ) -> str:
        """
        Generate cache key for a request.

        Args:
            command_type: Type of command (research, ask, etc.)
            community_id: Community identifier
            content: Content to hash

        Returns:
            Cache key string
        """
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        return f"{command_type}:{community_id}:{content_hash}"

    async def _get_from_cache(self, key: str) -> Optional[Dict[str, Any]]:
        """
        Get value from Redis cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None
        """
        try:
            import json
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
            return None
        except Exception as e:
            logger.error(f"Cache get error: {describe_db_error(e)}")
            return None

    async def _save_to_cache(
        self,
        key: str,
        value: Dict[str, Any],
        ttl: int
    ) -> None:
        """
        Save value to Redis cache.

        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds
        """
        try:
            import json
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception as e:
            logger.error(f"Cache save error: {describe_db_error(e)}")

    async def _check_semantic_cache(
        self,
        community_id: int,
        query: str,
        query_type: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check for semantically similar cached results in mem0.

        Args:
            community_id: Community identifier
            query: Query string
            query_type: Type of query

        Returns:
            Similar cached result or None
        """
        try:
            results = await self.mem0_service.search(
                query=query,
                community_id=community_id,
                limit=1,
                filter_metadata={'type': query_type}
            )

            if results and len(results) > 0:
                result = results[0]
                similarity = result.get('score', 0.0)

                if similarity >= Config.SEMANTIC_CACHE_THRESHOLD:
                    return {
                        'content': result.get('content'),
                        'similarity': similarity
                    }

            return None
        except Exception as e:
            logger.error(f"Semantic cache check error: {describe_db_error(e)}")
            return None

    async def _save_to_mem0(
        self,
        community_id: int,
        content: str,
        metadata: Dict[str, Any]
    ) -> None:
        """
        Save content to mem0 for future semantic lookups.

        Args:
            community_id: Community identifier
            content: Content to save
            metadata: Additional metadata
        """
        try:
            await self.mem0_service.add(
                content=content,
                community_id=community_id,
                metadata=metadata
            )
        except Exception as e:
            logger.error(f"mem0 save error: {describe_db_error(e)}")

    async def _get_community_context(
        self,
        community_id: int,
        query: str
    ) -> list:
        """
        Get relevant community context from mem0.

        Args:
            community_id: Community identifier
            query: Query to find relevant context

        Returns:
            List of relevant memories
        """
        try:
            return await self.mem0_service.search(
                query=query,
                community_id=community_id,
                limit=5
            )
        except Exception as e:
            logger.error(f"Get community context error: {describe_db_error(e)}")
            return []

    async def _get_recent_context(
        self,
        community_id: int,
        duration_minutes: int
    ) -> list:
        """
        Get recent context messages for summarization.

        This would typically come from a context service or database.

        Args:
            community_id: Community identifier
            duration_minutes: How many minutes back to look

        Returns:
            List of recent messages
        """
        try:
            # TODO: Implement context retrieval from context service
            # For now, return empty list
            logger.warning(
                "Context retrieval not yet implemented",
                extra={
                    'community_id': community_id,
                    'duration_minutes': duration_minutes
                }
            )
            return []
        except Exception as e:
            logger.error(f"Get recent context error: {describe_db_error(e)}")
            return []

    async def _generate_ai_response(
        self,
        system_prompt: str,
        user_prompt: str,
        community_id: int,
        user_id: str,
        context_type: str
    ) -> Dict[str, Any]:
        """
        Generate AI response using configured provider.

        Args:
            system_prompt: System prompt for AI
            user_prompt: User prompt for AI
            community_id: Community identifier
            user_id: User identifier
            context_type: Type of context (research, ask, etc.)

        Returns:
            Dict with success, content, tokens_used, and optional error
        """
        try:
            response = await self.ai_provider.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                community_id=community_id,
                user_id=user_id,
                metadata={'context_type': context_type}
            )

            if not response:
                return {
                    'success': False,
                    'error': 'Empty response from AI provider',
                    'tokens_used': 0
                }

            return {
                'success': True,
                'content': response.get('content', ''),
                'tokens_used': response.get('tokens_used', 0)
            }

        except Exception as e:
            logger.error(f"AI generation error: {describe_db_error(e)}")
            return {
                'success': False,
                'error': describe_db_error(e),
                'tokens_used': 0
            }
