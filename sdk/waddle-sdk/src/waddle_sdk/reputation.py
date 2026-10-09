"""Community-scoped reputation over the WIT ``reputation`` import (issue #726).

``stage-next``-only: ``wit/waddle-bundle/stage.wit``'s ``interface reputation``
exists only in ``world stage-next`` (a bundle must be built against that
world and hold the ``reputation.read`` / ``reputation.community.write``
permissions). The community, tenant and app are always server-derived -- a
bundle names only the TARGET user (a UUID string), the ``delta`` and a
``reason`` code. The host additionally verifies the target is an active member
of the invocation's community, enforces the declared/catalog delta bounds and
the durable rolling-24h per-user cap, and audits every applied adjustment.

**Eager import (componentize-py).** componentize-py only wizens a WIT
host-import submodule that something imports at Python MODULE-LOAD time (see
``_component_entry.py`` and the project note on eager wizening): a capability
referenced only inside function bodies is absent from ``wit_world.imports`` at
runtime despite the component declaring it. The ``from wit_world.imports
import reputation`` just below forces that binding to be wizened; it is
try/except-guarded because host-side unit tests (and a ``stage`` 1.0.0 build)
legitimately have no such module.

**Binding shapes** (componentize-py ``bindings`` for the committed WIT):
``get(user: str) -> int`` / ``adjust(user: str, delta: int, reason: str) ->
int``, each raising the generated ``Err`` (``.value`` = the ``reputation.error``
variant: ``Error_Denied(value: str)``, ``Error_NotAMember``,
``Error_DailyCapExceeded``, ``Error_Invalid(value: str)``,
``Error_Unavailable(value: str)``, ``Error_Backend(value: str)``). This module
classifies it structurally via ``getattr(exc, "value", exc)`` (same pattern as
``waddle_sdk.db``/``kv``) and re-raises one of this module's exceptions --
a gate denial is NEVER swallowed into a default balance.
"""

from __future__ import annotations

import re
from uuid import UUID

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import reputation as _reputation_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _reputation_binding = None

#: Mirrors ``core/bundle_host_reputation/src/lib.rs::MAX_REASON_LEN`` (pinned by
#: ``tests/test_reputation.py::test_reason_rules_match_host_lib_rs``).
MAX_REASON_LEN = 100

#: Mirrors ``validate_reason``'s charset in the same Rust file: ``[a-z0-9._:-]``.
_REASON_RE = re.compile(r"[a-z0-9._:\-]+")

_I32_MIN = -(2**31)
_I32_MAX = 2**31 - 1


class ReputationError(Exception):
    """Base for every error this facade raises."""


class InvalidReputationArgError(ReputationError, ValueError):
    """A ``user``/``delta``/``reason`` argument is malformed (raised before any host call)."""


class DeniedError(ReputationError):
    """The host gate denied the call; ``code`` is the gate's stable denial code.

    e.g. ``not_granted``, ``delta_out_of_bounds``, ``quota_exceeded``,
    ``rate_limited``, ``instance_denied``.
    """

    def __init__(self, message: str, code: str) -> None:
        """Store the gate's stable denial ``code`` alongside the message."""
        super().__init__(message)
        self.code = code


class NotAMemberError(ReputationError):
    """The target user is not an active member of this invocation's community."""


class DailyCapExceededError(ReputationError):
    """Applying the delta would exceed the user's rolling-24h absolute-delta cap."""


class UnavailableError(ReputationError):
    """The capability is not wired or its feature flag is off on this host."""


class BackendError(ReputationError):
    """Host-side storage failure (details are deliberately not exposed to bundles)."""


def validate_user(user: str) -> str:
    """Return ``user`` normalized to canonical lowercase hyphenated UUID text, or raise."""
    try:
        return str(UUID(str(user)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidReputationArgError(f"user must be a UUID string, got {user!r}") from exc


def validate_reason(reason: str) -> str:
    """Return ``reason`` if it is a 1..=100 char ``[a-z0-9._:-]`` machine code, else raise."""
    if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON_LEN:
        raise InvalidReputationArgError(f"reason must be 1..={MAX_REASON_LEN} characters")
    if _REASON_RE.fullmatch(reason) is None:
        raise InvalidReputationArgError("reason must match [a-z0-9._:-]")
    return reason


def _raise_for(exc: Exception, op: str) -> None:
    """Classify a raised WIT ``reputation.error`` and re-raise as this module's own exception."""
    detail = getattr(exc, "value", exc)
    type_name = type(detail).__name__
    payload = getattr(detail, "value", None)
    message = f"reputation.{op} failed: {type_name}" + (f": {payload}" if payload else "")
    if type_name == "Error_Denied":
        raise DeniedError(message, code=str(payload)) from exc
    if type_name == "Error_NotAMember":
        raise NotAMemberError(message) from exc
    if type_name == "Error_DailyCapExceeded":
        raise DailyCapExceededError(message) from exc
    if type_name == "Error_Invalid":
        raise InvalidReputationArgError(message) from exc
    if type_name == "Error_Unavailable":
        raise UnavailableError(message) from exc
    raise BackendError(message) from exc


async def get(user: str) -> int:
    """Return ``user``'s community-scoped reputation (0 for a member with no adjustments)."""
    canonical = validate_user(user)
    import wit_world

    try:
        return int(wit_world.imports.reputation.get(canonical))
    except ReputationError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "get")
        raise  # unreachable -- _raise_for always raises


async def adjust(user: str, delta: int, reason: str) -> int:
    """Atomically add ``delta`` (may be negative) to ``user``'s score; return the NEW score.

    Raises one of this module's :class:`ReputationError` subclasses on any
    failure -- notably :class:`DeniedError` on a gate denial (never a silent
    no-op or a stale balance).
    """
    canonical = validate_user(user)
    if isinstance(delta, bool) or not isinstance(delta, int) or not _I32_MIN <= delta <= _I32_MAX:
        raise InvalidReputationArgError(f"delta must be a 32-bit integer, got {delta!r}")
    reason = validate_reason(reason)
    import wit_world

    try:
        return int(wit_world.imports.reputation.adjust(canonical, delta, reason))
    except ReputationError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified structurally, see module docstring
        _raise_for(exc, "adjust")
        raise  # unreachable -- _raise_for always raises
