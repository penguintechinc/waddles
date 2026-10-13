"""
AI Service with Provider Abstraction
=====================================

Unified AI service supporting multiple providers:
- Ollama: Direct connection with TLS
- WaddleAI: Centralized proxy for OpenAI, Claude, MCP
"""

import logging
import time
from typing import Optional, Dict, Any, Protocol
from dataclasses import dataclass

from flask_core.ai_telemetry import AITelemetry

from config import Config

logger = logging.getLogger(__name__)

# Spans + duration/error metrics for every chat-reply generation (PII-free; no-op
# unless an OTel provider is configured via the standard OTLP env vars).
telemetry = AITelemetry("waddles.ai_interaction.ai_service")


class AIProvider(Protocol):
    """Protocol defining AI provider interface"""

    async def health_check(self) -> bool:
        """Check if provider is available"""
        ...

    async def generate_response(
        self,
        message_content: str,
        message_type: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> Optional[str]:
        """Generate AI response"""
        ...

    async def get_available_models(self) -> list:
        """Get list of available models"""
        ...


@dataclass(slots=True)
class AIService:
    """
    Main AI service with provider abstraction.

    Uses structural pattern matching (Python 3.13) for provider selection.
    """

    provider: AIProvider

    @classmethod
    def create(cls):
        """
        Factory method to create AIService with configured provider.

        Returns:
            AIService instance with appropriate provider
        """
        provider_type = Config.AI_PROVIDER.lower()

        # Python 3.13 structural pattern matching
        match provider_type:
            case 'ollama':
                from .ollama_provider import OllamaProvider
                provider = OllamaProvider()
                logger.info("Initialized Ollama provider (direct connection)")

            case 'waddleai':
                from .waddleai_provider import WaddleAIProvider
                provider = WaddleAIProvider()
                logger.info("Initialized WaddleAI proxy provider")

            case _:
                raise ValueError(
                    f"Unknown AI provider: {provider_type}. "
                    f"Must be 'ollama' or 'waddleai'"
                )

        return cls(provider=provider)

    async def health_check(self) -> bool:
        """
        Check provider health.

        Returns:
            True if healthy, False otherwise
        """
        try:
            return await self.provider.health_check()
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    async def generate_response(
        self,
        message_content: str,
        message_type: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> Optional[str]:
        """
        Generate AI response using configured provider.

        Args:
            message_content: Message text
            message_type: Type of message (chatMessage, subscription, etc.)
            user_id: User identifier
            platform: Platform name (twitch, discord, slack)
            context: Additional context (trigger type, history, etc.)

        Returns:
            Generated response text or None if generation failed
        """
        provider_label = type(self.provider).__name__.removesuffix("Provider").lower()
        model_label = str(getattr(self.provider, "model", "unknown"))
        started = time.perf_counter()

        def _record(error_code: Optional[str] = None) -> None:
            telemetry.record_call(
                provider=provider_label,
                model=model_label,
                mode="text",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error_code=error_code,
            )

        try:
            with telemetry.span(
                provider=provider_label, model=model_label, mode="text"
            ):
                response = await self.provider.generate_response(
                    message_content, message_type, user_id, platform, context
                )

            if not response:
                # The provider already logged why (blank/timeout/HTTP error);
                # the canned reply is a deliberate, counted degradation.
                _record(error_code="no_reply")
                return self._get_fallback_response(message_type, context)

            _record()
            return response

        except Exception as e:
            _record(error_code=type(e).__name__)
            logger.error(f"Error generating response: {e}", exc_info=True)
            return self._get_fallback_response(message_type, context)

    async def get_available_models(self) -> list:
        """
        Get list of available models from provider.

        Returns:
            List of model names
        """
        try:
            return await self.provider.get_available_models()
        except Exception as e:
            logger.error(f"Failed to get available models: {e}")
            return []

    def _get_fallback_response(  # noqa: E501
        self, message_type: str, context: Dict[str, Any]
    ) -> str:
        """
        Get fallback response when AI generation fails.

        Args:
            message_type: Type of message
            context: Message context

        Returns:
            Appropriate fallback response
        """
        # Python 3.13 structural pattern matching for fallback responses
        match message_type:
            case 'chatMessage':
                trigger_type = context.get('trigger_type', 'unknown')
                match trigger_type:
                    case 'greeting':
                        return "Hello! 👋"
                    case 'farewell':
                        return "See you later! 👋"
                    case 'question':
                        return (  # noqa: E501
                            "I'm having trouble processing that right now. "
                            "Please try again!"
                        )
                    case _:
                        return "Thanks for chatting! 😊"

            case 'subscription':
                return "Thanks for subscribing! 🎉"

            case 'follow':
                return "Welcome to the community! 👋"

            case 'donation':
                return "Thank you so much for your generosity! 💖"

            case 'cheer':
                return "Thanks for the bits! 🎊"

            case 'raid':
                return "Welcome raiders! 🚀"

            case 'boost':
                return "Thanks for boosting! ⭐"

            case 'member_join':
                return "Welcome! 👋"

            case _:
                return "Thanks for interacting! 😊"
