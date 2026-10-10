"""
Ollama Provider with TLS Support
=================================

Direct Ollama connection with configurable host:port and TLS options.
"""

import httpx
import ssl
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from flask_core.ai_guard import redact_pii, sanitize_model_output
from flask_core.ai_tool_authz import ToolCallDenied, reject_unsolicited_tool_calls

from config import Config
from .prompt_safety import UNTRUSTED_DATA_NOTICE, safe_label, wrap_untrusted

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OllamaProvider:
    """
    Ollama AI provider with TLS support.

    Supports:
    - Configurable host and port
    - TLS/SSL with custom certificates
    - SSL verification control
    - Async HTTP requests with httpx
    """

    host: str = field(default_factory=lambda: Config.OLLAMA_HOST)
    port: str = field(default_factory=lambda: Config.OLLAMA_PORT)
    use_tls: bool = field(default_factory=lambda: Config.OLLAMA_USE_TLS)
    model: str = field(default_factory=lambda: Config.OLLAMA_MODEL)
    temperature: float = field(  # noqa: E501
        default_factory=lambda: Config.OLLAMA_TEMPERATURE
    )
    max_tokens: int = field(default_factory=lambda: Config.OLLAMA_MAX_TOKENS)
    timeout: int = field(default_factory=lambda: Config.OLLAMA_TIMEOUT)
    cert_path: str = field(default_factory=lambda: Config.OLLAMA_CERT_PATH)
    verify_ssl: bool = field(default_factory=lambda: Config.OLLAMA_VERIFY_SSL)
    disable_thinking: bool = field(
        default_factory=lambda: Config.OLLAMA_DISABLE_THINKING
    )
    base_url: str = field(init=False)
    ssl_context: Optional[ssl.SSLContext] = field(init=False, default=None)

    def __post_init__(self):
        """Initialize provider after dataclass creation"""
        # Build base URL
        protocol = 'https' if self.use_tls else 'http'
        self.base_url = f"{protocol}://{self.host}:{self.port}"

        # Setup SSL context for TLS
        self.ssl_context = None
        if self.use_tls:
            self.ssl_context = self._create_ssl_context()

        logger.info(
            f"Initialized Ollama provider: {self.base_url} "
            f"(TLS: {self.use_tls}, Model: {self.model})"
        )

    def _create_ssl_context(self) -> ssl.SSLContext:
        """
        Create SSL context for TLS connections.

        Returns:
            SSL context with appropriate settings
        """
        if self.verify_ssl:
            # Create default context with certificate verification
            context = ssl.create_default_context()

            # Load custom certificate if provided
            if self.cert_path:
                try:
                    context.load_verify_locations(self.cert_path)
                    logger.info(  # noqa: E501
                        f"Loaded custom certificate from {self.cert_path}"
                    )
                except Exception as e:
                    logger.warning(  # noqa: E501
                        f"Failed to load certificate from "
                        f"{self.cert_path}: {e}"
                    )

            return context
        else:
            # Create context without verification (development only)
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            logger.warning(  # noqa: E501
                "SSL verification disabled - not recommended for production!"
            )
            return context

    async def health_check(self) -> bool:
        """
        Check if Ollama is available.

        Returns:
            True if healthy, False otherwise
        """
        try:
            verify_param = (  # noqa: E501
                self.ssl_context if self.use_tls else True
            )
            async with httpx.AsyncClient(verify=verify_param) as client:
                response = await client.get(
                    f"{self.base_url}/api/tags",
                    timeout=5.0
                )
                return response.status_code == 200

        except httpx.ConnectError:
            logger.error(f"Cannot connect to Ollama at {self.base_url}")
            return False
        except httpx.TimeoutException:
            logger.error(f"Ollama health check timed out at {self.base_url}")
            return False
        except Exception as e:
            logger.error(f"Ollama health check failed: {e}")
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
        Generate AI response using Ollama.

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
            # Build a real system/user message pair -- untrusted content
            # (message_content, event descriptions) is delimited and
            # labeled as data, never concatenated into one instruction
            # string. regression: sec-llm01-audit
            messages = self._build_messages(
                message_content, message_type, user_id, platform, context
            )
            if Config.AI_REDACT_PII_SELF_HOSTED:
                # Opt-in egress hygiene for the self-hosted tier (the WaddleAI
                # provider, which leaves our infrastructure, always redacts).
                messages = [
                    {**m, "content": redact_pii(m["content"])} for m in messages
                ]

            # Build request payload (Ollama's /api/chat message API, not
            # /api/generate's single-string prompt)
            payload = {
                "model": self.model,
                "messages": messages,
                # temperature belongs under `options`; a top-level key is
                # silently ignored by Ollama (the model ran at its default).
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_tokens
                },
                "stream": False  # Get complete response
            }
            if self.disable_thinking:
                # Reasoning models (e.g. gemma4) otherwise burn num_predict on
                # hidden thinking and return an empty `content`.
                payload["think"] = False

            # Make async request
            verify_param = (  # noqa: E501
                self.ssl_context if self.use_tls else True
            )
            async with httpx.AsyncClient(verify=verify_param) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=self.timeout
                )

                if response.status_code == 200:
                    data = response.json()
                    # This surface never offers tools: a tool call in the answer
                    # is a hijacked/misbehaving model -- refuse it loudly (the
                    # ToolCallDenied propagates; AIService logs + meters it and
                    # serves its canned reply, never the model's output).
                    reject_unsolicited_tool_calls(
                        data, provider="ollama", surface="chat_reply"
                    )
                    generated_text = data.get('message', {}).get('content', '')

                    # Clean and validate response
                    cleaned_response = self._clean_response(generated_text)

                    if not cleaned_response:
                        # Never hand the caller a blank "success": surface why,
                        # so AIService's canned fallback is a logged decision.
                        logger.error(
                            f"Ollama returned an empty completion "
                            f"(model={self.model}, "
                            f"done_reason={data.get('done_reason')!r}, "
                            f"thinking_present="
                            f"{bool(data.get('message', {}).get('thinking'))})"
                        )
                        return None

                    logger.info(  # noqa: E501
                        f"Ollama generated response: {len(cleaned_response)} "
                        f"chars (tokens: {data.get('eval_count', 0)})"
                    )

                    return cleaned_response

                else:
                    logger.error(  # noqa: E501
                        f"Ollama request failed: {response.status_code} - "
                        f"{response.text}"
                    )
                    return None

        except ToolCallDenied:
            raise
        except httpx.TimeoutException:
            logger.error(f"Ollama request timed out after {self.timeout}s")
            return None
        except Exception as e:
            logger.error(  # noqa: E501
                f"Error generating Ollama response: {e}",
                exc_info=True
            )
            return None

    async def get_available_models(self) -> list:
        """
        Get list of available Ollama models.

        Returns:
            List of model names
        """
        try:
            verify_param = (  # noqa: E501
                self.ssl_context if self.use_tls else True
            )
            async with httpx.AsyncClient(verify=verify_param) as client:
                response = await client.get(
                    f"{self.base_url}/api/tags",
                    timeout=5.0
                )

                if response.status_code == 200:
                    data = response.json()
                    models = [  # noqa: E501
                        model['name']
                        for model in data.get('models', [])
                    ]
                    logger.info(f"Found {len(models)} Ollama models")
                    return models

                return []

        except Exception as e:
            logger.error(f"Failed to get Ollama models: {e}")
            return []

    def _build_messages(
        self,
        message_content: str,
        message_type: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """
        Build the system/user message list for Ollama's /api/chat endpoint.

        Args:
            message_content: Message text
            message_type: Type of message
            user_id: User identifier
            platform: Platform name
            context: Additional context

        Returns:
            Messages list (system role first) for /api/chat -- untrusted
            content (chat text, event usernames) is delimited via
            `prompt_safety.wrap_untrusted`, never concatenated straight
            into the instructions. regression: sec-llm01-audit
        """
        # Python 3.13 pattern matching for message-building
        match message_type:
            case 'chatMessage':
                return self._build_chat_messages(  # noqa: E501
                    message_content, user_id, platform, context
                )
            case _:
                return self._build_event_messages(  # noqa: E501
                    message_type, user_id, platform, context
                )

    def _build_chat_messages(
        self,
        message_content: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """Build messages for chat responses"""

        trigger_type = context.get('trigger_type', 'unknown')

        # Build context-aware system prompt (instructions only -- no
        # untrusted content lives in this message)
        system_parts = [Config.SYSTEM_PROMPT]
        system_parts.append(f"\nPlatform: {safe_label(platform)}")

        # Add trigger-specific context
        match trigger_type:
            case 'greeting':
                system_parts.append(  # noqa: E501
                    "The user is greeting you. Respond warmly and friendly."
                )
            case 'farewell':
                system_parts.append(  # noqa: E501
                    "The user is saying goodbye. "
                    "Respond with a friendly farewell."
                )
            case 'question':
                system_parts.append(  # noqa: E501
                    "The user is asking a question. "
                    "Provide a helpful, informative answer."
                )
            case _:
                system_parts.append(  # noqa: E501
                    "Respond helpfully and naturally to the user's message."
                )

        system_parts.append("\nYour response (keep it under 200 characters):")
        system_parts.append(f"\n{UNTRUSTED_DATA_NOTICE}")

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": '\n'.join(system_parts)}
        ]

        # Add conversation history if available -- each turn is itself
        # untrusted (a prior user/assistant message), delimited the same
        # way as the current message.
        if Config.ENABLE_CHAT_CONTEXT and 'conversation_history' in context:
            history = context['conversation_history']
            if history:
                for msg in history[-3:]:  # Last 3 messages
                    role = msg.get('role', 'unknown')
                    content = msg.get('content', '')
                    normalized_role = (  # noqa: E501
                        'assistant' if role == 'assistant' else 'user'
                    )
                    messages.append({
                        "role": normalized_role,
                        "content": wrap_untrusted(content)
                    })

        # Add current user message -- message_content is the untrusted
        # payload; the "User X said:" framing is our own trusted text.
        messages.append({
            "role": "user",
            "content": (  # noqa: E501
                f"User {user_id} said:\n{wrap_untrusted(message_content)}"
            )
        })

        return messages

    def _build_event_messages(
        self,
        message_type: str,
        user_id: str,
        platform: str,
        context: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        """Build messages for event responses"""

        # user_id (platform username/display name) is attacker-influenceable
        # -- delimit it before it's templated into event_desc.
        safe_user_id = wrap_untrusted(user_id)

        # Event-specific prompts using pattern matching
        match message_type:
            case 'subscription':
                event_desc = f"User {safe_user_id} just subscribed!"
                instruction = "Generate a celebratory thank you message."

            case 'follow':
                event_desc = f"User {safe_user_id} just followed!"
                instruction = "Generate a welcoming thank you message."

            case 'donation':
                event_desc = f"User {safe_user_id} just made a donation!"
                instruction = "Generate a grateful thank you message."

            case 'cheer':
                event_desc = f"User {safe_user_id} just sent bits!"
                instruction = "Generate an excited thank you message."

            case 'raid':
                event_desc = (  # noqa: E501
                    f"User {safe_user_id} just raided the channel!"
                )
                instruction = "Generate a welcoming message for the raiders."

            case 'boost':
                event_desc = (  # noqa: E501
                    f"User {safe_user_id} just boosted the server!"
                )
                instruction = "Generate a thank you message for the boost."

            case 'member_join':
                event_desc = f"User {safe_user_id} just joined!"
                instruction = "Generate a welcoming message."

            case _:
                event_desc = (  # noqa: E501
                    f"User {safe_user_id} triggered a "
                    f"{safe_label(message_type)} event!"
                )
                instruction = "Generate an appropriate response."

        system_content = (
            f"{Config.SYSTEM_PROMPT}\n\n"
            f"Platform: {safe_label(platform)}\n\n"
            f"{instruction}\n"
            "Keep it short, enthusiastic, and under 150 characters:\n\n"
            f"{UNTRUSTED_DATA_NOTICE}"
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": f"Event: {event_desc}"},
        ]

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

        # Neutralise exfiltration channels in the reply itself (remote-image
        # beacons, active HTML, @everyone/@here and role pings, invisible
        # smuggling characters) before it is posted to a chat platform.
        cleaned = sanitize_model_output(response).strip()

        # Remove any leaked system prompts
        if cleaned.lower().startswith("you are"):
            lines = cleaned.split('\n')
            for i, line in enumerate(lines):
                if not line.lower().startswith("you are") and line.strip():
                    cleaned = '\n'.join(lines[i:])
                    break

        # Remove markdown formatting if present
        cleaned = cleaned.replace('**', '').replace('*', '')

        # Limit length for chat readability
        if len(cleaned) > 400:
            cleaned = cleaned[:397] + "..."

        # Remove multiple newlines
        while '\n\n' in cleaned:
            cleaned = cleaned.replace('\n\n', '\n')

        return cleaned.strip()
