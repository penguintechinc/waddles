"""`!eli5 <thing>` -> a simple 'explain like I'm 5' explanation of the thing, relayed to the caller's own origin platform.

AI-backed (`ai.generate` permission, `stage-next` world): `transform()` makes exactly ONE
`waddle_sdk.ai.generate` call per invocation (PROCESS stage only; `dispatch` just relays the
finished reply). The host picks the model (`json=False` -> the default text model), frames the
prompt as untrusted data, and REJECTS a prompt carrying PII (`DeniedError.code == "contains_pii"`)
-- that one refusal becomes a friendly "remove personal info" reply; every other AI error is
re-raised (fail loud, never a canned/empty completion). The completion is untrusted text and is
only length-capped here before it is relayed.

Activation-gated only -- no feature flag: the platform's installed -> enabled -> activated check
decides whether this bundle runs (same as `eightball`).

PII-free logging: only the command and input/output LENGTHS are logged -- never the thing, the
actor, or the AI output.

Grammar: `waddle_sdk.command.parse_command` validates the bare `!eli5` form; a free-text argument
is not a shared-grammar verb, so the text after the command token is taken as the thing.
"""

from __future__ import annotations

import re
from typing import Any

from waddle_sdk import ai, log, relay
from waddle_sdk.command import CommandSpec, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SPEC = CommandSpec(name="eli5")

#: Matches `!eli5` alone or `!eli5 <anything>` (case-insensitive); `!eli5` + letters does not match.
COMMAND_PATTERN = re.compile(r"^!eli5(?:\s+.*)?$", re.IGNORECASE | re.DOTALL)

#: Input cap (characters) -- bounds the prompt and abuse; over it -> a friendly "too long" reply.
MAX_INPUT_CHARS = 200

#: Completion budget handed to the host (host max is 512) and the relayed-reply cap (chat limits).
MAX_TOKENS = 160
MAX_REPLY_CHARS = 400
TEMPERATURE = 0.7

USAGE = "Usage: !eli5 <thing>"
TOO_LONG = "That's a bit long -- keep it under 200 characters."
PII_REPLY = "I can't use that -- please remove personal info (emails, handles, numbers) and try again."

PROMPT_TEMPLATE = (
    "Explain the following like I'm 5 years old, in two or three short, simple, friendly "
    "sentences. Keep it family-friendly (SFW). If it is unsafe or inappropriate to explain, "
    "politely refuse instead.\n"
    "Thing: {text}"
)


def build_prompt(text: str) -> str:
    """Build the single AI prompt from the (whitespace-collapsed, length-capped) thing."""
    return PROMPT_TEMPLATE.format(text=text)


def _extract_input(text: str) -> str:
    """Return the whitespace-collapsed free text after the command token ('' when bare)."""
    parse_command(f"!{SPEC.name}", SPEC)  # validates the declared command name/grammar
    _head, _, rest = text.strip().partition(" ")
    return " ".join(rest.split())


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!eli5 <thing>` -> one AI call -> a reply event.

    Returns `None` for any non-matching payload. Raises the `waddle_sdk.ai` error for every AI
    failure except a PII rejection (fail loud).
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not COMMAND_PATTERN.match(text.strip()):
        return None

    user_input = _extract_input(text)
    if not user_input:
        reply = USAGE
        log.info("eli5.transform usage", platform=event.platform)
    elif len(user_input) > MAX_INPUT_CHARS:
        reply = TOO_LONG
        log.info(
            "eli5.transform too_long",
            platform=event.platform,
            input_len=len(user_input),
        )
    else:
        try:
            completion = await ai.generate(
                build_prompt(user_input),
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                json=False,
            )
        except ai.DeniedError as exc:
            if exc.code != "contains_pii":
                log.error("eli5.transform ai_denied", code=exc.code)
                raise
            reply = PII_REPLY
            log.info("eli5.transform pii_rejected", platform=event.platform)
        except ai.AiError as exc:
            log.error("eli5.transform ai_error", error_type=type(exc).__name__)
            raise
        else:
            reply = " ".join(completion.split())[:MAX_REPLY_CHARS]
            log.info(
                "eli5.transform answered",
                platform=event.platform,
                input_len=len(user_input),
                output_len=len(reply),
            )

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply, "channel_id": event.payload.get("channel_id")},
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


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host import
    only. Raises `ValueError` when the envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "eli5 reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("eli5.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
