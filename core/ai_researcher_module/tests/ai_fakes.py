"""Shared fakes for the researcher module's offline provider tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FakeConfig:
    """Just the `Config` attributes `AIProviderService` reads."""

    AI_PROVIDER: str = "ollama"
    MAX_CONCURRENT_LLM_CALLS: int = 1
    OLLAMA_HOST: str = "ollama.test"
    OLLAMA_PORT: str = "11434"
    OLLAMA_USE_TLS: bool = False
    OLLAMA_MODEL: str = "text-model"
    OLLAMA_TIMEOUT: int = 5
    OLLAMA_CERT_PATH: str = ""
    OLLAMA_VERIFY_SSL: bool = True
    OLLAMA_SUPPORTS_JSON: bool = False
    OLLAMA_DISABLE_THINKING: bool = True
    MEM0_EMBEDDER_MODEL: str = "embed-model"


@dataclass(slots=True)
class FakeRedis:
    """Minimal async Redis stand-in (get/setex) -- the cache is not under test."""

    store: dict[str, str] = field(default_factory=dict)

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value


@dataclass(slots=True)
class FakeRateLimiter:
    """Always-allow limiter exposing the `check_limit(key, limit_type) -> bool` shape the service calls."""

    async def check_limit(self, key: str, limit_type: str) -> bool:
        return True


@dataclass(slots=True)
class FakeMem0:
    """Vector-memory stand-in: returns canned context, records writes."""

    memories: list[dict[str, Any]] = field(default_factory=list)
    added: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.memories

    async def add(self, **kwargs: Any) -> None:
        self.added.append(kwargs)
