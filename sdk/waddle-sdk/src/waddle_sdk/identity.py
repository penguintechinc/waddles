"""Actor / mention -> community ``user_uuid`` over the WIT ``identity`` import.

``stage-next``-only: ``wit/waddle-bundle/stage.wit``'s ``interface identity``
exists only in ``world stage-next`` (a bundle must be built against that world
and hold the ``identity.resolve`` permission). It is the prerequisite of every
points-game bundle: ``economy`` and ``reputation`` name their targets by the
community ``user_uuid``, and a bundle only ever holds the tokenized
``{user:<token>}`` placeholder -- which is NOT that uuid. So the bundle asks the
host::

    actor  = await identity.resolve_actor()            # whose message triggered me
    for token in identity.mention_tokens(event.text):  # who did they @mention
        target = await identity.resolve_mention(token)
    await economy.transfer(actor, target, 25)

**UUID-only, PII-safe.** Both calls return a canonical lower-case UUID string
and nothing else -- never a platform id, handle or display name. Neither takes
an argument that selects WHOSE identity is resolved: ``resolve_actor`` takes
none (the host derives the triggering actor from the event it delivered), and
``resolve_mention`` takes only the opaque token the bundle was already shown.
The host answers ONLY for mentions present in the triggering message, so it is
not a directory lookup: an unknown token, or a raw handle the bundle was never
shown, is :class:`NotFoundError`.

**Fail-closed.** Every refusal raises one of this module's exceptions and a
caller that does not catch it fails the invocation loudly -- there is no default,
guessed or pseudonym uuid on any path. In particular an identity that exists but
is not yet linked is :class:`NotLinkedError` (tell the user their account is not
linked; never proceed), and a handle that matches several identities is
:class:`AmbiguousError`.

**Eager import (componentize-py).** componentize-py only wizens a WIT host-import
submodule that something imports at Python MODULE-LOAD time (see
``_component_entry.py`` and the project note on eager wizening): a capability
referenced only inside function bodies is absent from ``wit_world.imports`` at
runtime despite the component declaring it. The ``from wit_world.imports import
identity`` just below forces that binding to be wizened; it is try/except-guarded
because host-side unit tests (and a ``stage`` 1.0.0 build) legitimately have no
such module.

**Binding shapes** (componentize-py ``bindings`` for the committed WIT):
``resolve_actor() -> str``, ``resolve_mention(token: str) -> str``, each raising
the generated ``Err`` (``.value`` = the ``identity.error`` variant:
``Error_Denied(value: str)``, ``Error_NotLinked``, ``Error_NotAMember``,
``Error_NotFound``, ``Error_Ambiguous``, ``Error_Invalid(value: str)``,
``Error_Unavailable(value: str)``, ``Error_Backend(value: str)``). This module
classifies it structurally via ``getattr(exc, "value", exc)`` (same pattern as
``waddle_sdk.economy``) and re-raises one of this module's exceptions.
"""

from __future__ import annotations

from uuid import UUID

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import identity as _identity_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _identity_binding = None

#: Mirrors ``core/bundle_executor/src/host/stage_next_identity.rs::
#: MAX_MENTION_TOKEN_LEN`` (pinned by ``tests/test_identity.py``): a longer token
#: is refused before it costs a host round trip.
MAX_MENTION_TOKEN_LEN = 256

_PLACEHOLDER_OPEN = "{user:"


class IdentityError(Exception):
    """Base for every error this facade raises."""


class InvalidIdentityArgError(IdentityError, ValueError):
    """An argument is malformed (raised before any host call, or by the host as ``invalid``)."""


class DeniedError(IdentityError):
    """The host gate denied the call; ``code`` is the gate's stable denial code.

    e.g. ``not_granted`` (the bundle never declared ``identity.resolve``),
    ``rate_limited``, ``instance_denied``.
    """

    def __init__(self, message: str, code: str) -> None:
        """Store the gate's stable denial ``code`` alongside the message."""
        super().__init__(message)
        self.code = code


class NotLinkedError(IdentityError):
    """The identity has no resolved community user_uuid yet (its account is not linked).

    Surface this to the user ("link your account first") and stop -- never
    continue with a guessed or substituted identity.
    """


class NotAMemberError(IdentityError):
    """The identity resolves but is not an active member of this invocation's community."""


class NotFoundError(IdentityError):
    """The token is not a mention this message carried, or matches no identity in the tenant."""


class AmbiguousError(IdentityError):
    """A free-text handle matches more than one identity; the host never guesses between them."""


