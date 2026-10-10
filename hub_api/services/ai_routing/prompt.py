"""Compose the (system, user) pair a provider is sent -- structure, not concatenation.

`AIRequest.prompt` is the invoking user's own instruction and is sent as-is. Everything the
SERVER retrieved on their behalf (`AIRequest.untrusted_context`) is rendered by
`flask_core.ai_guard.render_retrieved_data` as a labelled, delimited, defanged
`<retrieved_data>` block appended to the user turn, with injection-flagged items dropped, and
the standing `UNTRUSTED_DATA_NOTICE` is appended to the system turn so the model is told what
that block is. A request with neither a system prompt nor retrieved context composes to
`(None, prompt)` -- byte-for-byte the payload shape that existed before this hardening.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask_core.ai_guard import render_retrieved_data, with_untrusted_notice

from services.ai_routing.models import AIRequest


@dataclass(slots=True, frozen=True)
class ComposedPrompt:
    """The provider-ready prompt: optional system turn, user turn, and what the guard did."""

    system: str | None
    user: str
    #: True when untrusted retrieved content shaped this prompt (side-effecting tools refused).
    tainted: bool = False
    #: Retrieved items omitted because they tripped the injection scan (counts only).
    dropped_items: int = 0


def compose_prompt(request: AIRequest) -> ComposedPrompt:
    """Build the system/user turns for `request`, defanging any retrieved context.

    Args:
        request: The normalized completion request.

    Returns:
        The composed prompt. `tainted` is True whenever `request.untrusted_context` is non-empty,
        whether or not any item survived the injection scan.
    """
    if not request.untrusted_context:
        return ComposedPrompt(system=request.system_prompt, user=request.prompt)
    rendered = render_retrieved_data(request.untrusted_context, source="caller_context")
    return ComposedPrompt(
        system=with_untrusted_notice(request.system_prompt),
        user=f"{request.prompt}\n\n{rendered.text}",
        tainted=True,
        dropped_items=rendered.dropped,
    )
