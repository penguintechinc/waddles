"""
Game Lookup Service
====================

Business logic for game data lookup: SearXNG web search, AI synthesis,
caching, and community game management.

Two search modes:
- Full (``!or/game``): SearXNG + Qdrant RAG + LLM synthesis
- Quick (``!game search``): SearXNG results only, no AI

Patterns reused from: services/research_service.py
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from flask_core import describe_db_error
from flask_core.ai_guard import (
    render_search_results,
    sanitize_model_output,
    wrap_untrusted,
)

logger = logging.getLogger(__name__)

GAME_SEARCH_SYSTEM_PROMPT = (
    "You are a gaming expert assistant. Given web search results about a game, "
    "synthesize a clear, accurate answer. Cite sources where possible. "
    "Focus on factual in-game data (stats, locations, mechanics). "
    "If the search results are insufficient, say so honestly."
)


@dataclass(slots=True)
class GameLookupResult:
    """Result of a game lookup operation."""
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


class GameLookupService:
    """
    Game lookup with SearXNG metasearch, AI synthesis, and caching.

    Dependencies are injected via constructor (same pattern as ResearchService).
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
    ) -> GameLookupResult:
        """
        Full AI-augmented game search pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Match query to a configured game
        4. Redis exact cache check
        5. SearXNG web search (game-scoped)
        6. AI synthesis of results
        7. Cache result + log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'game')
            if not rl.allowed:
                return GameLookupResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return GameLookupResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Match game
            games = await self.get_community_games(community_id)
            matched_game = self._match_game(query, games)

            # 4. Redis cache
            cache_key = self._cache_key('full', community_id, query, matched_game)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, matched_game, user_id, platform,
                    query, 'full', cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return GameLookupResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 5. SearXNG search
            search_query = query
            engines = None
            game_name = None
            if matched_game:
                game_name = matched_game['name']
                search_query = self.searxng.build_game_query(
                    query, game_name,
                    matched_game.get('search_keywords'),
                )
                engines = matched_game.get('preferred_engines')

            searx_resp = await self.searxng.search(
                search_query,
                engines=engines,
                limit=self.config.GAME_SEARCH_MAX_RESULTS,
            )

            if not searx_resp.results:
                return GameLookupResult(
                    success=True,
                    content="No results found for your query.",
                    game_name=game_name,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 6. AI synthesis
            context_text = render_search_results(searx_resp.results)
            system_prompt = getattr(
                self.config, 'GAME_SEARCH_SYSTEM_PROMPT',
                GAME_SEARCH_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Game: {wrap_untrusted(game_name or 'Unknown', max_chars=300)}\n"
                f"Question: {wrap_untrusted(query, max_chars=300)}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Synthesize a clear, concise answer based on these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 7. Cache and log
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'game_name': game_name,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, matched_game, user_id, platform,
                query, 'full', len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return GameLookupResult(
                success=True,
                content=result_content,
                sources=sources,
                game_name=game_name,
                tokens_used=tokens_used,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Game search failed: %s", describe_db_error(exc))
            return GameLookupResult(
                success=False,
                content="An internal error occurred during game search.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def quick_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> GameLookupResult:
        """
        Quick search — SearXNG results only, no AI synthesis.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Match game
        4. Redis cache check
        5. SearXNG search
        6. Format top results directly
        7. Cache + log
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'game_quick')
            if not rl.allowed:
                return GameLookupResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return GameLookupResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Match game
            games = await self.get_community_games(community_id)
            matched_game = self._match_game(query, games)

            # 4. Cache
            cache_key = self._cache_key('quick', community_id, query, matched_game)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, matched_game, user_id, platform,
                    query, 'quick', cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return GameLookupResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    game_name=cached.get('game_name'),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 5. SearXNG search
            search_query = query
            engines = None
            game_name = None
            if matched_game:
                game_name = matched_game['name']
                search_query = self.searxng.build_game_query(
                    query, game_name,
                    matched_game.get('search_keywords'),
                )
                engines = matched_game.get('preferred_engines')

            searx_resp = await self.searxng.search(
                search_query,
                engines=engines,
                limit=5,  # fewer results for quick mode
            )

            if not searx_resp.results:
                return GameLookupResult(
                    success=True,
                    content="No results found for your query.",
                    game_name=game_name,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 6. Format directly (no AI)
            sources = [r.to_dict() for r in searx_resp.results]
            lines = []
            for i, r in enumerate(searx_resp.results[:5], 1):
                lines.append(f"{i}. **{r.title}**\n   {r.content}\n   {r.url}")
            content = '\n'.join(lines)
            if game_name:
                content = f"**{game_name}** — Quick Search Results:\n\n{content}"
            # Web snippets go straight to chat here: strip beacons / mass pings.
            content = sanitize_model_output(content)

            # 7. Cache + log
            await self._set_cache(cache_key, {
                'content': content,
                'sources': sources,
                'game_name': game_name,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, matched_game, user_id, platform,
                query, 'quick', len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return GameLookupResult(
                success=True,
                content=content,
                sources=sources,
                game_name=game_name,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Quick game search failed: %s", describe_db_error(exc))
            return GameLookupResult(
                success=False,
                content="An internal error occurred during game search.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    # =========================================================================
    # Game management (community admin)
    # =========================================================================

    async def get_community_games(self, community_id: int) -> list[dict]:
        """Get active games configured for a community."""
        try:
            rows = await self.dal.execute(
                """
                SELECT id, name, name_normalized, abbreviations,
                       search_keywords, wiki_url, preferred_engines,
                       is_active, created_at
                FROM game_lookup_games
                WHERE community_id = $1 AND is_active = TRUE
                ORDER BY name
                """,
                [community_id],
            )
            return [dict(r) for r in rows] if rows else []
        except Exception as exc:
            logger.error("Failed to load community games: %s", describe_db_error(exc))
            return []

    async def add_game(self, community_id: int, admin_id: str, game_data: dict) -> dict:
        """Add or update a game for a community."""
        name = game_data['name']
        name_normalized = name.strip().lower()
        abbreviations = game_data.get('abbreviations', [])
        search_keywords = game_data.get('search_keywords', [])
        wiki_url = game_data.get('wiki_url', '')
        preferred_engines = game_data.get('preferred_engines', [])

        rows = await self.dal.execute(
            """
            INSERT INTO game_lookup_games
                (community_id, name, name_normalized, abbreviations,
                 search_keywords, wiki_url, preferred_engines, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
            ON CONFLICT (community_id, name_normalized) DO UPDATE SET
                name = EXCLUDED.name,
                abbreviations = EXCLUDED.abbreviations,
                search_keywords = EXCLUDED.search_keywords,
                wiki_url = EXCLUDED.wiki_url,
                preferred_engines = EXCLUDED.preferred_engines,
                is_active = TRUE,
                updated_at = NOW()
            RETURNING id, name, name_normalized
            """,
            [community_id, name, name_normalized, abbreviations,
             search_keywords, wiki_url, preferred_engines],
        )
        row = rows[0] if rows else {}
        logger.info(
            "Game added: %s for community %s by %s",
            name, community_id, admin_id,
        )
        return dict(row) if row else {}

    async def remove_game(self, community_id: int, game_id: int) -> bool:
        """Deactivate a game (soft delete)."""
        try:
            await self.dal.execute(
                """
                UPDATE game_lookup_games
                SET is_active = FALSE, updated_at = NOW()
                WHERE id = $1 AND community_id = $2
                """,
                [game_id, community_id],
            )
            return True
        except Exception as exc:
            logger.error("Failed to remove game %s: %s", game_id, describe_db_error(exc))
            return False

    async def get_cached_items(
        self,
        community_id: int,
        game_id: int | None = None,
        item_type: str | None = None,
    ) -> list[dict]:
        """Get cached game items with optional filters."""
        conditions = ["community_id = $1", "expires_at > NOW()"]
        params: list = [community_id]
        idx = 2

        if game_id is not None:
            conditions.append(f"game_id = ${idx}")
            params.append(game_id)
            idx += 1
        if item_type:
            conditions.append(f"item_type = ${idx}")
            params.append(item_type)
            idx += 1

        where = ' AND '.join(conditions)
        rows = await self.dal.execute(
            f"""
            SELECT id, game_id, name, item_type, description,
                   source_url, source_engine, metadata, hit_count, expires_at
            FROM game_lookup_items
            WHERE {where}
            ORDER BY hit_count DESC
            LIMIT 100
            """,
            params,
        )
        return [dict(r) for r in rows] if rows else []

    async def copy_template_games(
        self,
        community_id: int,
        game_names: list[str],
    ) -> int:
        """
        Copy pre-seeded template games (community_id=1) into a community.

        Returns the number of games copied.
        """
        if not game_names:
            return 0

        normalized = [n.strip().lower() for n in game_names]
        # Fetch templates
        placeholders = ', '.join(f'${i}' for i in range(2, len(normalized) + 2))
        rows = await self.dal.execute(
            f"""
            SELECT name, name_normalized, abbreviations, search_keywords,
                   wiki_url, preferred_engines
            FROM game_lookup_games
            WHERE community_id = 1 AND name_normalized IN ({placeholders})
            """,
            [1] + normalized,  # $1 is already used for community_id in the query
        )

        if not rows:
            return 0

        copied = 0
        for row in rows:
            try:
                await self.dal.execute(
                    """
                    INSERT INTO game_lookup_games
                        (community_id, name, name_normalized, abbreviations,
                         search_keywords, wiki_url, preferred_engines, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
                    ON CONFLICT (community_id, name_normalized) DO NOTHING
                    """,
                    [community_id, row['name'], row['name_normalized'],
                     row['abbreviations'], row['search_keywords'],
                     row['wiki_url'], row['preferred_engines']],
                )
                copied += 1
            except Exception as exc:
                logger.warning("Failed to copy game %s: %s", row['name'], describe_db_error(exc))

        return copied

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _match_game(self, query: str, games: list[dict]) -> dict | None:
        """Fuzzy-match a query to one of the community's configured games."""
        query_lower = query.lower()
        for game in games:
            # Check name
            if game['name_normalized'] in query_lower:
                return game
            # Check abbreviations
            abbrevs = game.get('abbreviations') or []
            for abbr in abbrevs:
                if abbr.lower() in query_lower.split():
                    return game
        return None

    def _cache_key(
        self,
        search_type: str,
        community_id: int,
        query: str,
        game: dict | None,
    ) -> str:
        game_id = game['id'] if game else 0
        raw = f"{search_type}:{community_id}:{game_id}:{query.lower().strip()}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"game_lookup:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'GAME_CACHE_TTL', 7200)
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception as exc:
            logger.warning("Cache set failed: %s", describe_db_error(exc))

    async def _log_search(
        self,
        community_id: int,
        game: dict | None,
        user_id: str,
        platform: str,
        query: str,
        search_type: str,
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to game_lookup_searches table."""
        try:
            game_id = game['id'] if game else None
            await self.dal.execute(
                """
                INSERT INTO game_lookup_searches
                    (community_id, game_id, user_id, platform, query,
                     search_type, result_count, was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                [community_id, game_id, user_id, platform, query,
                 search_type, result_count, was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log search: %s", describe_db_error(exc))
