"""`!rephrase <text>` -> the text reworded more clearly and politely (AI, stateless).

The model is called through `waddle_sdk.ai.generate` (the DANGEROUS `ai.generate` permission,
`stage-next` world, **process stage only** -- so the call happens in `transform()` and
`dispatch()` is a pure relay). Plain text (`json=False`), so the HOST picks its plain-text model
(`gemma4:e2b`); a bundle can't name or override it. One AI call per invocation.

Activation-gated only -- NO feature flag (model: `eightball`); the platform's installed ->
enabled -> activated check decides whether this bundle runs.

Fail loud: every `waddle_sdk.ai` refusal (`contains_pii`, `rate_limited`, `not_granted`,
unavailable model, ...) becomes a clear chat reply AND a raise in `dispatch()` -- never a canned
or empty rewrite. Missing text is a usage reply (`CommandUsageError`).

PII-free logs: only platform, op/command, and input/output LENGTHS are logged -- never the typed
text, the rewrite, `event.actor`, or an exception message. The host additionally REJECTS prompts
carrying emails/phones/@handles/tokens (`contains_pii`), surfaced as a "remove personal info"
reply.

Output safety: the model completion is untrusted text (a prompt-injected input can make it
emit `{user:<token>}` placeholders or `@everyone`), so it goes through
`waddle_sdk.safe_text.sanitize_user_text` LAST, immediately before it enters the reply. No
`allow_tokens`: nothing in model output is ever a host-minted mention. The reply always starts with
the fixed `✍️ ` prefix, so a completion like `/ban x` can never lead it (a chat-command lead
guard is unnecessary here; pinned by `test_model_completion_can_never_lead_the_reply`).
"""

from __future__ import annotations

from typing import Any, NoReturn

from waddle_sdk import ai, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="rephrase")

#: Chat-sized bounds: the input fits under the host's ~2000-token prompt bound, the reply under
#: Twitch's 500-char message limit.
MAX_TEXT_LEN = 400
MAX_REPLY_LEN = 450
MAX_COMPLETION_TOKENS = 256
#: Some variety is fine for a rewrite, but stay close to the original meaning.
TEMPERATURE = 0.5

_USAGE = "Usage: !rephrase <text>  (e.g. !rephrase give me that now)"

_ERROR_MESSAGES: dict[str, str] = {
    "contains_pii": (
        "I can't rephrase that -- it looks like it contains personal info "
        "(an email, phone number, @mention or similar). Remove it and try again."
    ),
    "rate_limited": "Rephrase is busy right now, please try again in a minute.",
    "too_long": f"That text is too long to rephrase (max {MAX_TEXT_LEN} characters).",
    "unavailable": "Rephrase is unavailable right now (the AI model is unreachable), try later.",
    "not_granted": "Rephrase is not enabled for this community (AI capability not granted).",
    "filtered": "The model couldn't produce a rephrasing for that text.",
}
_GENERIC_ERROR = "Rephrase failed due to an internal error, please try again later."


def build_prompt(text: str) -> str:
    """Build the single rephrase prompt (SFW, meaning-preserving, text only)."""
    return (
        "You are a helpful writing assistant. Reword the text between the <text> tags so it "
        "is clearer and more polite, keeping its meaning and language. Keep it family-friendly "
        "and no longer than the original plus a few words. "
        "Return ONLY the reworded text, nothing else -- no quotes, notes or explanations.\n"
        f"<text>{text}</text>"
    )


def _clean_reply(raw: str) -> str:
    """Collapse the completion to one bounded chat line (it is untrusted model text)."""
    one_line = " ".join(raw.split())
    if len(one_line) > MAX_REPLY_LEN:
        one_line = one_line[: MAX_REPLY_LEN - 1].rstrip() + "…"
    return one_line


def _error_key(exc: ai.AiError) -> str:
    """Map a `waddle_sdk.ai` exception onto a stable, PII-free error key."""
    if isinstance(exc, ai.DeniedError):
        return exc.code if exc.code in _ERROR_MESSAGES else "generic"
    if isinstance(exc, ai.TooManyTokensError):
        return "too_long"
    if isinstance(exc, ai.UnavailableError):
        return "unavailable"
    if isinstance(exc, ai.ContentFilteredError):
        return "filtered"
    return "generic"


def _parse(rest: str) -> str:
    """Return the text to rephrase from the text after `!rephrase`.

    Raises:
        CommandUsageError: Missing text, a bare verb shape the shared parser rejects, or text
            over `MAX_TEXT_LEN`.
    """
    if not rest:
        parse_command(f"!{SPEC.name}", SPEC)
        raise CommandUsageError(_USAGE)
    if len(rest) > MAX_TEXT_LEN:
        raise CommandUsageError(_ERROR_MESSAGES["too_long"])
    return rest


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!rephrase`, call the AI host, build the reply.

    Returns `None` for a non-matching payload. Every other outcome (usage, success, AI refusal)
    is forwarded with a `command` of `usage`/`rephrase`/`error` -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None

    channel_id = event.payload.get("channel_id")
    command = "rephrase"
    reply: str
    try:
        body = _parse(rest.strip())
    except CommandUsageError as exc:
        command, reply = "usage", str(exc)
        log.info("rephrase.usage", platform=event.platform)
    else:
        try:
            raw = await ai.generate(
                build_prompt(body),
                max_tokens=MAX_COMPLETION_TOKENS,
                temperature=TEMPERATURE,
                json=False,
            )
        except ai.AiError as exc:
            key = _error_key(exc)
            command = "error"
            reply = _ERROR_MESSAGES.get(key, _GENERIC_ERROR)
            # Type + stable key only -- never the exception message, prompt or text.
            log.error(
                "rephrase.ai_error",
                platform=event.platform,
                error=type(exc).__name__,
                reason=key,
                input_len=len(body),
            )
        else:
            cleaned = _clean_reply(raw)
            if not cleaned:
                command, reply = "error", _ERROR_MESSAGES["filtered"]
                log.error("rephrase.empty_completion", platform=event.platform)
            else:
                reply = f"✍️ {sanitize_user_text(cleaned)}"
                log.info(
                    "rephrase.transform matched",
                    platform=event.platform,
                    input_len=len(body),
                    output_len=len(cleaned),
                )

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply, "channel_id": channel_id, "command": command},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _fail(detail: str) -> NoReturn:
    """Raise after the user-facing error reply was relayed, so the failure is never silent."""
    raise RuntimeError(f"rephrase failed: {detail}")


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    Raises:
        ValueError: The envelope's payload has no `channel_id`.
        RuntimeError: `transform()` recorded an AI failure (the error reply is relayed first).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "rephrase reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    command = payload.get("command", "rephrase")
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("rephrase.dispatch relayed", platform=provider, command=command)
    if command == "error":
        _fail("ai host refused or failed (error reply relayed)")
    return DispatchResult(transport=provider, detail=str(command))
