"""
Price Tracker Service
======================

Business logic for game price comparison and deal tracking: SearXNG web
search, AI synthesis, and caching.

Two search modes:
- Price search (``!or/price``): SearXNG + LLM synthesis for price comparison
- Deals search (``!game deals``): SearXNG + LLM synthesis for current deals

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

PRICE_SEARCH_SYSTEM_PROMPT = (
    "You are a game price comparison expert. Given web search results, "
    "provide structured pricing information: store name, current price, "
    "discount percentage, and sale end date if available. Always compare "
    "across multiple stores. Note if a game is free-to-play."
)

DEALS_SEARCH_SYSTEM_PROMPT = (
    "You are a game deals expert. Given web search results about current "
    "game deals and sales, list the best deals: game name, store, price, "
    "discount percentage, and sale end date. Focus on significant discounts "
    "(>30%)."
)


@dataclass(slots=True)
class PriceTrackerResult:
    """Result of a price tracking operation."""
    success: bool
    content: str
    sources: list[dict] = field(default_factory=list)
    game_name: str | None = None
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
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason,
        }


class PriceTrackerService:
    """
    Price tracking with SearXNG metasearch, AI synthesis, and caching.

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

    async def search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> PriceTrackerResult:
        """
        Full AI-augmented price search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis exact cache check
        4. SearXNG web search (price-scoped with site: filters)
        5. AI synthesis of results
        6. Cache result + log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'price_lookup',
            )
            if not rl.allowed:
                return PriceTrackerResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return PriceTrackerResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)
            game_name = query.strip()

            # 3. Redis cache
            cache_key = self._cache_key('price', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform,
                    query, cached.get('game_name'),
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return PriceTrackerResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search with site: scoping
            search_query = (
                f"{query} price buy "
                "site:steampowered.com OR site:epicgames.com "
                "OR site:isthereanydeal.com"
            )

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'PRICE_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return PriceTrackerResult(
                    success=True,
                    content="No pricing results found for your query.",
                    game_name=game_name,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'PRICE_SEARCH_SYSTEM_PROMPT',
                PRICE_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {game_name}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Provide structured pricing comparison based on these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache and log
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': game_name,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, user_id, platform,
                query, game_name, len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return PriceTrackerResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Price search failed: %s", describe_db_error(exc))
            return PriceTrackerResult(
                success=False,
                content="An internal error occurred during price search.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def deals_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> PriceTrackerResult:
        """
        Deals search — SearXNG + AI synthesis for current game deals.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG search (deals-scoped)
        5. AI synthesis of results
        6. Cache + log
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'deals_lookup',
            )
            if not rl.allowed:
                return PriceTrackerResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return PriceTrackerResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)
            game_name = query.strip()

            # 3. Cache
            cache_key = self._cache_key('deals', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform,
                    query, cached.get('game_name'),
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return PriceTrackerResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} game deals sale discount"

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'DEALS_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return PriceTrackerResult(
                    success=True,
                    content="No deals found for your query.",
                    game_name=game_name,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'DEALS_SEARCH_SYSTEM_PROMPT',
                DEALS_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game/Query: {game_name}\n\n"
                f"Search results:\n{context_text}\n\n"
                "List the best current deals based on these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache and log
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': game_name,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, user_id, platform,
                query, game_name, len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return PriceTrackerResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Deals search failed: %s", describe_db_error(exc))
            return PriceTrackerResult(
                success=False,
                content="An internal error occurred during deals search.",
                blocked_reason="internal_error",
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
        return f"price_tracker:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'PRICE_CACHE_TTL', 900)
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
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to price_tracker_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO price_tracker_searches
                    (community_id, user_id, platform, query, game_name,
                     result_count, was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                [community_id, user_id, platform, query, game_name,
                 result_count, was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log search: %s", describe_db_error(exc))
