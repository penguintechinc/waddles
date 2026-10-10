"""
Tech Troubleshooter Service
=============================

Business logic for tech troubleshooting: SearXNG web search, AI synthesis,
caching, and dangerous-command safety scanning.

Two search modes:
- Full (``!or/fix``): SearXNG + LLM synthesis with safety scanning
- Quick (``!or/troubleshoot``): SearXNG results only, no AI

Patterns reused from: services/game_lookup_service.py
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from flask_core import describe_db_error
from flask_core.ai_guard import (
    render_search_results,
    sanitize_model_output,
    wrap_untrusted,
)

logger = logging.getLogger(__name__)

TECH_FIX_SYSTEM_PROMPT = (
    "You are a tech support expert. Given web search results about a technical "
    "issue, provide step-by-step troubleshooting instructions. Be specific and "
    "actionable. NEVER include dangerous commands without explicit safety warnings. "
    "If a fix involves data loss risk, clearly label it [CAUTION]."
)

# Patterns that indicate potentially dangerous commands
DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\b',
    r'\bformat\b',
    r'\bdd\s+if=',
    r'\bmkfs\b',
    r'\bfdisk\b',
    r'\bdeltree\b',
    r'\brd\s+/s\b',
]

_DANGEROUS_RE = re.compile('|'.join(DANGEROUS_PATTERNS), re.IGNORECASE)


@dataclass(slots=True)
class TechTroubleshooterResult:
    """Result of a tech troubleshooting operation."""
    success: bool
    content: str
    sources: list[dict] = field(default_factory=list)
    tokens_used: int = 0
    processing_time_ms: int = 0
    was_cached: bool = False
    blocked_reason: str | None = None
    safety_flagged: bool = False

    def to_dict(self) -> dict:
        return {
            'success': self.success,
            'content': self.content,
            'sources': self.sources,
            'tokens_used': self.tokens_used,
            'processing_time_ms': self.processing_time_ms,
            'was_cached': self.was_cached,
            'blocked_reason': self.blocked_reason,
            'safety_flagged': self.safety_flagged,
        }


class TechTroubleshooterService:
    """
    Tech troubleshooting with SearXNG metasearch, AI synthesis, and caching.

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

    async def fix(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> TechTroubleshooterResult:
        """
        Full AI-augmented tech troubleshooting pipeline.

        Flow:
        1. Rate limit check
        2. Safety sanitize
        3. Redis exact cache check
        4. SearXNG web search (tech-scoped)
        5. AI synthesis of results
        6. Dangerous-command safety scan
        7. Cache result + log search
        """
        start_time = time.time()
        try:
            # 1. Rate limit
            rl = await self.rate_limiter.increment(community_id, user_id, 'tech_fix')
            if not rl.allowed:
                return TechTroubleshooterResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety check
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return TechTroubleshooterResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Redis cache
            cache_key = self._cache_key('fix', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform,
                    query, cached.get('safety_flagged', False),
                    cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return TechTroubleshooterResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    was_cached=True,
                    safety_flagged=cached.get('safety_flagged', False),
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} fix solution troubleshoot"

            searx_resp = await self.searxng.search(
                search_query,
                limit=getattr(self.config, 'TECH_SEARCH_MAX_RESULTS', 10),
            )

            if not searx_resp.results:
                return TechTroubleshooterResult(
                    success=True,
                    content="No results found for your query.",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            sources = [r.to_dict() for r in searx_resp.results]

            # 5. AI synthesis
            context_text = render_search_results(searx_resp.results)
            system_prompt = getattr(
                self.config, 'TECH_FIX_SYSTEM_PROMPT',
                TECH_FIX_SYSTEM_PROMPT,
            )
            user_prompt = (
                f"Technical issue: {wrap_untrusted(query, max_chars=300)}\n\n"
                f"Search results:\n{context_text}\n\n"
                "Provide step-by-step troubleshooting instructions "
                "based on these results."
            )

            ai_resp = await self.ai_provider.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
            )

            result_content = ai_resp.content
            tokens_used = ai_resp.tokens_used

            # 6. Dangerous-command safety scan
            safety_flagged = False
            result_content, safety_flagged = self._scan_dangerous_commands(
                result_content,
            )

            # 7. Cache and log
            await self._set_cache(cache_key, {
                'content': result_content,
                'sources': sources,
                'result_count': len(sources),
                'safety_flagged': safety_flagged,
            })

            await self._log_search(
                community_id, user_id, platform,
                query, safety_flagged, len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return TechTroubleshooterResult(
                success=True,
                content=result_content,
                sources=sources,
                tokens_used=tokens_used,
                safety_flagged=safety_flagged,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Tech fix failed: %s", describe_db_error(exc))
            return TechTroubleshooterResult(
                success=False,
                content="An internal error occurred during troubleshooting.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    async def troubleshoot(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
    ) -> TechTroubleshooterResult:
        """
        Quick troubleshoot — SearXNG results only, no AI synthesis.

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
                community_id, user_id, 'tech_fix',
            )
            if not rl.allowed:
                return TechTroubleshooterResult(
                    success=False,
                    content="Rate limit exceeded. Please try again later.",
                    blocked_reason="rate_limit",
                )

            # 2. Safety
            safety = self.safety_layer.check_prompt(query)
            if not safety.is_safe:
                return TechTroubleshooterResult(
                    success=False,
                    content="Query blocked by safety filter.",
                    blocked_reason=safety.blocked_reason,
                )
            query = self.safety_layer.sanitize_prompt(query)

            # 3. Cache
            cache_key = self._cache_key('troubleshoot', community_id, query)
            cached = await self._get_cache(cache_key)
            if cached:
                await self._log_search(
                    community_id, user_id, platform,
                    query, False, cached.get('result_count', 0), True,
                    int((time.time() - start_time) * 1000),
                )
                return TechTroubleshooterResult(
                    success=True,
                    content=cached['content'],
                    sources=cached.get('sources', []),
                    was_cached=True,
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 4. SearXNG search
            search_query = f"{query} fix solution troubleshoot"

            searx_resp = await self.searxng.search(
                search_query,
                limit=5,  # fewer results for quick mode
            )

            if not searx_resp.results:
                return TechTroubleshooterResult(
                    success=True,
                    content="No results found for your query.",
                    processing_time_ms=int((time.time() - start_time) * 1000),
                )

            # 5. Format directly (no AI)
            sources = [r.to_dict() for r in searx_resp.results]
            lines = []
            for i, r in enumerate(searx_resp.results[:5], 1):
                lines.append(f"{i}. **{r.title}**\n   {r.content}\n   {r.url}")
            content = f"**Troubleshoot Results:**\n\n" + '\n'.join(lines)
            # Web snippets go straight to chat here: strip beacons / mass pings.
            content = sanitize_model_output(content)

            # 6. Cache + log
            await self._set_cache(cache_key, {
                'content': content,
                'sources': sources,
                'result_count': len(sources),
            })

            await self._log_search(
                community_id, user_id, platform,
                query, False, len(sources), False,
                int((time.time() - start_time) * 1000),
            )

            return TechTroubleshooterResult(
                success=True,
                content=content,
                sources=sources,
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as exc:
            logger.error("Quick troubleshoot failed: %s", describe_db_error(exc))
            return TechTroubleshooterResult(
                success=False,
                content="An internal error occurred during troubleshooting.",
                blocked_reason="internal_error",
                processing_time_ms=int((time.time() - start_time) * 1000),
            )

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _scan_dangerous_commands(self, content: str) -> tuple[str, bool]:
        """
        Scan AI-generated content for dangerous command patterns.

        If found, prefix each dangerous line with ``[CAUTION] `` and return
        the modified content along with a flag indicating the content was
        flagged.
        """
        flagged = False
        lines = content.split('\n')
        result_lines = []
        for line in lines:
            if _DANGEROUS_RE.search(line):
                flagged = True
                if not line.lstrip().startswith('[CAUTION]'):
                    line = f"[CAUTION] {line}"
            result_lines.append(line)
        return '\n'.join(result_lines), flagged

    def _cache_key(
        self,
        search_type: str,
        community_id: int,
        query: str,
    ) -> str:
        raw = f"{search_type}:{community_id}:{query.lower().strip()}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"tech_fix:{search_type}:{community_id}:{h}"

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
            ttl = getattr(self.config, 'TECH_CACHE_TTL', 14400)
            await self.redis.setex(key, ttl, json.dumps(value))
        except Exception as exc:
            logger.warning("Cache set failed: %s", describe_db_error(exc))

    async def _log_search(
        self,
        community_id: int,
        user_id: str,
        platform: str,
        query: str,
        safety_flagged: bool,
        result_count: int,
        was_cached: bool,
        processing_time_ms: int,
    ) -> None:
        """Log search to tech_troubleshooter_searches table."""
        try:
            await self.dal.execute(
                """
                INSERT INTO tech_troubleshooter_searches
                    (community_id, user_id, platform, query, issue_text,
                     safety_flagged, result_count, was_cached, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                [community_id, user_id, platform, query, query,
                 safety_flagged, result_count, was_cached, processing_time_ms],
            )
        except Exception as exc:
            logger.warning("Failed to log search: %s", describe_db_error(exc))
