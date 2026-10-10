"""
Clip/Highlight Researcher Service
==================================

Business logic for clip and highlight research: SearXNG web search,
AI synthesis, caching, and search logging.

Two search modes:
- Clips (``!or/clips``): Search for game clips/highlights by game and topic
- Highlights (``!or/highlight``): Search for player highlights and best plays

Patterns reused from: services/game_lookup_service.py
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from flask_core import describe_db_error

logger = logging.getLogger(__name__)

CLIP_SEARCH_SYSTEM_PROMPT = (
    "You are a gaming clip curator. Given web search results about game clips "
    "and videos, describe what each video demonstrates. For each result provide: "
    "title, channel/creator, URL, and a one-line summary of the content. "
    "Focus on educational and entertaining clips."
)

HIGHLIGHT_SEARCH_SYSTEM_PROMPT = (
    "You are a gaming highlight analyst. Given web search results about a "
    "player's highlights, describe their best plays and notable moments. "
    "For each result provide: title, channel, URL, and what makes it notable."
)


@dataclass(slots=True)
class ClipResearcherResult:
    """Result of a clip/highlight research operation."""
    success: bool
    content: str
    sources: list[dict] = field(default_factory=list)
    game_name: str | None = None
    topic: str | None = None
    player_name: str | None = None
    search_type: str | None = None
    tokens_used: int = 0
    processing_time_ms: int = 0
    was_cached: bool = False
    blocked_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'content': self.content,
            'sources': self.sources,
            'game_name': self.game_name,
            'topic': self.topic,
            'player_name': self.player_name,
            'search_type': self.search_type,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason,
        }


class ClipResearcherService:
    """
    Clip/highlight research with SearXNG metasearch, AI synthesis, and caching.

    Dependencies are injected via constructor (same pattern as GameLookupService).
    """

    def __init__(
        self,
        dal,
        redis_client,
        ai_provider,
        safety_layer,
        rate_limiter,
        searxng_service,
        get_mem0_fn,
        config,
    ):
        self.dal = dal
        self.redis = redis_client
        self.ai_provider = ai_provider
        self.safety_layer = safety_layer
        self.rate_limiter = rate_limiter
        self.searxng = searxng_service
        self._get_mem0 = get_mem0_fn
        self.config = config

    # =========================================================================
    # Public search methods
    # =========================================================================

    async def search_clips(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        game_name: str | None = None,
        topic: str | None = None,
    ) -> ClipResearcherResult:
        """
        Clip search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG web search (video-biased)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'clip_research')
            if not rl.allowed:
                return ClipResearcherResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type="clips",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return ClipResearcherResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type="clips",
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            cache_key = self._cache_key('clips', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    game_name, topic, None, 'clips',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return ClipResearcherResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    topic=cached.get('topic'),
                    search_type="clips",
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} clips gameplay site:youtube.com OR site:twitch.tv"
            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'CLIP_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return ClipResearcherResult(
                    success=True,
                    content="No clip results found for your query.",
                    game_name=game_name,
                    topic=topic,
                    search_type="clips",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'CLIP_SEARCH_SYSTEM_PROMPT',
                CLIP_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {game_name or 'Unknown'}\n"
                f"Topic: {topic or 'General'}\n"
                f"Query: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Curate these clip results with title, creator, URL, "
                "and a summary of each."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache result
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': game_name,
                'topic': topic,
                'result_count': len(sources),
            })

            # 7. Log search
            await self._log_search(
                community_id, user_id, platform, query,
                game_name, topic, None, 'clips',
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return ClipResearcherResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                topic=topic,
                search_type="clips",
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Clip search failed: %s", describe_db_error(exc))
            return ClipResearcherResult(
                success=False,
                content="An internal error occurred during clip search.",
                blocked_reason="internal_error",
                search_type="clips",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def search_highlights(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        player_name: str | None = None,
    ) -> ClipResearcherResult:
        """
        Highlight/player search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG web search (highlight-biased)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'clip_research')
            if not rl.allowed:
                return ClipResearcherResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type="highlight",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return ClipResearcherResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type="highlight",
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            cache_key = self._cache_key('highlight', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    None, None, cached.get('player_name'), 'highlight',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return ClipResearcherResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    player_name=cached.get('player_name'),
                    search_type="highlight",
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} highlights best plays site:youtube.com"
            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'CLIP_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return ClipResearcherResult(
                    success=True,
                    content="No highlight results found for your query.",
                    player_name=player_name,
                    search_type="highlight",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'HIGHLIGHT_SEARCH_SYSTEM_PROMPT',
                HIGHLIGHT_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Player: {player_name or 'Unknown'}\n"
                f"Query: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Analyze these highlights with title, channel, URL, "
                "and what makes each notable."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache result
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'player_name': player_name,
                'result_count': len(sources),
            })

            # 7. Log search
            await self._log_search(
                community_id, user_id, platform, query,
                None, None, player_name, 'highlight',
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return ClipResearcherResult(
                success=True,
                content=result_content,
                sources=sources,
                player_name=player_name,
                search_type="highlight",
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Highlight search failed: %s", describe_db_error(exc))
            return ClipResearcherResult(
                success=False,
                content="An internal error occurred during highlight search.",
                blocked_reason="internal_error",
                search_type="highlight",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _cache_key(
        self,
        search_type: str,
        community_id: int,
        query: str,
    ) -> str:
        raw = f"{search_type}:{community_id}:{query.lower().strip()}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"clip_research:{search_type}:{community_id}:{h}"

    async def _get_cache(self, key: str) -> dict | None:
        if not self.redis:
            return None
        try:
            data = await self.redis.get(key)
            return json.loads(data) if data else None
        except Exception:
            return None

    async def _set_cache(self, key: str, value: dict) -> None:
        if not self.redis:
            return
        try:
            ttl = getattr(self.config, 'CLIP_CACHE_TTL', 3600)
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception as exc:
            logger.warning("Cache set failed: %s", describe_db_error(exc))

    async def _log_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        game_name: str | None,
        topic: str | None,
        player_name: str | None,
        search_type: str,
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to clip_researcher_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO clip_researcher_searches
                    (community_id, user_id, platform, query, game_name,
                     topic, player_name, search_type, result_count,
                     was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                [community_id, user_id, platform, query, game_name,
                 topic, player_name, search_type, result_count,
                 was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log clip search: %s", describe_db_error(exc))
