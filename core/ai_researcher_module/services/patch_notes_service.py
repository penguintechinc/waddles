"""
Patch Notes Service
====================

Business logic for patch notes tracking: SearXNG web search, AI synthesis,
caching, and search logging.

Two search modes:
- Full (``!or/patch``): SearXNG + LLM synthesis
- Quick (``!or/changelog``): SearXNG results only, no AI

Patterns reused from: services/game_lookup_service.py
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from flask_core import describe_db_error

logger = logging.getLogger(__name__)

PATCH_NOTES_SYSTEM_PROMPT = (
    "You are a gaming patch notes expert. Given web search results about game "
    "updates, summarize the key changes: buffs, nerfs, new content, bug fixes, "
    "and major balance changes. Be specific about what changed. If results seem "
    "outdated, note the date."
)


@dataclass(slots=True)
class PatchNotesResult:
    """Result of a patch notes lookup operation."""
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


class PatchNotesService:
    """
    Patch notes tracking with SearXNG metasearch, AI synthesis, and caching.

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
    ) -> PatchNotesResult:
        """
        Full AI-augmented patch notes search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis exact cache check
        4. SearXNG web search (patch-scoped)
        5. AI synthesis of results
        6. Cache result
        7. Log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'patch_notes',
            )
            if not rl.allowed:
                return PatchNotesResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return PatchNotesResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            game_name = query.strip()
            cache_key = self._cache_key('full', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    cached.get('game_name', game_name),
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return PatchNotesResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} patch notes update changelog"

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'GAME_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return PatchNotesResult(
                    success=True,
                    content="No patch notes found for your query.",
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
                self.config, 'PATCH_NOTES_SYSTEM_PROMPT',
                PATCH_NOTES_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {game_name}\n"
                f"Question: {query}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Summarize the latest patch notes and changes based on "
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
                'game_name': game_name,
                'result_count': len(sources),
            })

            # 7. Log
            await self._log_search(
                community_id, user_id, platform, query, game_name,
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return PatchNotesResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Patch notes search failed: %s", describe_db_error(exc))
            return PatchNotesResult(
                success=False,
                content="An internal error occurred during patch notes search.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def quick_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> PatchNotesResult:
        """
        Quick search — SearXNG results only, no AI synthesis.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis cache check
        4. SearXNG search
        5. Format top results directly
        6. Cache + log
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(
                community_id, user_id, 'patch_notes',
            )
            if not rl.allowed:
                return PatchNotesResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return PatchNotesResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Cache
            game_name = query.strip()
            cache_key = self._cache_key('quick', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform, query,
                    cached.get('game_name', game_name),
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return PatchNotesResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} patch notes update changelog"

            searx_resp = await self.searxng.search(
                search_query,
                limit=5,  # fewer results for quick mode
            )

            if not searx_resp.results:
                return PatchNotesResult(
                    success=True,
                    content="No patch notes found for your query.",
                    game_name=game_name,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 5. Format directly (no AI)
            sources = [r.to_dict() for r in searx_resp.results]
            lines = []
            for i, r in enumerate(searx_resp.results[:5], 1):
                lines.append(f"{i}. **{r.title}**\n   {r.content}\n   {r.url}")
            content = '\n'.join(lines)
            content = (
                f"**{game_name}** — Recent Patch Notes:\n\n{content}"
            )

            # 6. Cache + log
            await self._set_cache(cache_key, {
                'content': content,
                'sources': sources,
                'game_name': game_name,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, user_id, platform, query, game_name,
                len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return PatchNotesResult(
                success=True,
                content=content,
                sources=sources,
                game_name=game_name,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Quick patch notes search failed: %s", describe_db_error(exc))
            return PatchNotesResult(
                success=False,
                content="An internal error occurred during patch notes search.",
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
        return f"patch_notes:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'PATCH_CACHE_TTL', 1800)
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
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to patch_notes_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO patch_notes_searches
                    (community_id, user_id, platform, query, game_name,
                     result_count, was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                [community_id, user_id, platform, query, game_name,
                 result_count, was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log patch notes search: %s", describe_db_error(exc))
