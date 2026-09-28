"""
Waddles Input Sanitization Library

Provides XSS prevention and input sanitization utilities matching the hub module's
sanitization approach using bleach library.

Usage:
    from flask_core.sanitization import sanitize_html, sanitize_input

    user_comment = sanitize_html(request_data['comment'])
    user_bio = sanitize_input(request_data['bio'], allow_html=True)
"""

from bleach import clean
from typing import Optional, List, Dict
import re
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

# Allowed HTML tags for rich text content
ALLOWED_TAGS = [
    'b', 'i', 'u', 'em', 'strong', 'a',
    'p', 'br', 'span', 'div',
    'ul', 'ol', 'li',
    'code', 'pre', 'blockquote'
]

# Allowed HTML attributes per tag
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'rel'],
    'span': ['class'],
    'div': ['class'],
    'code': ['class'],
}

# Protocols allowed in links
ALLOWED_PROTOCOLS = ['http', 'https', 'mailto']


# ============================================================================
# Core Sanitization Functions
# ============================================================================

def sanitize_html(
    text: str,
    tags: Optional[List[str]] = None,
    attributes: Optional[Dict[str, List[str]]] = None,
    protocols: Optional[List[str]] = None,
    strip: bool = True
) -> str:
    """
    Remove potentially dangerous HTML/JS from user input while preserving safe HTML.

    Args:
        text: Input text to sanitize
        tags: Allowed HTML tags (default: ALLOWED_TAGS)
        attributes: Allowed attributes per tag (default: ALLOWED_ATTRIBUTES)
        protocols: Allowed protocols in links (default: ALLOWED_PROTOCOLS)
        strip: Strip disallowed tags instead of escaping them

    Returns:
        Sanitized text with only allowed HTML

    Usage:
        safe_comment = sanitize_html(user_input)
        # Allows safe HTML like <b>, <i>, <a>, etc.
    """
    if not text:
        return ''

    if tags is None:
        tags = ALLOWED_TAGS
    if attributes is None:
        attributes = ALLOWED_ATTRIBUTES
    if protocols is None:
        protocols = ALLOWED_PROTOCOLS

    try:
        sanitized = clean(
            text,
            tags=tags,
            attributes=attributes,
            protocols=protocols,
            strip=strip
        )

        logger.debug(
            f"SYSTEM sanitize_html input_length={len(text)} "
            f"output_length={len(sanitized)} stripped={strip}"
        )

        return sanitized.strip()

    except Exception as e:
        logger.error(f"ERROR sanitize_html_failed error={str(e)}")
        # Return empty string on error to be safe
        return ''


def sanitize_input(text: str, allow_html: bool = False) -> str:
    """
    Sanitize general text input. Strips all HTML unless allow_html=True.

    Args:
        text: Input text to sanitize
        allow_html: If True, allows safe HTML tags. If False, strips all HTML.

    Returns:
        Sanitized text

    Usage:
        # Strip all HTML
        plain_text = sanitize_input(user_input)

        # Allow safe HTML
        formatted_text = sanitize_input(user_input, allow_html=True)
    """
    if not text:
        return ''

    if allow_html:
        return sanitize_html(text)
    else:
        # Strip all HTML tags
        try:
            sanitized = clean(text, tags=[], strip=True)
            return sanitized.strip()
        except Exception as e:
            logger.error(f"ERROR sanitize_input_failed error={str(e)}")
            return ''


def sanitize_sql_like(text: str) -> str:
    """
    Sanitize text for use in SQL LIKE queries by escaping special characters.

    Args:
        text: Input text to sanitize for LIKE query

    Returns:
        Sanitized text with escaped SQL LIKE wildcards

    Usage:
        search_term = sanitize_sql_like(user_search)
        query = f"SELECT * FROM users WHERE name LIKE '%{search_term}%'"
    """
    if not text:
        return ''

    # Escape SQL LIKE special characters
    # % and _ are wildcards in LIKE queries
    sanitized = text.replace('\\', '\\\\')  # Escape backslash first
    sanitized = sanitized.replace('%', '\\%')
    sanitized = sanitized.replace('_', '\\_')

    return sanitized


def strip_whitespace(text: str) -> str:
    """
    Strip leading/trailing whitespace and normalize internal whitespace.

    Args:
        text: Input text to normalize

    Returns:
        Text with normalized whitespace

    Usage:
        normalized = strip_whitespace("  hello   world  ")
        # Returns: "hello world"
    """
    if not text:
        return ''

    # Replace multiple spaces/tabs/newlines with single space
    normalized = re.sub(r'\s+', ' ', text)
    return normalized.strip()


