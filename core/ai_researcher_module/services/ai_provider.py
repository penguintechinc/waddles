"""
AI Provider Abstraction for Research Module
============================================

Multi-provider AI service supporting:
- Ollama (direct connection with TLS)
- OpenAI (API integration)
- Anthropic Claude (API integration)

Features:
- Async HTTP client with connection pooling
- Semaphore-based concurrency limiting
- Comprehensive error handling and timeouts
- Processing time tracking
- Token usage monitoring
"""

import asyncio
import httpx
import ssl
import logging
import time
from enum import Enum
from typing import Optional, Any
from dataclasses import dataclass, field

from config import Config
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


class AIProvider(Enum):
    """AI provider types"""
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass(slots=True)
class AIResponse:
    """
    AI response container with metadata.

    Attributes:
        content: Generated text content
        model: Model used for generation
        tokens_used: Number of tokens consumed
        processing_time_ms: Time taken to generate (milliseconds)
    """
    content: str
    model: str
    tokens_used: int
    processing_time_ms: int


class AIProviderService:
    """
    Unified AI provider service with abstraction layer.

    Supports multiple providers with consistent interface:
    - Connection pooling via httpx.AsyncClient
    - Concurrency limiting via semaphore
    - Automatic timeout handling
    - Comprehensive logging
    """

    def __init__(self, config: Config):
        """
        Initialize AI provider service.

        Args:
            config: Configuration object
        """
        self.config = config
        self.provider = AIProvider(config.AI_PROVIDER.lower())

        # Concurrency control
        self.semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_LLM_CALLS)

        # HTTP client (created lazily)
        self._client: Optional[httpx.AsyncClient] = None

        # Provider-specific setup
        self._setup_provider()

        logger.info(
            f"Initialized AIProviderService: provider={self.provider.value}, "
            f"max_concurrent={config.MAX_CONCURRENT_LLM_CALLS}"
        )

    def _setup_provider(self):
        """Configure provider-specific settings"""
        match self.provider:
            case AIProvider.OLLAMA:
                protocol = 'https' if self.config.OLLAMA_USE_TLS else 'http'
                self.base_url = (
                    f"{protocol}://{self.config.OLLAMA_HOST}:"
                    f"{self.config.OLLAMA_PORT}"
                )
                self.timeout = self.config.OLLAMA_TIMEOUT
                self.ssl_context = self._create_ssl_context()
                logger.info(f"Ollama provider: {self.base_url}")

            case AIProvider.OPENAI:
                self.base_url = "https://api.openai.com/v1"
                self.timeout = 60
                self.ssl_context = None
                logger.info("OpenAI provider initialized")

            case AIProvider.ANTHROPIC:
                self.base_url = "https://api.anthropic.com/v1"
                self.timeout = 60
                self.ssl_context = None
                logger.info("Anthropic provider initialized")

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        """
        Create SSL context for TLS connections (Ollama).

        Returns:
            SSL context or None
        """
        if not self.config.OLLAMA_USE_TLS:
            return None

        if self.config.OLLAMA_VERIFY_SSL:
            context = ssl.create_default_context()

            if self.config.OLLAMA_CERT_PATH:
                try:
                    context.load_verify_locations(self.config.OLLAMA_CERT_PATH)
                    logger.info(
                        f"Loaded SSL cert: {self.config.OLLAMA_CERT_PATH}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to load SSL cert: {describe_db_error(e)}")

            return context
        else:
            # Disable verification (dev only)
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            logger.warning("SSL verification disabled!")
            return context

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Get or create HTTP client with connection pooling.

        Returns:
            Async HTTP client
        """
        if self._client is None:
            verify_param = self.ssl_context if self.ssl_context else True

            self._client = httpx.AsyncClient(
                verify=verify_param,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20
                )
            )

        return self._client

    async def close(self):
        """Close HTTP client and cleanup resources"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 500
    ) -> AIResponse:
        """
        Generate AI response with concurrency limiting.

        Args:
            prompt: User prompt text
            system_prompt: Optional system instruction
            temperature: Generation temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate

        Returns:
            AIResponse with content and metadata

        Raises:
            Exception: On generation failure
        """
        async with self.semaphore:
            start_time = time.perf_counter()

            try:
                # Route to provider-specific implementation
                match self.provider:
                    case AIProvider.OLLAMA:
                        response = await self._generate_ollama(
                            prompt, system_prompt, temperature, max_tokens
                        )
                    case AIProvider.OPENAI:
                        response = await self._generate_openai(
                            prompt, system_prompt, temperature, max_tokens
                        )
                    case AIProvider.ANTHROPIC:
                        response = await self._generate_anthropic(
                            prompt, system_prompt, temperature, max_tokens
                        )
                    case _:
                        raise ValueError(f"Unknown provider: {self.provider}")

                processing_time = int((time.perf_counter() - start_time) * 1000)
                response.processing_time_ms = processing_time

                logger.info(
                    f"Generated response: provider={self.provider.value}, "
                    f"model={response.model}, tokens={response.tokens_used}, "
                    f"time={processing_time}ms"
                )

                return response

            except Exception as e:
                processing_time = int((time.perf_counter() - start_time) * 1000)
                logger.error(
                    f"Generation failed: provider={self.provider.value}, "
                    f"error={describe_db_error(e)}, time={processing_time}ms"
                )
                raise

    async def generate_with_context(
        self,
        prompt: str,
        context: list[dict],
        system_prompt: Optional[str] = None
    ) -> AIResponse:
        """
        Generate with conversation context.

        Args:
            prompt: User prompt text
            context: List of message dicts with 'role' and 'content'
            system_prompt: Optional system instruction

        Returns:
            AIResponse with content and metadata
        """
        # Build contextualized prompt
        context_text = "\n".join([
            f"{msg.get('role', 'user')}: {msg.get('content', '')}"
            for msg in context[-10:]  # Last 10 messages
        ])

        full_prompt = f"{context_text}\n\nCurrent query: {prompt}"

        return await self.generate(
            prompt=full_prompt,
            system_prompt=system_prompt
        )

    async def embed(self, text: str) -> list[float]:
        """
        Generate text embeddings.

        Args:
            text: Text to embed

        Returns:
            List of embedding values

        Raises:
            NotImplementedError: If provider doesn't support embeddings
        """
        match self.provider:
            case AIProvider.OLLAMA:
                return await self._embed_ollama(text)
            case _:
                raise NotImplementedError(
                    f"Embeddings not implemented for {self.provider.value}"
                )

    async def health_check(self) -> bool:
        """
        Check if AI provider is available.

        Returns:
            True if healthy, False otherwise
        """
        try:
            match self.provider:
                case AIProvider.OLLAMA:
                    return await self._health_check_ollama()
                case AIProvider.OPENAI:
                    return await self._health_check_openai()
                case AIProvider.ANTHROPIC:
                    return await self._health_check_anthropic()
                case _:
                    return False

        except Exception as e:
            logger.error(f"Health check failed: {describe_db_error(e)}")
            return False

    # ========================================================================
    # OLLAMA IMPLEMENTATION
    # ========================================================================

    async def _generate_ollama(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> AIResponse:
        """Generate response using Ollama"""
        client = await self._get_client()

        # Build full prompt with system instruction
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        payload = {
            "model": self.config.OLLAMA_MODEL,
            "prompt": full_prompt,
            "temperature": temperature,
            "options": {
                "num_predict": max_tokens
            },
            "stream": False
        }

        try:
            response = await client.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout
            )

            response.raise_for_status()
            data = response.json()

            content = data.get('response', '')
            tokens = data.get('eval_count', 0)

            return AIResponse(
                content=content,
                model=self.config.OLLAMA_MODEL,
                tokens_used=tokens,
                processing_time_ms=0  # Will be set by caller
            )

        except httpx.TimeoutException:
            logger.error(f"Ollama request timed out after {self.timeout}s")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP error: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Ollama generation error: {describe_db_error(e)}")
            raise

    async def _embed_ollama(self, text: str) -> list[float]:
        """Generate embeddings using Ollama"""
        client = await self._get_client()

        payload = {
            "model": self.config.MEM0_EMBEDDER_MODEL,
            "prompt": text
        }

        try:
            response = await client.post(
                f"{self.base_url}/api/embeddings",
                json=payload,
                timeout=30.0
            )

            response.raise_for_status()
            data = response.json()

            return data.get('embedding', [])

        except Exception as e:
            logger.error(f"Ollama embedding error: {describe_db_error(e)}")
            raise

    async def _health_check_ollama(self) -> bool:
        """Health check for Ollama"""
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/api/tags",
                timeout=5.0
            )
            return response.status_code == 200

        except (httpx.ConnectError, httpx.TimeoutException):
            logger.error(f"Cannot connect to Ollama at {self.base_url}")
            return False
        except Exception as e:
            logger.error(f"Ollama health check failed: {describe_db_error(e)}")
            return False

    # ========================================================================
    # OPENAI IMPLEMENTATION (STUBS)
    # ========================================================================

    async def _generate_openai(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> AIResponse:
        """Generate response using OpenAI (stub)"""
        raise NotImplementedError(
            "OpenAI provider not yet implemented. "
            "Use AI_PROVIDER=ollama or implement OpenAI integration."
        )

    async def _health_check_openai(self) -> bool:
        """Health check for OpenAI (stub)"""
        raise NotImplementedError("OpenAI health check not implemented")

    # ========================================================================
    # ANTHROPIC IMPLEMENTATION (STUBS)
    # ========================================================================

    async def _generate_anthropic(
        self,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int
    ) -> AIResponse:
        """Generate response using Anthropic (stub)"""
        raise NotImplementedError(
            "Anthropic provider not yet implemented. "
            "Use AI_PROVIDER=ollama or implement Anthropic integration."
        )

    async def _health_check_anthropic(self) -> bool:
        """Health check for Anthropic (stub)"""
        raise NotImplementedError("Anthropic health check not implemented")
