"""
Event/Tournament Lookup Service
================================

Business logic for event and tournament lookup: SearXNG web search,
AI synthesis, caching, and search logging.

Two search modes:
- Events (``!or/events``): Search for upcoming/ongoing gaming events
- Tournament (``!or/tournament``): Search for specific tournament details

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

EVENT_SEARCH_SYSTEM_PROMPT = (
    "You are an esports and gaming event expert. Given web search results "
    "about gaming events, format the results as: Event/Tournament name, "
    "Dates, Game, Teams/Players, Format, Current stage or results, and "
    "Where to watch (stream links). Focus on upcoming and ongoing events."
)

TOURNAMENT_SEARCH_SYSTEM_PROMPT = (
    "You are an esports tournament analyst. Given web search results about "
    "a specific tournament, provide: Tournament name, Organizer, Dates, "
    "Format (group stage, playoffs, etc.), Prize pool, Participating teams, "
    "Current standings or bracket status, and Where to watch."
)


@dataclass(slots=True)
class EventLookupResult:
    """Result of an event/tournament lookup operation."""
    success: bool
    content: str
    sources: list[dict] = field(default_factory=list)
    game_name: str | None = None
    tournament_name: str | None = None
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
            'tournament_name': self.tournament_name,
            'search_type': self.search_type,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason,
        }


class EventLookupService:
    """
    Event/tournament lookup with SearXNG metasearch, AI synthesis, and caching.

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

    async def search_events(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        game_name: str | None = None,
    ) -> EventLookupResult:
        """
        Event search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG web search (esports-biased)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'event_lookup')
            if not rl.allowed:
                return EventLookupResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type="events",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return EventLookupResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type="events",
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            cache_key = self._cache_key('events', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    cached.get('game_name'), None, 'events',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return EventLookupResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    search_type="events",
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = (
                f"{query} esports tournament schedule "
                "site:liquipedia.net OR site:hltv.org OR site:vlr.gg OR site:start.gg"
            )
            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'EVENT_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return EventLookupResult(
                    success=True,
                    content="No event results found for your query.",
                    game_name=game_name,
                    search_type="events",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'EVENT_SEARCH_SYSTEM_PROMPT',
                EVENT_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {game_name or 'Unknown'}\n"
                f"Query: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Format these event results with event name, dates, game, "
                "teams, format, stage, and where to watch."
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
                'result_count': len(sources),
            })

            # 7. Log search
            await self._log_search(
                community_id, user_id, platform, query,
                game_name, None, 'events',
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return EventLookupResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                search_type="events",
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Event search failed: %s", describe_db_error(exc))
            return EventLookupResult(
                success=False,
                content="An internal error occurred during event search.",
                blocked_reason="internal_error",
                search_type="events",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def search_tournament(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        tournament_name: str | None = None,
    ) -> EventLookupResult:
        """
        Tournament search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG web search (tournament-biased)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'tournament_lookup')
            if not rl.allowed:
                return EventLookupResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type="tournament",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return EventLookupResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type="tournament",
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            cache_key = self._cache_key('tournament', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    None, cached.get('tournament_name'), 'tournament',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return EventLookupResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    tournament_name=cached.get('tournament_name'),
                    search_type="tournament",
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = (
                f"{query} tournament bracket results "
                "site:liquipedia.net OR site:hltv.org OR site:vlr.gg"
            )
            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'EVENT_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return EventLookupResult(
                    success=True,
                    content="No tournament results found for your query.",
                    tournament_name=tournament_name,
                    search_type="tournament",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'TOURNAMENT_SEARCH_SYSTEM_PROMPT',
                TOURNAMENT_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Tournament: {tournament_name or 'Unknown'}\n"
                f"Query: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Provide tournament details: name, organizer, dates, format, "
                "prize pool, teams, standings, and where to watch."
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
                'tournament_name': tournament_name,
                'result_count': len(sources),
            })

            # 7. Log search
            await self._log_search(
                community_id, user_id, platform, query,
                None, tournament_name, 'tournament',
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return EventLookupResult(
                success=True,
                content=result_content,
                sources=sources,
                tournament_name=tournament_name,
                search_type="tournament",
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Tournament search failed: %s", describe_db_error(exc))
            return EventLookupResult(
                success=False,
                content="An internal error occurred during tournament search.",
                blocked_reason="internal_error",
                search_type="tournament",
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
        return f"event_lookup:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'EVENT_CACHE_TTL', 1800)
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
        tournament_name: str | None,
        search_type: str,
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to event_lookup_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO event_lookup_searches
                    (community_id, user_id, platform, query, game_name,
                     tournament_name, search_type, result_count,
                     was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                [community_id, user_id, platform, query, game_name,
                 tournament_name, search_type, result_count,
                 was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log event search: %s", describe_db_error(exc))
