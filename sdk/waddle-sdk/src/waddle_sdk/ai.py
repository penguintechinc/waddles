"""Guarded text generation over the WIT ``ai`` import (the ``ai.generate`` permission).

``stage-next``-only: ``wit/waddle-bundle/stage.wit``'s ``interface ai`` exists
only in ``world stage-next`` (a bundle must be built against that world and hold
the DANGEROUS ``ai.generate`` permission; the host rate limits it to 10 calls
per 60 s). **NOT PRODUCTION-READY -- do not enable
waddles.bundle-ai-capability in any environment until issue #858 is resolved.**
Enterprise-tier enforcement is NOT YET implemented (issue #858); do not rely on
it. A bundle supplies ONE free-text ``prompt`` and three options; the model,
endpoint, system prompt, tenant and community are all
host-controlled and cannot be named, widened or overridden from here.

**The prompt is checked, framed and bounded by the host.**

* A prompt over ~2000 tokens, or ``max_tokens`` over 512, raises
  :class:`TooManyTokensError` (``bound`` is the limit that was exceeded).
* A prompt carrying PII or a secret-shaped string (an email, phone, SSN, card
  number, API token, raw platform mention, raw ``@handle``, IP address) is
  REJECTED -- never redacted -- with :class:`DeniedError` ``code ==
  "contains_pii"``. A ``{user:<uuid>}`` placeholder is fine; tokenize or strip
  everything else first. The refusal names only a category, never your text.
* The host frames your text as UNTRUSTED data inside its own system preamble, so
  instructions embedded in the prompt (or in chat text you fold into it) cannot
  replace the host's rules -- but that is a mitigation, not a guarantee.

**The completion is untrusted text.** Treat it exactly like any other
user-supplied string: never execute it, never use it as a permission, identity
or routing decision, and sanitize it again for whatever sink it reaches. The host
already strips markdown-image beacons, active HTML, mass/role/user mentions and
invisible characters and caps the length, but the content is still the model's.

**Fail loud.** Every refusal raises one of this module's exceptions -- a failure
is NEVER swallowed into an empty or canned completion. A slow or unreachable
model raises :class:`UnavailableError` (the host abandons the call before the
invocation's own deadline); retry later, do not spin.

**Eager import (componentize-py).** componentize-py only wizens a WIT
host-import submodule that something imports at Python MODULE-LOAD time (see
``_component_entry.py`` and the project note on eager wizening): a capability
referenced only inside function bodies is absent from ``wit_world.imports`` at
runtime despite the component declaring it. The ``from wit_world.imports
import ai`` just below forces that binding to be wizened; it is try/except-
guarded because host-side unit tests (and a ``stage`` 1.0.0 build) legitimately
have no such module.

**Binding shapes** (componentize-py ``bindings`` for the committed WIT):
``generate(prompt: str, opts: Options) -> str`` where ``Options`` is the record
``Options(max_tokens: Optional[int], temperature: Optional[float], json: bool)``,
raising the generated ``Err`` (``.value`` = the ``ai.error`` variant:
``Error_Denied(value: str)``, ``Error_TooManyTokens(value: int)``,
``Error_ContentFiltered``, ``Error_Invalid(value: str)``,
``Error_Unavailable(value: str)``, ``Error_Backend(value: str)``). This module
classifies it structurally via ``getattr(exc, "value", exc)`` (same pattern as
``waddle_sdk.economy``) and re-raises one of this module's exceptions.
"""

from __future__ import annotations

import math

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import ai as _ai_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _ai_binding = None

#: Mirrors ``core/svc_process/src/ai.rs::MAX_COMPLETION_TOKENS`` (pinned by
#: ``tests/test_ai.py::test_limits_match_the_stage_ai_rs``): the largest
#: ``max_tokens`` the host accepts.
MAX_COMPLETION_TOKENS = 512

#: Mirrors ``core/svc_process/src/ai.rs::MAX_PROMPT_TOKENS``: the prompt bound.
MAX_PROMPT_TOKENS = 2_000

#: Mirrors ``core/svc_process/src/ai.rs::MAX_TEMPERATURE``.
MAX_TEMPERATURE = 2.0


class AiError(Exception):
    """Base for every error this facade raises."""


class InvalidAiArgError(AiError, ValueError):
    """An argument is malformed (raised before any host call, or by the host as ``invalid``)."""


