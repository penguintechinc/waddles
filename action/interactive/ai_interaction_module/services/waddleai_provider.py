"""
WaddleAI Provider
=================

Centralized AI proxy for OpenAI, Claude, MCP, and other providers.
Routes all AI requests (except Ollama) through WaddleAI for centralized
management, security, cost optimization, and intelligent routing.
"""

import httpx
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from config import Config
from .prompt_safety import UNTRUSTED_DATA_NOTICE, wrap_untrusted

logger = logging.getLogger(__name__)


@dataclass
class WaddleAIProvider:
    """
    WaddleAI proxy provider for centralized AI management.

    Features:
    - Intelligent routing to cheapest/fastest provider
    - Security scanning (prompt injection, jailbreak prevention)
    - Token tracking and cost management
    - Multi-provider support (OpenAI, Claude, MCP, etc.)
    - Automatic failover
    """

    base_url: str = field(
        default_factory=lambda: Config.WADDLEAI_BASE_URL
    )
    api_key: str = field(
        default_factory=lambda: Config.WADDLEAI_API_KEY
    )
    model: str = field(
        default_factory=lambda: Config.WADDLEAI_MODEL
    )
    temperature: float = field(  # noqa: E501
        default_factory=lambda: Config.WADDLEAI_TEMPERATURE
    )
    max_tokens: int = field(  # noqa: E501
        default_factory=lambda: Config.WADDLEAI_MAX_TOKENS
    )
    timeout: int = field(
        default_factory=lambda: Config.WADDLEAI_TIMEOUT
    )
    preferred_model: str = field(  # noqa: E501
        default_factory=lambda: Config.WADDLEAI_PREFERRED_MODEL
    )
    headers: Dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self):
        """Initialize provider"""
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # Add preferred model header if specified
        if self.preferred_model:
            self.headers["X-Preferred-Model"] = self.preferred_model

        logger.info(  # noqa: E501
            f"Initialized WaddleAI provider: {self.base_url} "
            f"(Model: {self.model}, "
            f"Preferred: {self.preferred_model or 'auto'})"
        )

    async def health_check(self) -> bool:
        """
        Check if WaddleAI proxy is available.

        Returns:
            True if healthy, False otherwise
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/healthz",
                    timeout=5.0
                )
                return response.status_code == 200

        except Exception as e:
            logger.error(f"WaddleAI health check failed: {e}")
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
        Generate AI response via WaddleAI proxy.

        Args:
            message_content: Message text
            message_type: Type of message
            user_id: User identifier
            platform: Platform name
            context: Additional context

        Returns:
            Generated response or None
        """
        try:
            # Build messages array for OpenAI-compatible API
            messages = self._build_messages(
                message_content, message_type, user_id, platform, context
            )

            # Build request payload
            payload = {
                "model": self.model,  # 'auto' for intelligent routing
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens
            }

            # Make async request to WaddleAI
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers=self.headers,
                    json=payload,
                    timeout=self.timeout
                )

                if response.status_code == 200:
                    data = response.json()
                    content = data['choices'][0]['message']['content']

                    # Log WaddleAI metrics
                    usage = data.get('usage', {})
                    waddleai_tokens = usage.get('waddleai_tokens', 0)
                    waddleai_data = data.get('waddleai', {})
                    actual_provider = waddleai_data.get('provider', 'unknown')
                    actual_model = data.get('model', 'unknown')

                    logger.info(  # noqa: E501
                        f"WaddleAI response: {waddleai_tokens} tokens, "
                        f"provider: {actual_provider}, "
                        f"model: {actual_model}"
                    )

                    # Clean and return response
                    cleaned = self._clean_response(content)
                    if not cleaned:
                        # Never hand the caller a blank "success": say why, so
                        # AIService's canned fallback is a logged decision.
                        logger.error(
                            f"WaddleAI returned an empty completion "
                            f"(model={actual_model}, finish_reason="
                            f"{data['choices'][0].get('finish_reason')!r})"
                        )
                        return None
                    return cleaned

                elif response.status_code == 429:
                    logger.warning("WaddleAI quota exceeded")
                    return None

                elif response.status_code == 401:
                    logger.error(  # noqa: E501
                        "WaddleAI authentication failed - check API key"
                    )
                    return None

                else:
                    logger.error(  # noqa: E501
                        f"WaddleAI request failed: {response.status_code} "
                        f"- {response.text}"
                    )
                    return None

        except httpx.TimeoutException:
            logger.error(  # noqa: E501
                f"WaddleAI request timed out after {self.timeout}s"
            )
            return None
        except Exception as e:
            logger.error(  # noqa: E501
                f"Error generating WaddleAI response: {e}",
                exc_info=True
            )
            return None

    async def get_available_models(self) -> list:
        """
        Get available models from WaddleAI.

        Returns:
            List of model names across all providers
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/v1/models",
                    headers=self.headers,
                    timeout=10.0
                )

                if response.status_code == 200:
                    data = response.json()
                    models = [model['id'] for model in data.get('data', [])]
                    logger.info(f"Found {len(models)} models via WaddleAI")
                    return models

                return []

        except Exception as e:
            logger.error(f"Failed to get models from WaddleAI: {e}")
            return []

    def _build_messages(
        self,
        message_content: str,
        message_type: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> list:
        """
        Build OpenAI-compatible messages array.

        Args:
            message_content: Message text
            message_type: Type of message
            user_id: User identifier
            platform: Platform name
            context: Additional context

        Returns:
            List of message dictionaries
        """
        messages = []

        # Add system message
        system_prompt = self._create_system_prompt(  # noqa: E501
            message_type, platform, context
        )
        messages.append({
            "role": "system",
            "content": system_prompt
        })

        # Add conversation history if available -- each turn is itself
        # untrusted (a prior user/assistant message), delimited the same
        # way as the current message rather than passed through verbatim.
        if (  # noqa: E501
            Config.ENABLE_CHAT_CONTEXT and
            'conversation_history' in context
        ):
            history = context['conversation_history']
            for msg in history[-Config.CONTEXT_HISTORY_LIMIT:]:
                if (  # noqa: E501
                    isinstance(msg, dict) and
                    'role' in msg and 'content' in msg
                ):
                    role = (  # noqa: E501
                        'assistant' if msg['role'] == 'assistant' else 'user'
                    )
                    messages.append({
                        "role": role,
                        "content": wrap_untrusted(str(msg['content']))
                    })

        # Add current user message -- message_content/user_id are the
        # untrusted payload, delimited via wrap_untrusted; the "User X
        # said:" framing is our own trusted text. regression: sec-llm01-audit
        if message_type == 'chatMessage':
            user_prompt = (  # noqa: E501
                f"User {user_id} said:\n{wrap_untrusted(message_content)}"
            )
        else:
            user_prompt = self._create_event_user_message(  # noqa: E501
                message_type, user_id
            )

        messages.append({
            "role": "user",
            "content": user_prompt
        })

        return messages

    def _create_system_prompt(
        self,
        message_type: str,
        platform: str,
        context: Dict[str, Any]
    ) -> str:
        """Create system prompt based on message type"""

        base_prompt = Config.SYSTEM_PROMPT

        # Add platform context
        platform_context = f"\nYou are responding on {platform}."

        # Add message type specific instructions
        match message_type:
            case 'chatMessage':
                trigger_type = context.get('trigger_type', 'unknown')
                match trigger_type:
                    case 'greeting':
                        instruction = (  # noqa: E501
                            " Respond to this greeting warmly and briefly."
                        )
                    case 'farewell':
                        instruction = (  # noqa: E501
                            " Respond to this farewell kindly and briefly."
                        )
                    case 'question':
                        instruction = (  # noqa: E501
                            " Answer this question helpfully and concisely."
                        )
                    case _:
                        instruction = " Respond naturally and helpfully."
                type_context = instruction

            case _:
                type_context = (  # noqa: E501
                    f" Respond enthusiastically to this "
                    f"{message_type} event."
                )

        length_instruction = (
            "\nKeep your response under 200 characters."
        )

        return (  # noqa: E501
            base_prompt + platform_context + type_context +
            length_instruction + "\n\n" + UNTRUSTED_DATA_NOTICE
        )

    def _create_event_user_message(  # noqa: E501
        self, message_type: str, user_id: str
    ) -> str:
        """Create user message for events"""

        # user_id (platform username/display name) is attacker-influenceable
        # -- delimit it before it's templated into the event description.
        safe_user_id = wrap_untrusted(user_id)

        match message_type:
            case 'subscription':
                return (  # noqa: E501
                    f"User {safe_user_id} just subscribed! "
                    f"Generate a celebratory thank you."
                )
            case 'follow':
                return (  # noqa: E501
                    f"User {safe_user_id} just followed! "
                    f"Generate a welcoming thank you."
                )
            case 'donation':
                return (  # noqa: E501
                    f"User {safe_user_id} just donated! "
                    f"Generate a grateful thank you."
                )
            case 'cheer':
                return (  # noqa: E501
                    f"User {safe_user_id} just sent bits! "
                    f"Generate an excited thank you."
                )
            case 'raid':
                return (  # noqa: E501
                    f"User {safe_user_id} raided the channel! "
                    f"Welcome the raiders."
                )
            case 'boost':
                return (  # noqa: E501
                    f"User {safe_user_id} boosted the server! "
                    f"Thank them for boosting."
                )
            case 'member_join':
                return (  # noqa: E501
                    f"User {safe_user_id} just joined! "
                    f"Welcome them warmly."
                )
            case _:
                return (  # noqa: E501
                    f"User {safe_user_id} triggered a {message_type} event! "
                    f"Respond appropriately."
                )

    def _clean_response(self, response: str) -> str:
        """
        Clean and validate AI response.

        Args:
            response: Raw AI response

        Returns:
            Cleaned response text
        """
        if not response:
            return ""

        # Strip whitespace
        cleaned = response.strip()

        # Remove markdown formatting
        cleaned = cleaned.replace('**', '').replace('*', '')

        # Remove quotes if response is entirely quoted
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = cleaned[1:-1]

        # Limit length
        if len(cleaned) > 400:
            cleaned = cleaned[:397] + "..."

        # Remove multiple spaces
        while '  ' in cleaned:
            cleaned = cleaned.replace('  ', ' ')

        return cleaned.strip()
