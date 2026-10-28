"""`!translate <target-lang> <text>` -> an AI translation (Waddles v2 port, stateless).

Ported forward from the v2 `translate_interaction_module`'s WaddleAI provider: same instruction
("professional translator ... return ONLY the translation") and same auto-detect-the-source
behaviour (the v2 two-call detect-then-translate flow is folded into one prompt -- the host
rate limits `ai.generate` to 10 calls/60 s, so one call per command).

The model is called through `waddle_sdk.ai.generate` (the `ai.generate` permission, `stage-next`
world, **process stage only** -- so the call happens in `transform()`, and `dispatch()` is a pure
relay). The model is HOST-controlled (`gemma4:e2b` for `json=False`, as used here); a bundle can't
name or override it. Activation-gated only -- no feature flag: the platform's installed -> enabled
-> activated check decides whether this bundle runs.

Fail loud: every `waddle_sdk.ai` refusal (`contains_pii`, `rate_limited`, `not_granted`,
unavailable model, ...) becomes a clear chat reply AND a raise in `dispatch()` -- never a canned or
empty translation. Missing/invalid language or text is a usage reply (`CommandUsageError`).

PII-free logs: only platform, op, target language, and input/output LENGTHS are logged -- never
the typed text, the translation, `event.actor`, or an exception message. The host additionally
REJECTS prompts carrying emails/phones/@handles/tokens (`contains_pii`); that is surfaced to the
caller as a "remove personal info" reply.

Output safety: the model completion and the typed language token echoed in the usage reply are
untrusted, so both go through `waddle_sdk.safe_text.sanitize_user_text` LAST (no `allow_tokens`:
model output is never a host-minted mention; `lang` is `_LANG_RE`-validated and emitted as-is).
The reply always starts with the fixed `[<lang>] ` prefix, so a completion like `/ban x` can never
lead it (a chat-command lead guard is unnecessary here; pinned by
`test_model_completion_can_never_lead_the_reply`).
"""

from __future__ import annotations

import re
from typing import Any, NoReturn

from waddle_sdk import ai, log, relay
from waddle_sdk.command import VERBS, CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope
from waddle_sdk.safe_text import sanitize_user_text

SPEC = CommandSpec(name="translate")

#: Chat-sized bounds: the input fits comfortably under the host's ~2000-token prompt bound, and
#: the reply under Twitch's 500-char message limit once prefixed.
MAX_TEXT_LEN = 400
MAX_REPLY_LEN = 450
MAX_COMPLETION_TOKENS = 256
#: Low temperature -- translation should be faithful, not creative.
TEMPERATURE = 0.2

_LANG_RE = re.compile(r"^[A-Za-z][A-Za-z_-]{1,31}$")
_USAGE = (
    "Usage: !translate <target-language> <text>  (e.g. !translate spanish good morning)"
)

_ERROR_MESSAGES: dict[str, str] = {
    "contains_pii": (
        "I can't translate that -- it looks like it contains personal info "
        "(an email, phone number, @mention or similar). Remove it and try again."
    ),
    "rate_limited": "Translation is busy right now, please try again in a minute.",
    "too_long": f"That text is too long to translate (max {MAX_TEXT_LEN} characters).",
    "unavailable": "Translation is unavailable right now (the AI model is unreachable), try later.",
    "not_granted": "Translation is not enabled for this community (AI capability not granted).",
    "filtered": "The model couldn't produce a translation for that text.",
}
_GENERIC_ERROR = "Translation failed due to an internal error, please try again later."


def build_prompt(target_lang: str, text: str) -> str:
    """Build the single translate prompt (v2 wording; source language auto-detected)."""
    return (
        "You are a professional translator. Detect the language of the text between the "
        f"<text> tags and translate it into {target_lang}. "
        "Return ONLY the translation, nothing else -- no quotes, notes or explanations.\n"
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


def _parse(rest: str) -> tuple[str, str]:
    """Return `(target_lang, text)` from the text after `!translate`.

    Grammar extension (same as `compliment`'s positional target): the first token is a language,
    which has no slot in the shared `parse_command` grammar, so only the bare / verb shapes are
    delegated to it.

    Raises:
        CommandUsageError: Missing/invalid language, missing text, or text over `MAX_TEXT_LEN`.
    """
    if not rest:
        parse_command(f"!{SPEC.name}", SPEC)
        raise CommandUsageError(_USAGE)
    tok1, _, tail = rest.partition(" ")
    if tok1.lower() in VERBS:
        parse_command(f"!{SPEC.name} {rest}", SPEC)  # may raise its own usage error
        raise CommandUsageError(_USAGE)
    if not _LANG_RE.match(tok1):
        raise CommandUsageError(
            f"'{sanitize_user_text(tok1[:32])}' is not a valid language. {_USAGE}"
        )
    text = tail.strip()
    if not text:
        raise CommandUsageError(f"Nothing to translate. {_USAGE}")
    if len(text) > MAX_TEXT_LEN:
        raise CommandUsageError(_ERROR_MESSAGES["too_long"])
    return tok1.lower(), text


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: match `!translate`, call the AI host, build the reply.

    Returns `None` for a non-matching payload. Every other outcome
    (usage, success, AI refusal) is forwarded with a `command` of `usage`/`translate`/`error` --
    never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    head, _, rest = text.strip().partition(" ")
    if head.lower() != f"!{SPEC.name}":
        return None

    channel_id = event.payload.get("channel_id")
    command = "translate"
    reply: str
    try:
        lang, body = _parse(rest.strip())
    except CommandUsageError as exc:
        command, reply = "usage", str(exc)
        log.info("translate.usage", platform=event.platform)
    else:
        try:
            raw = await ai.generate(
                build_prompt(lang, body),
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
                "translate.ai_error",
                platform=event.platform,
                error=type(exc).__name__,
                reason=key,
                lang=lang,
                input_len=len(body),
            )
        else:
            cleaned = _clean_reply(raw)
            if not cleaned:
                command, reply = "error", _ERROR_MESSAGES["filtered"]
                log.error(
                    "translate.empty_completion",
                    platform=event.platform,
                    lang=lang,
                )
            else:
                reply = f"[{lang}] {sanitize_user_text(cleaned)}"
                log.info(
                    "translate.transform matched",
                    platform=event.platform,
                    lang=lang,
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
    raise RuntimeError(f"translate failed: {detail}")


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
            "translate reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    command = payload.get("command", "translate")
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("translate.dispatch relayed", platform=provider, command=command)
    if command == "error":
        _fail("ai host refused or failed (error reply relayed)")
    return DispatchResult(transport=provider, detail=str(command))
