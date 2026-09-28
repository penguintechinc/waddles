"""Shared response formatting utilities for platform receiver bots.

Modules return a generic response dict.  Each platform bot must convert that
into platform-native output (Discord embeds, Slack Block Kit, Twitch chat
text).  This module provides helpers for the common cases.
"""
from typing import Any, Dict, List, Optional

# Twitch / Kick / YouTube: plain-text only, 500 char limit per message
CHAT_MAX_LEN = 490


def get_response_content(response: Dict[str, Any]) -> str:
    """Extract the human-readable content string from a router response dict."""
    if not response.get("success", False):
        return response.get("error", "An error occurred.")
    action = response.get("action", response.get("response", {}))
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        return action.get("content", action.get("text", ""))
    return ""


def split_for_chat(text: str, max_len: int = CHAT_MAX_LEN) -> List[str]:
    """Split a long string into chat-safe chunks (≤ max_len characters).

    Tries to split on word boundaries.  Returns at most 5 chunks to avoid
    flooding channels.
    """
    if len(text) <= max_len:
        return [text]
    chunks: List[str] = []
    while text and len(chunks) < 5:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Find last space within max_len
        split_at = text.rfind(" ", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


def is_success(response: Dict[str, Any]) -> bool:
    """Return True if the router response indicates success."""
    return bool(response.get("success", False))


def format_error(response: Dict[str, Any], username: Optional[str] = None) -> str:
    """Format an error response for display in chat."""
    error = response.get("error", "An unknown error occurred.")
    if username:
        return f"@{username} {error}"
    return error
