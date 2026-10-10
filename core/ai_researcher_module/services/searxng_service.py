"""
SearXNG Metasearch Service
============================

HTTP client for the self-hosted SearXNG metasearch engine.
Used by the Game Lookup sub-module for real-time web search.

Patterns reused from: services/ai_provider.py (lazy httpx client, semaphore)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx
from flask_core import describe_db_error

logger = logging.getLogger(__name__)

# SECURITY (PII in logs): httpx logs every request URL at INFO ("HTTP Request: GET <url>"),
# and a SearXNG URL embeds the user's research question as its `q=` query string.
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(slots=True)
class SearchResult:
    """A single search result from SearXNG."""
    title: str
    url: str
    content: str      # snippet text
    engine: str
    score: float

    def to_dict(self) -> dict:
        return {
            'title': self.title,
            'url': self.url,
            'content': self.content,
            'engine': self.engine,
            'score': self.score,
        }


@dataclass(slots=True)
class SearXNGResponse:
    """Aggregated response from a SearXNG search."""
    results: list[SearchResult]
    query: str
    total_results: int
    search_time_ms: int

    def to_dict(self) -> dict:
        return {
            'results': [r.to_dict() for r in self.results],
            'query': self.query,
            'total_results': self.total_results,
            'search_time_ms': self.search_time_ms,
        }


class SearXNGService:
    """HTTP client for SearXNG metasearch API."""

    def __init__(self, base_url: str, timeout: int = 15, max_concurrent: int = 5):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=10,
                ),
            )
        return self._client

    async def search(
        self,
        query: str,
        engines: list[str] | None = None,
        categories: list[str] | None = None,
        limit: int = 10,
    ) -> SearXNGResponse:
        """
        Execute a search against SearXNG.

        Args:
            query: Search query string
            engines: Specific engines to use (e.g. ['google', 'wikipedia'])
            categories: SearXNG categories to search
            limit: Maximum number of results to return

        Returns:
            SearXNGResponse with ranked results
        """
        async with self.semaphore:
            start_time = time.perf_counter()
            client = await self._get_client()

            params: dict = {
                'q': query,
                'format': 'json',
            }
            if engines:
                params['engines'] = ','.join(engines)
            if categories:
                params['categories'] = ','.join(categories)

            try:
                resp = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "SearXNG HTTP error: status=%s (request URL omitted: it embeds the query)",
                    exc.response.status_code,
                )
                return SearXNGResponse(
                    results=[], query=query, total_results=0,
                    search_time_ms=int((time.perf_counter() - start_time) * 1000),
                )
            except Exception as exc:
                logger.error("SearXNG request failed: %s", describe_db_error(exc))
                return SearXNGResponse(
                    results=[], query=query, total_results=0,
                    search_time_ms=int((time.perf_counter() - start_time) * 1000),
                )

            raw_results = data.get('results', [])
            results = []
            for item in raw_results[:limit]:
                results.append(SearchResult(
                    title=item.get('title', ''),
                    url=item.get('url', ''),
                    content=item.get('content', ''),
                    engine=item.get('engine', ''),
                    score=float(item.get('score', 0.0)),
                ))

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            return SearXNGResponse(
                results=results,
                query=query,
                total_results=len(results),
                search_time_ms=elapsed_ms,
            )

    def build_game_query(
        self,
        query: str,
        game_name: str,
        keywords: list[str] | None = None,
    ) -> str:
        """
        Build an enriched search query for a specific game.

        Example: "Andromeda ship stats" + "Star Citizen" + ["RSI"]
              -> "Star Citizen Andromeda ship stats RSI wiki"
        """
        parts = [game_name, query]
        if keywords:
            parts.extend(keywords[:2])  # limit to avoid overly long queries
        parts.append('wiki')
        return ' '.join(parts)

    async def health_check(self) -> bool:
        """Check if SearXNG is reachable."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/healthz")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self):
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
