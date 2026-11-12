"""Authorization check over the WIT ``authz`` import: ``await caller_can("community:ban")``.

``stage-next``-only (``wit/waddle-bundle/stage.wit`` ``interface authz``); always
granted, no capability-gate permission. The host answers "may the actor whose
event triggered this invocation exercise ``scope`` in this community?" from the
community role it resolves itself.

**Authorization source of truth.** The event payload's ``is_mod`` /
``is_broadcaster`` badges are DISPLAY-ONLY: they are platform chat badges, can
be stale or forged by a connector, and must NEVER gate an action. Gate every
privileged action on ``await caller_can(SCOPE_...)`` instead.

**Fail-closed.** A malformed scope, an unavailable host, a backend error or a
missing binding all log (scope + error kind only -- never PII) and return
``False``; this function never raises and never returns ``True`` by default.
A scope is validated locally (same rule as the executor and stage) so a
malformed one costs no host round trip.

**Eager import (componentize-py).** See ``waddle_sdk.identity``: the guarded
``from wit_world.imports import authz`` forces componentize-py to wizen the
binding; host-side tests and a ``stage`` 1.0.0 build have no such module.
"""

from __future__ import annotations

import re

try:  # eager: forces componentize-py to wizen the binding -- see module docstring
    from wit_world.imports import authz as _authz_binding  # noqa: F401
except (ImportError, AttributeError):  # host-side tests / stage-1.0.0 world: no such binding
    _authz_binding = None

SCOPE_MEMBER = "community:member"
SCOPE_VIP = "community:vip"
SCOPE_MODERATE = "community:moderate"
SCOPE_BAN = "community:ban"
SCOPE_TIMEOUT = "community:timeout"
SCOPE_ADMIN = "community:admin"
SCOPE_OWNER = "community:owner"

#: Mirrors the executor + stage rule: 1..=64 bytes of ``^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*$``.
MAX_SCOPE_LEN = 64
_SCOPE_RE = re.compile(r"[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*")


def is_valid_scope(scope: object) -> bool:
    """Return True when ``scope`` is a string the host would accept (length + shape)."""
    return (
        isinstance(scope, str)
        and 1 <= len(scope.encode("utf-8")) <= MAX_SCOPE_LEN
        and _SCOPE_RE.fullmatch(scope) is not None
    )


def _log_denied(reason: str, scope: object, error_kind: str | None = None) -> None:
    """Log a fail-closed outcome (scope + error kind only); logging must never raise."""
    try:
        from waddle_sdk import log

        fields: dict[str, str] = {"reason": reason}
        if is_valid_scope(scope):
            fields["scope"] = str(scope)
        if error_kind is not None:
            fields["error_kind"] = error_kind
        log.warn("authz.caller_can failed closed", **fields)
    except Exception:  # noqa: BLE001, S110 - telemetry failure must not change the verdict
        pass


async def caller_can(scope: str) -> bool:
    """Return True only if the triggering actor holds ``scope`` in this community.

    Any failure (invalid scope, host unavailable/backend error, no binding)
    returns False after logging -- never raises, never defaults to allowed.
    """
    if not is_valid_scope(scope):
        _log_denied("invalid_scope", scope)
        return False
    try:
        import wit_world

        allowed = wit_world.imports.authz.caller_can(scope)
    except Exception as exc:  # noqa: BLE001 - fail closed on every host error
        detail = getattr(exc, "value", exc)
        _log_denied("host_error", scope, type(detail).__name__)
        return False
    return allowed is True
