"""
InputSanitizer Module for Waddles Module SDK

Provides Pydantic-based input validation and sanitization for module commands.
Includes the CommandInput model for validating command parameters and the InputSanitizer
utility class for sanitizing various input types.

Usage:
    from libs.module_sdk.security import CommandInput, InputSanitizer

    # Validate command input
    try:
        cmd_input = CommandInput(
            command='translate',
            args='Hello world',
            user_id='user123',
            entity_id='stream456',
            community_id=1
        )
    except ValueError as e:
        print(f"Invalid input: {e}")

    # Sanitize arbitrary strings
    safe_text = InputSanitizer.sanitize_string(user_input, max_length=1000)
"""

from pydantic import BaseModel, Field, field_validator, ValidationError
from typing import Optional, List, Tuple, Any, Dict
import re
import json
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ============================================================================
# Command Input Pydantic Model
# ============================================================================

class CommandInput(BaseModel):
    """
    Pydantic model for validating command inputs with security constraints.

    Validates:
    - Command name format (alphanumeric, underscores, hyphens)
    - Arguments for injection attempts
    - User, entity, and community IDs
    - Community ID as positive integer
    """

    command: str = Field(
        ...,
        max_length=100,
        description="Command name to execute"
    )
    args: str = Field(
        default='',
        max_length=1000,
        description="Command arguments"
    )
    user_id: str = Field(
        ...,
        max_length=100,
        description="User ID executing the command"
    )
    entity_id: str = Field(
        ...,
        max_length=100,
        description="Entity ID (stream, channel, etc.) for the command"
    )
    community_id: int = Field(
        ...,
        ge=1,
        description="Community ID (must be >= 1)"
    )

    @field_validator('command', mode='before')
    @classmethod
    def validate_command(cls, v: str) -> str:
        """
        Validate command format.

        Commands must:
        - Be non-empty
        - Match pattern: alphanumeric, underscores, hyphens only
        - Not exceed max_length (100)

        Args:
            v: Command string to validate

        Returns:
            Validated command string

        Raises:
            ValueError: If command format is invalid
        """
        if not isinstance(v, str):
            raise ValueError('command must be a string')

        v = v.strip()

        if not v:
            raise ValueError('command cannot be empty')

        # Allow alphanumeric, underscores, and hyphens
        if not re.match(r'^[a-zA-Z0-9_-]+$', v):
            raise ValueError(
                'command must contain only alphanumeric characters, '
                'underscores, and hyphens'
            )

        return v

    @field_validator('args', mode='before')
    @classmethod
    def validate_args(cls, v: str) -> str:
        r"""
        Validate and sanitize arguments for injection attempts.

        Removes:
        - Angle brackets (< >)
        - Curly braces ({ })
        - Backslashes (\)
        - Other potentially dangerous characters

        Args:
            v: Arguments string to validate

        Returns:
            Sanitized arguments string

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(v, str):
            raise ValueError('args must be a string')

        if not v:
            return ''

        # Check for injection attempt characters
        dangerous_patterns = [
            (r'<|>', 'angle brackets'),
            (r'\{|\}', 'curly braces'),
            (r'\\+', 'backslashes'),
        ]

        for pattern, description in dangerous_patterns:
            if re.search(pattern, v):
                logger.warning(
                    f'SECURITY injection_attempt detected in args '
                    f'pattern={description}'
                )
                raise ValueError(
                    f'args contains invalid characters ({description}): {v[:50]}'
                )

        # Strip whitespace
        v = v.strip()

        return v

    @field_validator('user_id', mode='before')
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """
        Validate user ID format.

        Args:
            v: User ID string to validate

        Returns:
            Validated user ID

        Raises:
            ValueError: If user_id is empty or invalid format
        """
        if not isinstance(v, str):
            raise ValueError('user_id must be a string')

        v = v.strip()

        if not v:
            raise ValueError('user_id cannot be empty')

        return v

    @field_validator('entity_id', mode='before')
    @classmethod
    def validate_entity_id(cls, v: str) -> str:
        """
        Validate entity ID format.

        Args:
            v: Entity ID string to validate

        Returns:
            Validated entity ID

        Raises:
            ValueError: If entity_id is empty or invalid format
        """
        if not isinstance(v, str):
            raise ValueError('entity_id must be a string')

        v = v.strip()

        if not v:
            raise ValueError('entity_id cannot be empty')

        return v

    class Config:
        """Pydantic model configuration."""
        extra = 'forbid'  # Don't allow extra fields
        json_schema_extra = {
            'example': {
                'command': 'translate',
                'args': 'Hello world',
                'user_id': 'user_123',
                'entity_id': 'channel_456',
                'community_id': 1
            }
        }


# ============================================================================
# InputSanitizer Utility Class
# ============================================================================

class InputSanitizer:
    """
    Static utility class for sanitizing and validating various input types.

    Provides methods for:
    - String sanitization with length and HTML controls
    - URL validation with domain whitelist support
    - JSON schema validation with error reporting
    """

    # Dangerous characters that are commonly used in injection attacks
    DANGEROUS_CHARS = r'[<>"\'{};\\]'

    # Common injection patterns
    INJECTION_PATTERNS = [
        r'<script[^>]*>.*?</script>',  # Script tags
        r'javascript:',                 # JavaScript protocol
        r'on\w+\s*=',                   # Event handlers (onclick, onerror, etc.)
        r'<iframe[^>]*>',               # iFrame injection
        r'<img[^>]*on',                 # Image with event handler
    ]

    @staticmethod
    def sanitize_string(
        value: str,
        max_length: int = 1000,
        allow_html: bool = False
    ) -> str:
        """
        Sanitize a string by removing/escaping dangerous characters.

        Args:
            value: String to sanitize
            max_length: Maximum allowed length
            allow_html: Whether to allow HTML tags (if False, they're escaped)

        Returns:
            Sanitized string

        Raises:
            ValueError: If string exceeds max_length or contains injection attempts

        Example:
            safe_text = InputSanitizer.sanitize_string(user_input, max_length=500)
        """
        if not isinstance(value, str):
            raise ValueError('value must be a string')

        # Strip whitespace
        value = value.strip()

        # Check length
        if len(value) > max_length:
            raise ValueError(
                f'input exceeds maximum length of {max_length} characters '
                f'(got {len(value)})'
            )

        # Check for injection patterns
        for pattern in InputSanitizer.INJECTION_PATTERNS:
            if re.search(pattern, value, re.IGNORECASE):
                logger.warning(
                    f'SECURITY potential_injection_detected '
                    f'pattern={pattern}'
                )
                raise ValueError(
                    f'input contains potentially malicious content'
                )

        # If HTML is not allowed, escape dangerous characters
        if not allow_html:
            # Escape HTML entities
            html_escape_table = {
                "&": "&amp;",
                '"': "&quot;",
                "'": "&#x27;",
                ">": "&gt;",
                "<": "&lt;",
            }
            value = ''.join(html_escape_table.get(c, c) for c in value)
        else:
            # Even if HTML is allowed, still check for dangerous tags
            dangerous_tags = ['script', 'iframe', 'object', 'embed', 'form']
            for tag in dangerous_tags:
                tag_pattern = f'<{tag}[^>]*>|</{tag}>'
                if re.search(tag_pattern, value, re.IGNORECASE):
                    logger.warning(
                        f'SECURITY dangerous_tag_detected tag={tag}'
                    )
                    raise ValueError(f'dangerous HTML tag detected: {tag}')

        return value

    @staticmethod
    def validate_url(
        url: str,
        allowed_domains: Optional[List[str]] = None
    ) -> bool:
        """
        Validate a URL for safety and optionally check against allowed domains.

        Args:
            url: URL string to validate
            allowed_domains: List of allowed domain names (optional).
                           If provided, URL must match one of these domains.
                           Example: ['example.com', 'trusted.org']

        Returns:
            True if URL is valid and safe, False otherwise

        Example:
            is_valid = InputSanitizer.validate_url(
                'https://example.com/page',
                allowed_domains=['example.com', 'trusted.org']
            )
        """
        if not isinstance(url, str):
            logger.warning('SECURITY invalid_url_type not_string')
            return False

        url = url.strip()

        if not url:
            logger.warning('SECURITY empty_url')
            return False

        # Check for dangerous protocols
        dangerous_protocols = [
            'javascript:',
            'data:',
            'vbscript:',
            'file:',
        ]

        url_lower = url.lower()
        for protocol in dangerous_protocols:
            if url_lower.startswith(protocol):
                logger.warning(
                    f'SECURITY dangerous_protocol_detected '
                    f'protocol={protocol}'
                )
                return False

        # Try to parse URL
        try:
            parsed = urlparse(url)
        except Exception as e:
            logger.warning(f'SECURITY url_parse_failed error={str(e)}')
            return False

        # Check for valid scheme
        if not parsed.scheme or not parsed.scheme.lower() in ['http', 'https']:
            logger.warning(
                f'SECURITY invalid_url_scheme scheme={parsed.scheme}'
            )
            return False

        # Check for valid netloc (domain)
        if not parsed.netloc:
            logger.warning('SECURITY invalid_url_no_domain')
            return False

        # If allowed domains are specified, validate against them
        if allowed_domains:
            domain = parsed.netloc.lower()
            # Handle subdomains - exact match or parent domain match
            domain_matched = False

            for allowed_domain in allowed_domains:
                allowed_domain = allowed_domain.lower()
                if domain == allowed_domain or domain.endswith('.' + allowed_domain):
                    domain_matched = True
                    break

            if not domain_matched:
                logger.warning(
                    f'SECURITY domain_not_allowed '
                    f'domain={domain} allowed={allowed_domains}'
                )
                return False

        return True

    @staticmethod
    def validate_json(
        data: Any,
        schema: Dict[str, Any]
    ) -> Tuple[bool, List[str]]:
        """
        Validate data against a JSON schema.

        Args:
            data: Data to validate (dict or JSON string)
            schema: Simple validation schema with keys and their validation rules.
                   Schema format:
                   {
                       'field_name': {
                           'type': 'string',  # str, int, bool, list, dict
                           'required': True,
                           'max_length': 100,  # for strings
                           'min_length': 1,    # for strings
                           'pattern': r'^[a-z]+$',  # regex for strings
                           'enum': ['value1', 'value2'],  # allowed values
                       }
                   }

        Returns:
            Tuple of (is_valid: bool, errors: List[str])

        Example:
            schema = {
                'username': {
                    'type': 'string',
                    'required': True,
                    'min_length': 1,
                    'max_length': 100,
                },
                'age': {
                    'type': 'int',
                    'required': False,
                    'min': 0,
                }
            }
            is_valid, errors = InputSanitizer.validate_json(user_data, schema)
            if not is_valid:
                for error in errors:
                    print(error)
        """
        errors = []

        # If data is a JSON string, try to parse it
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                logger.warning(f'SECURITY json_parse_failed error={str(e)}')
                return False, [f'Invalid JSON: {str(e)}']

        # Data must be a dict
        if not isinstance(data, dict):
            return False, ['Data must be a JSON object (dict)']

        # Validate each field according to schema
        for field_name, field_rules in schema.items():
            field_type = field_rules.get('type', 'string')
            is_required = field_rules.get('required', False)

            # Check if field exists
            if field_name not in data:
                if is_required:
                    errors.append(f'Required field missing: {field_name}')
                continue

            field_value = data[field_name]

            # Type validation
            type_map = {
                'string': str,
                'int': int,
                'bool': bool,
                'list': list,
                'dict': dict,
            }

            expected_type = type_map.get(field_type)
            if expected_type and not isinstance(field_value, expected_type):
                errors.append(
                    f'Field {field_name}: expected type {field_type}, '
                    f'got {type(field_value).__name__}'
                )
                continue

            # String-specific validations
            if field_type == 'string' and isinstance(field_value, str):
                max_length = field_rules.get('max_length')
                if max_length and len(field_value) > max_length:
                    errors.append(
                        f'Field {field_name}: exceeds max length {max_length}'
                    )

                min_length = field_rules.get('min_length')
                if min_length and len(field_value) < min_length:
                    errors.append(
                        f'Field {field_name}: below min length {min_length}'
                    )

                pattern = field_rules.get('pattern')
                if pattern and not re.match(pattern, field_value):
                    errors.append(
                        f'Field {field_name}: does not match pattern {pattern}'
                    )

            # Number-specific validations
            if field_type in ('int', 'float') and isinstance(field_value, (int, float)):
                min_val = field_rules.get('min')
                if min_val is not None and field_value < min_val:
                    errors.append(
                        f'Field {field_name}: below minimum value {min_val}'
                    )

                max_val = field_rules.get('max')
                if max_val is not None and field_value > max_val:
                    errors.append(
                        f'Field {field_name}: exceeds maximum value {max_val}'
                    )

            # Enum validation
            enum_values = field_rules.get('enum')
            if enum_values and field_value not in enum_values:
                errors.append(
                    f'Field {field_name}: must be one of {enum_values}'
                )

        return len(errors) == 0, errors


__all__ = [
    'CommandInput',
    'InputSanitizer',
]
