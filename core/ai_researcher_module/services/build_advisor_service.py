"""
Build/Loadout Advisor Service
===============================

Business logic for build and loadout recommendations: SearXNG web search,
AI synthesis, caching, and search logging.

Three search modes:
- Build search (``!or/build``): SearXNG + LLM synthesis for build guides
- Meta search (``!or/meta``): SearXNG + LLM synthesis for meta/tier lists
- Quick (future): SearXNG results only, no AI

Patterns reused from: services/game_lookup_service.py
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from flask_core import describe_db_error

logger = logging.getLogger(__name__)

BUILD_SEARCH_SYSTEM_PROMPT = (
    "You are a gaming build and loadout expert. Given web search results, "
    "synthesize current meta recommendations for the requested game and "
    "class/character. Include: recommended skills/abilities, gear/equipment, "
    "and playstyle tips. Label if information may be outdated."
)

META_SEARCH_SYSTEM_PROMPT = (
    "You are a gaming meta analyst. Given web search results, summarize "
    "the current meta: top tier characters/classes, dominant strategies, "
    "and recent tier list changes. Be specific about rankings."
)


@dataclass(slots=True)
class BuildAdvisorResult:
    """Result of a build advisor lookup operation."""
    success: bool
    content: str
    sources: list[dict] = field(default_factory=list)
    game_name: str | None = None
    class_name: str | None = None
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
            'class_name': self.class_name,
            'search_type': self.search_type,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason,
        }


class BuildAdvisorService:
    """
    Build and loadout advisor with SearXNG metasearch, AI synthesis, and caching.

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
        *,
        class_name: str = '',
        game_name: str = '',
    ) -> BuildAdvisorResult:
        """
        Full AI-augmented build search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis exact cache check
        4. SearXNG web search (build-scoped)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'build_advisor',
            )
            if not rl.allowed:
                return BuildAdvisorResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type='build',
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return BuildAdvisorResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type='build',
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            resolved_game = game_name or query.strip()
            resolved_class = class_name or ''
            cache_key = self._cache_key(
                'build', community_id, query, resolved_class,
            )
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    cached.get('game_name', resolved_game),
                    cached.get('class_name', resolved_class),
                    'build',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return BuildAdvisorResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    class_name=cached.get('class_name'),
                    search_type='build',
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_parts = [query, "build guide loadout"]
            if class_name:
                search_parts.insert(1, class_name)
            search_query = ' '.join(search_parts)

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'GAME_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return BuildAdvisorResult(
                    success=True,
                    content="No build guides found for your query.",
                    game_name=resolved_game,
                    class_name=resolved_class,
                    search_type='build',
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'BUILD_SEARCH_SYSTEM_PROMPT',
                BUILD_SEARCH_SYSTEM_PROMPT,
            )
            class_label = f" ({resolved_class})" if resolved_class else ""
            user_prompt = (
                f"Game: {resolved_game}{class_label}\n"
                f"Question: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Synthesize a clear build/loadout recommendation based on "
                "these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': resolved_game,
                'class_name': resolved_class,
                'result_count': len(sources),
            })

            # 7. Log
            await self._log_search(
                community_id, user_id, platform, query, resolved_game,
                resolved_class, 'build', len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return BuildAdvisorResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=resolved_game,
                class_name=resolved_class,
                search_type='build',
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Build search failed: %s", describe_db_error(exc))
            return BuildAdvisorResult(
                success=False,
                content="An internal error occurred during build search.",
                blocked_reason="internal_error",
                search_type='build',
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def meta_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        *,
        game_name: str = '',
    ) -> BuildAdvisorResult:
        """
        Meta/tier list search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG search (meta-scoped)
        5. AI synthesis
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'meta_lookup',
            )
            if not rl.allowed:
                return BuildAdvisorResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                    search_type='meta',
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return BuildAdvisorResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                    search_type='meta',
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            resolved_game = game_name or query.strip()
            cache_key = self._cache_key('meta', community_id, query, '')
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    cached.get('game_name', resolved_game),
                    '', 'meta',
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return BuildAdvisorResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    search_type='meta',
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} meta tier list"

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'GAME_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return BuildAdvisorResult(
                    success=True,
                    content="No meta information found for your query.",
                    game_name=resolved_game,
                    search_type='meta',
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = '\n'.join(
                f"[{r.title}]({r.url}): {r.content}"
                for r in searx_resp.results
            )
            system_prompt = getattr(
                self.config, 'META_SEARCH_SYSTEM_PROMPT',
                META_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {resolved_game}\n"
                f"Question: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Summarize the current meta and tier rankings based on "
                "these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Cache
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': resolved_game,
                'result_count': len(sources),
            })

            # 7. Log
            await self._log_search(
                community_id, user_id, platform, query, resolved_game,
                '', 'meta', len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return BuildAdvisorResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=resolved_game,
                search_type='meta',
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Meta search failed: %s", describe_db_error(exc))
            return BuildAdvisorResult(
                success=False,
                content="An internal error occurred during meta search.",
                blocked_reason="internal_error",
                search_type='meta',
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
        class_name: str,
    ) -> str:
        raw = (
            f"{search_type}:{community_id}:"
            f"{class_name.lower().strip()}:{query.lower().strip()}"
        )
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"build_advisor:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'BUILD_CACHE_TTL', 10800)
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception as exc:
            logger.warning("Cache set failed: %s", describe_db_error(exc))

    async def _log_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        game_name: str,
        class_name: str,
        search_type: str,
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to build_advisor_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO build_advisor_searches
                    (community_id, user_id, platform, query, game_name,
                     class_name, search_type, result_count, was_cached,
                     processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                [community_id, user_id, platform, query, game_name,
                 class_name, search_type, result_count, was_cached,
                 processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log build advisor search: %s", describe_db_error(exc))
