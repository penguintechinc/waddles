"""Prompt-injection structural mitigations shared by every AI provider adapter.

Both `OllamaProvider` and `WaddleAIProvider` build a prompt out of
platform-supplied, ultimately end-user-controlled text (chat messages,
event descriptions carrying attacker-influenceable usernames). The
implementation of the delimiter/neutraliser lives in `flask_core.ai_guard`
so this module, hub-api's `ai_routing` and the AI researcher all share ONE
definition of "untrusted content" (case-, spacing-, full-width- and
zero-width-proof tag neutralisation, chat-template control-token
defanging, optional truncation). `UNTRUSTED_DATA_NOTICE` is appended to
every system prompt; `wrap_untrusted` delimits and labels the actual
untrusted content wherever it is inserted into a message.

`safe_label` covers the other half: short *labels* that are interpolated
into a trusted instruction (platform name, event type) are not content, so
they are not wrapped -- they are validated against a strict identifier
shape and replaced with `unknown` if they do not fit, so a crafted value
can never smuggle instructions into the system turn.

regression: sec-llm01-audit, sec-llm01-hardening
"""

from __future__ import annotations

import re

from flask_core.ai_guard import UNTRUSTED_DATA_NOTICE, wrap_untrusted

_LABEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,39}")


def safe_label(value: object, default: str = "unknown") -> str:
    """Return `value` only if it is a short identifier; otherwise `default`.

    Args:
        value: A platform name / event type headed for a trusted instruction string.
        default: What to use when `value` is not identifier-shaped.

    Returns:
        `value` unchanged when it matches `[A-Za-z][A-Za-z0-9_.-]{0,39}`, else `default`.
    """
    if isinstance(value, str) and _LABEL_RE.fullmatch(value):
        return value
    return default


__all__ = ["UNTRUSTED_DATA_NOTICE", "safe_label", "wrap_untrusted"]