class DeniedError(AiError):
    """The host gate denied the call, or refused the prompt's content; ``code`` is stable.

    e.g. ``not_granted``, ``rate_limited``, ``quota_exceeded``, ``instance_denied``
    and ``contains_pii`` (the prompt carries PII or a secret-shaped string).
    """

    def __init__(self, message: str, code: str) -> None:
        """Store the host's stable denial ``code`` alongside the message."""
        super().__init__(message)
        self.code = code


class TooManyTokensError(AiError):
    """The prompt or ``max_tokens`` exceeds the host's bound; ``bound`` is that bound."""

    def __init__(self, message: str, bound: int) -> None:
        """Store the bound the host enforces."""
        super().__init__(message)
        self.bound = bound


class ContentFilteredError(AiError):
    """The model produced no usable text, or the host refused to pass its output on."""


class UnavailableError(AiError):
    """The capability is unprovisioned/flag-off, or the model is unreachable or timed out."""


class BackendError(AiError):
    """Host-side failure (details are deliberately not exposed to bundles)."""


def validate_prompt(prompt: str) -> str:
    """Return ``prompt`` if it is a non-empty ``str``, else raise.

    Only the cheap shape check: the token bound and PII rejection are the HOST's
    job (a client-side copy of either would drift from the real guard).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidAiArgError("prompt must be a non-empty string")
    return prompt


def validate_max_tokens(max_tokens: int | None) -> int | None:
    """Return ``max_tokens`` if ``None`` or a non-bool int in ``1..=MAX_COMPLETION_TOKENS``."""
    if max_tokens is None:
        return None
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        raise InvalidAiArgError(f"max_tokens must be an integer or None, got {max_tokens!r}")
    if not 1 <= max_tokens <= MAX_COMPLETION_TOKENS:
        raise TooManyTokensError(
            f"max_tokens must be 1..={MAX_COMPLETION_TOKENS}, got {max_tokens}",
            bound=MAX_COMPLETION_TOKENS,
        )
    return max_tokens


def validate_temperature(temperature: float | None) -> float | None:
    """Return ``temperature`` if ``None`` or a finite number in ``0.0..=MAX_TEMPERATURE``."""
    if temperature is None:
        return None
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, int | float)
        or not math.isfinite(temperature)
        or not 0.0 <= temperature <= MAX_TEMPERATURE
    ):
        raise InvalidAiArgError(
            f"temperature must be a number in 0.0..={MAX_TEMPERATURE}, got {temperature!r}"
        )
    return float(temperature)


def _raise_for(exc: Exception) -> None:
    """Classify a raised WIT ``ai.error`` and re-raise as this module's own exception."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    payload = getattr(detail, "value", None)
    message = "ai.generate failed: " + type_name + (f": {payload}" if payload is not None else "")
    if type_name == "Error_Denied":
        raise DeniedError(message, code=str(payload)) from exc
    if type_name == "Error_TooManyTokens":
        raise TooManyTokensError(message, bound=int(payload or 0)) from exc
    if type_name == "Error_ContentFiltered":
        raise ContentFilteredError(message) from exc
    if type_name == "Error_Invalid":
        raise InvalidAiArgError(message) from exc
    if type_name == "Error_Unavailable":
        raise UnavailableError(message) from exc
    raise BackendError(message) from exc


async def generate(
    prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    json: bool = False,
) -> str:
    """Return the model's completion for ``prompt`` (UNTRUSTED text -- see the module docstring).

    ``max_tokens`` (1..=512) bounds the completion length; ``temperature``
    (0.0..=2.0) the sampling; ``json=True`` selects the structured-output model
    and asks it for JSON (the result is still untrusted text to parse
    defensively). ``None`` options take the host defaults.

    Raises one of this module's :class:`AiError` subclasses on any refusal --
    notably :class:`DeniedError` (``contains_pii``, ``rate_limited``,
    ``not_granted``), :class:`TooManyTokensError`, :class:`UnavailableError` and
    :class:`ContentFilteredError`. Never returns an empty or canned completion.
    """
    prompt = validate_prompt(prompt)
    max_tokens = validate_max_tokens(max_tokens)
    temperature = validate_temperature(temperature)
    if not isinstance(json, bool):
        raise InvalidAiArgError(f"json must be a bool, got {json!r}")
    import wit_world

    try:
        opts = wit_world.imports.ai.Options(
            max_tokens=max_tokens, temperature=temperature, json=json
        )
        return str(wit_world.imports.ai.generate(prompt, opts))
    except AiError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc)
        raise  # unreachable -- _raise_for always raises
