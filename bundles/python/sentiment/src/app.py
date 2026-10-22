"""`!sentiment <text>` -> the mood of the text as a friendly one-line reply (AI, stateless).

The model is called through `waddle_sdk.ai.generate` (the DANGEROUS `ai.generate` permission,
`stage-next` world, **process stage only** -- so the call happens in `transform()` and
`dispatch()` is a pure relay). Structured output (`json=True`), so the HOST picks its structured model
(`gemma4:e4b`); a bundle can't name or override it. The model returns
`{sentiment, confidence, emoji}`, parsed DEFENSIVELY (untrusted text): malformed JSON, an unknown
label or a bad confidence fails loud with an error reply, never a guessed result. One AI call per invocation.

Activation-gated only -- NO feature flag (model: `eightball`); the platform's installed ->
enabled -> activated check decides whether this bundle runs.

Fail loud: every `waddle_sdk.ai` refusal (`contains_pii`, `rate_limited`, `not_granted`,
unavailable model, ...) becomes a clear chat reply AND a raise in `dispatch()` -- never a canned
or empty/guessed analysis. Missing text is a usage reply (`CommandUsageError`).

PII-free logs: only platform, op/command, the sentiment LABEL, and input LENGTH are logged -- never the typed
text, the model output, `event.actor`, or an exception message. The host additionally REJECTS prompts
carrying emails/phones/@handles/tokens (`contains_pii`), surfaced as a "remove personal info"
reply.

Output safety: `label` is checked against the closed `LABELS` set and the confidence is a
number, but the model-supplied `emoji` only has to be short, printable and not-all-ASCII
(`@here🙂` passes), so it goes through `waddle_sdk.safe_text.sanitize_user_text` LAST.
"""

from __future__ import annotations

import json
import re
from typing import Any, NoReturn

from waddle_sdk import ai, log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="sentiment")

#: Chat-sized bounds: the input fits under the host's ~2000-token prompt bound, the reply under
#: Twitch's 500-char message limit.
MAX_TEXT_LEN = 400
MAX_COMPLETION_TOKENS = 128
#: The closed label set the prompt asks for and the parser enforces.
LABELS: dict[str, str] = {
    "positive": "\U0001f604",
    "negative": "\U0001f61e",
    "neutral": "\U0001f610",
    "mixed": "\U0001f914",
}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
#: Low temperature -- classification should be stable, not creative.
TEMPERATURE = 0.1

_USAGE = "Usage: !sentiment <text>  (e.g. !sentiment I love this stream!)"

_ERROR_MESSAGES: dict[str, str] = {
    "contains_pii": (
        "I can't analyze that -- it looks like it contains personal info "
        "(an email, phone number, @mention or similar). Remove it and try again."
    ),
    "rate_limited": "Sentiment is busy right now, please try again in a minute.",
    "too_long": f"That text is too long to analyze (max {MAX_TEXT_LEN} characters).",
    "unavailable": "Sentiment is unavailable right now (the AI model is unreachable), try later.",
    "not_granted": "Sentiment is not enabled for this community (AI capability not granted).",
    "filtered": "The model couldn't produce an analysis for that text.",
    "malformed": "The AI returned an unreadable analysis, please try again.",
}
_GENERIC_ERROR = "Sentiment failed due to an internal error, please try again later."


def build_prompt(text: str) -> str:
    """Build the single sentiment prompt, asking for one small JSON object."""
    return (
        "You are a sentiment analyst. Classify the mood of the text between the <text> tags. "
        'Reply with ONLY a JSON object: {"sentiment": one of "positive", "negative", '
        '"neutral", "mixed", "confidence": a number from 0 to 1, "emoji": one emoji}. '
        "No other text.\n"
        f"<text>{text}</text>"
    )


class MalformedAnalysisError(ValueError):
    """The model's JSON was unparseable or off-schema (never carries the model's text)."""


def parse_analysis(raw: str) -> tuple[str, float, str]:
    """Return `(label, confidence, emoji)` from the untrusted completion, or raise.

    Tolerates a ```json fence; everything else must match the schema exactly. The emoji is used
    only if it is short and entirely non-ASCII, else the label's own emoji is substituted.

    Raises:
        MalformedAnalysisError: Not JSON, not an object, unknown label, or bad confidence/emoji.
    """
    try:
        data = json.loads(_FENCE_RE.sub("", raw.strip()))
    except (ValueError, RecursionError) as exc:
        raise MalformedAnalysisError("completion is not valid JSON") from exc
    if not isinstance(data, dict):
        raise MalformedAnalysisError("completion JSON is not an object")
    label = data.get("sentiment")
    if not isinstance(label, str) or label.strip().lower() not in LABELS:
        raise MalformedAnalysisError("unknown or missing sentiment label")
    label = label.strip().lower()
    conf = data.get("confidence")
    if (
        isinstance(conf, bool)
        or not isinstance(conf, int | float)
        or not 0.0 <= float(conf) <= 1.0
    ):
        raise MalformedAnalysisError("confidence must be a number in 0..1")
    emoji = data.get("emoji")
    if not isinstance(emoji, str):
        raise MalformedAnalysisError("emoji must be a string")
    emoji = emoji.strip()
    if not emoji or len(emoji) > 8 or not emoji.isprintable() or emoji.isascii():
        emoji = LABELS[label]
    return label, float(conf), emoji


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
    """Return the text to sentiment from the text after `!sentiment`.

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
    """Implement `process-stage.transform`: match `!sentiment`, call the AI host, build the reply.

    Returns `None` for a non-matching payload. Every other outcome (usage, success, AI refusal)
    is forwarded with a `command` of `usage`/`sentiment`/`error` -- never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None

    channel_id = event.payload.get("channel_id")
    command = "sentiment"
    reply: str
    try:
        body = _parse(rest.strip())
    except CommandUsageError as exc:
        command, reply = "usage", str(exc)
        log.info("sentiment.usage", platform=event.platform)
    else:
        try:
            raw = await ai.generate(
                build_prompt(body),
                max_tokens=MAX_COMPLETION_TOKENS,
                temperature=TEMPERATURE,
                json=True,
            )
        except ai.AiError as exc:
            key = _error_key(exc)
            command = "error"
            reply = _ERROR_MESSAGES.get(key, _GENERIC_ERROR)
            # Type + stable key only -- never the exception message, prompt or text.
            log.error(
                "sentiment.ai_error",
                platform=event.platform,
                error=type(exc).__name__,
                reason=key,
                input_len=len(body),
            )
        else:
            try:
                label, conf, emoji = parse_analysis(raw)
            except MalformedAnalysisError:
                command, reply = "error", _ERROR_MESSAGES["malformed"]
                log.error(
                    "sentiment.malformed_json",
                    platform=event.platform,
                    reason="malformed",
                    input_len=len(body),
                    output_len=len(raw),
                )
            else:
                reply = (
                    f"{sanitize_user_text(emoji)} Sentiment: {label} "
                    f"({round(conf * 100)}% confident)"
                )
                log.info(
                    "sentiment.transform matched",
                    platform=event.platform,
                    sentiment=label,
                    input_len=len(body),
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
    raise RuntimeError(f"sentiment failed: {detail}")


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
            "sentiment reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    command = payload.get("command", "sentiment")
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("sentiment.dispatch relayed", platform=provider, command=command)
    if command == "error":
        _fail("ai host refused or failed (error reply relayed)")
    return DispatchResult(transport=provider, detail=str(command))
