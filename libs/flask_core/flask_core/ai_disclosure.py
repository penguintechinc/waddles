"""AI-generated content disclosure (EU AI Act Art.50) for user-facing AI replies.

One shared marker so every path that delivers model output into a chat
channel discloses it the same way. PII-free by construction (a constant).
"""

from __future__ import annotations

#: Appended to every AI-generated chat reply (Art.50(1)/(2) transparency).
AI_DISCLOSURE = "\n\n— \U0001f916 AI-generated"


def with_ai_disclosure(text: str) -> str:
    """Return `text` carrying the AI-generated disclosure marker, exactly once.

    Idempotent so a reply disclosed upstream (hub-api) is not double-marked
    by a downstream handler that also guards the invariant.
    """
    if text.rstrip().endswith(AI_DISCLOSURE.strip()):
        return text
    return text.rstrip() + AI_DISCLOSURE