class UnavailableError(IdentityError):
    """The capability is unprovisioned, flag-off, or hub-api's resolver is unreachable."""


class BackendError(IdentityError):
    """Host-side failure (details are deliberately not exposed to bundles)."""


def mention_tokens(text: str) -> list[str]:
    r"""Return the mention tokens in ``text``, in order: the ``<token>`` of each ``{user:<token>}``.

    ``text`` is the chat text a bundle is delivered, in which the host replaced
    every mention with a ``{user:<token>}`` placeholder. A literal ``{user:..}``
    a chatter TYPED is escaped by the host (``\{user:..\}``) and is skipped here
    -- and even a mis-parse would be harmless: :func:`resolve_mention` only
    resolves tokens the host itself issued for this invocation, so a forged one is
    :class:`NotFoundError`. The token is opaque: do not parse it or treat it as a
    user id.
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":  # an escaped character (host-side brace/backslash escaping)
            i += 2
            continue
        if text.startswith(_PLACEHOLDER_OPEN, i):
            end = text.find("}", i + len(_PLACEHOLDER_OPEN))
            if end == -1:
                break
            token = text[i + len(_PLACEHOLDER_OPEN) : end]
            if token and "\\" not in token and "{" not in token:
                tokens.append(token)
            i = end + 1
            continue
        i += 1
    return tokens


def validate_token(token: str) -> str:
    """Return ``token`` stripped, or raise :class:`InvalidIdentityArgError` if unusable."""
    if not isinstance(token, str):
        raise InvalidIdentityArgError(f"token must be a string, got {type(token).__name__}")
    cleaned = token.strip()
    if not cleaned or len(cleaned.encode("utf-8")) > MAX_MENTION_TOKEN_LEN:
        raise InvalidIdentityArgError(f"token must be 1..={MAX_MENTION_TOKEN_LEN} bytes")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        raise InvalidIdentityArgError("token must not contain control characters")
    return cleaned


def _canonical(op: str, value: object) -> str:
    """Return ``value`` as canonical UUID text, or raise :class:`BackendError` (never non-uuid)."""
    try:
        text = str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BackendError(f"identity.{op} returned a non-uuid value") from exc
    if text != str(value):
        raise BackendError(f"identity.{op} returned a non-canonical uuid")
    return text


def _raise_for(exc: Exception, op: str) -> None:
    """Classify a raised WIT ``identity.error`` and re-raise as this module's own exception."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    payload = getattr(detail, "value", None)
    message = f"identity.{op} failed: {type_name}" + (f": {payload}" if payload is not None else "")
    if type_name == "Error_Denied":
        raise DeniedError(message, code=str(payload)) from exc
    if type_name == "Error_NotLinked":
        raise NotLinkedError(message) from exc
    if type_name == "Error_NotAMember":
        raise NotAMemberError(message) from exc
    if type_name == "Error_NotFound":
        raise NotFoundError(message) from exc
    if type_name == "Error_Ambiguous":
        raise AmbiguousError(message) from exc
    if type_name == "Error_Invalid":
        raise InvalidIdentityArgError(message) from exc
    if type_name == "Error_Unavailable":
        raise UnavailableError(message) from exc
    raise BackendError(message) from exc


async def resolve_actor() -> str:
    """Return the community ``user_uuid`` of the actor whose event triggered this invocation.

    Takes no argument: the host derives the actor. Raises :class:`NotLinkedError`
    when the actor has no resolved identity, :class:`NotAMemberError` when they
    are not an active member of this community, and :class:`DeniedError` /
    :class:`UnavailableError` when the capability is not granted / not enabled.
    """
    import wit_world

    try:
        value = wit_world.imports.identity.resolve_actor()
    except IdentityError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "resolve_actor")
        raise  # unreachable -- _raise_for always raises
    return _canonical("resolve_actor", value)


async def resolve_mention(token: str) -> str:
    """Return the community ``user_uuid`` of the user a mention in the triggering message names.

    ``token`` is one of :func:`mention_tokens`' results (the opaque token inside
    a ``{user:<token>}`` placeholder; the placeholder itself is also accepted).
    Raises :class:`NotFoundError` for a token the message did not carry,
    :class:`AmbiguousError` for a handle matching several identities,
    :class:`NotLinkedError` / :class:`NotAMemberError` for a target who is
    unresolved / not in this community.
    """
    cleaned = validate_token(token)
    import wit_world

    try:
        value = wit_world.imports.identity.resolve_mention(cleaned)
    except IdentityError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "resolve_mention")
        raise  # unreachable -- _raise_for always raises
    return _canonical("resolve_mention", value)