def sanitize_filename(filename: str, max_length: int = 255) -> str:
    """
    Sanitize filename to prevent directory traversal and invalid characters.

    Args:
        filename: Input filename to sanitize
        max_length: Maximum filename length

    Returns:
        Safe filename

    Usage:
        safe_name = sanitize_filename("../../etc/passwd")
        # Returns: "passwd"
    """
    if not filename:
        return 'unnamed'

    # Remove directory paths (prevent path traversal)
    filename = filename.replace('\\', '/').split('/')[-1]

    # Remove null bytes and control characters
    filename = ''.join(char for char in filename if ord(char) >= 32)

    # Replace invalid filename characters
    filename = re.sub(r'[<>:"|?*]', '_', filename)

    # Remove leading/trailing dots and spaces
    filename = filename.strip('. ')

    # Truncate to max length
    if len(filename) > max_length:
        # Try to preserve extension
        parts = filename.rsplit('.', 1)
        if len(parts) == 2:
            name, ext = parts
            max_name_length = max_length - len(ext) - 1
            filename = name[:max_name_length] + '.' + ext
        else:
            filename = filename[:max_length]

    # Fallback if empty after sanitization
    if not filename:
        return 'unnamed'

    return filename


def sanitize_url(url: str, allowed_schemes: Optional[List[str]] = None) -> Optional[str]:
    """
    Sanitize URL to prevent XSS via javascript: protocol and validate format.

    Args:
        url: Input URL to sanitize
        allowed_schemes: Allowed URL schemes (default: ['http', 'https'])

    Returns:
        Sanitized URL or None if invalid

    Usage:
        safe_url = sanitize_url(user_url)
        if safe_url:
            # URL is safe to use
    """
    if not url:
        return None

    if allowed_schemes is None:
        allowed_schemes = ['http', 'https']

    url = url.strip()

    # Check for dangerous protocols
    dangerous_protocols = ['javascript:', 'data:', 'vbscript:', 'file:']
    url_lower = url.lower()

    for protocol in dangerous_protocols:
        if url_lower.startswith(protocol):
            logger.warning(f"AUTHZ dangerous_url_blocked protocol={protocol}")
            return None

    # Validate allowed schemes
    has_valid_scheme = False
    for scheme in allowed_schemes:
        if url_lower.startswith(f'{scheme}://'):
            has_valid_scheme = True
            break

    if not has_valid_scheme:
        logger.warning(f"AUTHZ invalid_url_scheme url={url[:50]}")
        return None

    return url


def sanitize_json_string(text: str) -> str:
    """
    Sanitize text for safe inclusion in JSON strings (escape special characters).

    Args:
        text: Input text to sanitize

    Returns:
        Text with JSON special characters escaped

    Usage:
        safe_json_value = sanitize_json_string(user_input)
    """
    if not text:
        return ''

    # Escape JSON special characters
    replacements = {
        '"': '\\"',
        '\\': '\\\\',
        '\n': '\\n',
        '\r': '\\r',
        '\t': '\\t',
        '\b': '\\b',
        '\f': '\\f'
    }

    sanitized = text
    for char, escaped in replacements.items():
        sanitized = sanitized.replace(char, escaped)

    return sanitized


def truncate_text(
    text: str,
    max_length: int,
    suffix: str = '...'
) -> str:
    """
    Truncate text to maximum length with optional suffix.

    Args:
        text: Input text to truncate
        max_length: Maximum length (including suffix)
        suffix: Text to append if truncated

    Returns:
        Truncated text

    Usage:
        short_text = truncate_text(long_text, 100)
    """
    if not text or len(text) <= max_length:
        return text

    truncate_at = max_length - len(suffix)
    if truncate_at < 0:
        truncate_at = max_length

    return text[:truncate_at] + suffix


# ============================================================================
# Pydantic Validator Helpers
# ============================================================================

def sanitized_html_validator(v: str, allow_html: bool = True) -> str:
    """
    Pydantic validator for HTML content.

    Usage in Pydantic model:
        class UserComment(BaseModel):
            comment: str

            @validator('comment')
            def sanitize_comment(cls, v):
                from flask_core.sanitization import sanitized_html_validator
                return sanitized_html_validator(v, allow_html=True)
    """
    return sanitize_input(v, allow_html=allow_html)


def sanitized_filename_validator(v: str, max_length: int = 255) -> str:
    """
    Pydantic validator for filenames.

    Usage in Pydantic model:
        class FileUpload(BaseModel):
            filename: str

            @validator('filename')
            def sanitize_name(cls, v):
                from flask_core.sanitization import sanitized_filename_validator
                return sanitized_filename_validator(v)
    """
    return sanitize_filename(v, max_length=max_length)


def sanitized_url_validator(v: str, allowed_schemes: Optional[List[str]] = None) -> str:
    """
    Pydantic validator for URLs.

    Usage in Pydantic model:
        class Bookmark(BaseModel):
            url: str

            @validator('url')
            def sanitize_url_field(cls, v):
                from flask_core.sanitization import sanitized_url_validator
                return sanitized_url_validator(v)
    """
    sanitized = sanitize_url(v, allowed_schemes=allowed_schemes)
    if sanitized is None:
        raise ValueError('invalid or dangerous URL')
    return sanitized


__all__ = [
    # Core sanitization
    'sanitize_html',
    'sanitize_input',
    'sanitize_sql_like',
    'strip_whitespace',
    'sanitize_filename',
    'sanitize_url',
    'sanitize_json_string',
    'truncate_text',
    # Pydantic validators
    'sanitized_html_validator',
    'sanitized_filename_validator',
    'sanitized_url_validator',
    # Configuration constants
    'ALLOWED_TAGS',
    'ALLOWED_ATTRIBUTES',
    'ALLOWED_PROTOCOLS',
]
