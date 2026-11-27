"""
Community Access Token (CAT) opt-in
===================================

A CAT (``wdl_c_...``) is a community-scoped MACHINE credential, never a user session. It is
refused everywhere by default: ``tenant_middleware`` only accepts one on a route that was
explicitly opted in with :func:`allow_cat`, and only after

1. the CAT holds every scope the route declares (or the route explicitly declares
   :data:`CAT_SELF_ONLY`: it only ever returns the token's OWN binding, e.g. ``/communities/my``),
2. the route's target community -- extracted by the route's own declared
   :class:`CommunityExtractor`, whatever the param or body field is named -- equals the CAT's
   bound community. The default extractor rejects, so an opt-in that forgets to say where the
   community comes from fails closed.

The handler then sees a :class:`~flask_core.tenancy.CatPrincipal` (community id, token id,
scopes) on ``request.cat_principal`` -- never a user id. Every CAT decision emits an OTel span
event + counter and a structured audit log line (token id, community id, route; never the token).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

from .authz import AuthzDecision, has_required_scopes
from .tenancy import CatPrincipal

logger = logging.getLogger(__name__)

#: Function attribute :func:`allow_cat` sets; ``functools.wraps`` copies it up the decorator stack.
CAT_POLICY_ATTR = "__cat_policy__"

_tracer = trace.get_tracer("waddles.flask_core.cat_auth")
_meter = metrics.get_meter("waddles.flask_core.cat_auth")
_decisions = _meter.create_counter(
    "waddles_cat_auth_decisions_total",
    description="Community Access Token authorization decisions, by result and reason",
)


class _CatSelfOnly:
    """Sentinel scope declaration: the route returns only the CAT's own binding (no capability).

    Instantiated exactly once (``CAT_SELF_ONLY`` below); compare by identity.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Stable repr for logs and the parity test."""
        return "CAT_SELF_ONLY"


#: Explicit declaration that a route needs no capability scope because it only ever discloses
#: the presenting CAT's OWN community binding (token introspection). Never a default.
CAT_SELF_ONLY = _CatSelfOnly()

#: ``async (request, cat) -> community_id | None``. ``None`` (or any non-int) means REJECT.
CommunityExtractor = Callable[[Any, CatPrincipal], Awaitable[int | None]]


async def reject_community(request: Any, cat: CatPrincipal) -> int | None:
    """Default extractor: no declared target community, so every CAT request is refused."""
    return None


async def cat_bound_community(request: Any, cat: CatPrincipal) -> int | None:
    """Target IS the CAT's own community (the route has no community input at all)."""
    return cat.community_id


def from_view_arg(name: str) -> CommunityExtractor:
    """Target community comes from path parameter ``name`` (already converted to an int id)."""

    async def _extract(request: Any, cat: CatPrincipal) -> int | None:
        value = (request.view_args or {}).get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    _extract.__qualname__ = f"from_view_arg({name!r})"
    return _extract


def from_json_body(
    field: str,
    resolve: Callable[[Any], Awaitable[int | None]] | None = None,
) -> CommunityExtractor:
    """Target community comes from JSON body ``field``; ``resolve`` maps a public UUID to an id.

    Without ``resolve`` only an int id is accepted; anything else (missing, string, bool) rejects.
    """

    async def _extract(request: Any, cat: CatPrincipal) -> int | None:
        body = await request.get_json(silent=True)
        if not isinstance(body, dict) or field not in body:
            return None
        value = body[field]
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if resolve is not None and isinstance(value, str):
            return await resolve(value)
        return None

    _extract.__qualname__ = f"from_json_body({field!r})"
    return _extract


ScopeDeclaration = tuple[str, ...] | _CatSelfOnly


@dataclass(slots=True, frozen=True)
class CatPolicy:
    """One route's explicit CAT opt-in: required scopes and where its target community lives."""

    scopes: ScopeDeclaration
    community_from: CommunityExtractor


def allow_cat(
    *,
    scopes: list[str] | tuple[str, ...] | _CatSelfOnly,
    community_from: CommunityExtractor = reject_community,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Opt ONE route into accepting Community Access Tokens.

    Place it BELOW ``tenant_middleware`` (closer to the handler)::

        @bp.route("/admin/<cuuid:community_id>/music/settings")
        @tenant_middleware
        @allow_cat(scopes=["control_music"], community_from=from_view_arg("community_id"))
        async def get_music(community_id: int): ...

    Placed above it the marker never reaches ``tenant_middleware`` and CATs stay refused
    (fail closed). ``scopes`` must be a non-empty list of scope strings or the explicit
    :data:`CAT_SELF_ONLY`; an empty declaration is refused at decoration time.
    """
    if scopes is CAT_SELF_ONLY:
        declared: ScopeDeclaration = CAT_SELF_ONLY
    else:
        declared = tuple(str(s) for s in scopes)  # type: ignore[union-attr]
        if not declared or any(not s.strip() for s in declared):
            raise ValueError(
                "allow_cat() needs explicit non-empty scopes (or CAT_SELF_ONLY)"
            )
    policy = CatPolicy(scopes=declared, community_from=community_from)

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        setattr(f, CAT_POLICY_ATTR, policy)
        return f

    return decorator


def get_cat_policy(f: Any) -> CatPolicy | None:
    """Return the :class:`CatPolicy` a view function was opted in with, or ``None``."""
    policy = getattr(f, CAT_POLICY_ATTR, None)
    return policy if isinstance(policy, CatPolicy) else None


def get_cat_principal(request: Any) -> CatPrincipal | None:
    """Return the verified CAT principal for this request (``None`` for user/JWT requests)."""
    principal = getattr(request, "cat_principal", None)
    return principal if isinstance(principal, CatPrincipal) else None


def _rule(request: Any) -> str:
    """Route template (never the concrete path, which may carry ids) for telemetry labels."""
    rule = getattr(request, "url_rule", None)
    return str(rule.rule) if rule is not None else "unmatched"


def record_cat_decision(
    request: Any,
    *,
    allowed: bool,
    reason: str,
    cat: CatPrincipal | None = None,
    required: tuple[str, ...] = (),
) -> None:
    """Emit the OTel counter + span event + audit log for one CAT decision. Never the token."""
    route = _rule(request)
    result = "ALLOWED" if allowed else "FORBIDDEN"
    _decisions.add(1, {"result": result, "reason": reason, "route": route})
    attrs: dict[str, Any] = {
        "cat.result": result,
        "cat.reason": reason,
        "http.route": route,
    }
    if cat is not None:
        attrs["cat.token_id"] = cat.token_id
        attrs["cat.community_id"] = cat.community_id
    trace.get_current_span().add_event("cat_auth.decision", attributes=attrs)
    if cat is not None:
        request.authz_decision = AuthzDecision(
            required, allowed, reason, f"cat:{cat.token_id}", cat.scopes
        )
    log = logger.info if allowed else logger.warning
    log(
        "cat auth decision result=%s reason=%s route=%s cat_id=%s community_id=%s",
        result,
        reason,
        route,
        cat.token_id if cat is not None else None,
        cat.community_id if cat is not None else None,
        extra={"event_type": "AUDIT", "action": "cat_auth", "result": result},
    )


async def enforce_cat_policy(
    request: Any, policy: CatPolicy, cat: CatPrincipal
) -> tuple[str, int] | None:
    """Apply ``policy`` to a resolved CAT. Returns ``(message, status)`` to refuse, else ``None``."""
    with _tracer.start_as_current_span("cat_auth.enforce") as span:
        span.set_attribute("cat.token_id", cat.token_id)
        span.set_attribute("cat.community_id", cat.community_id)
        span.set_attribute("http.route", _rule(request))
        required: tuple[str, ...] = (
            () if policy.scopes is CAT_SELF_ONLY else policy.scopes
        )  # type: ignore[assignment]
        if required and not has_required_scopes(cat.scopes, required):
            record_cat_decision(
                request,
                allowed=False,
                reason="insufficient_scope",
                cat=cat,
                required=required,
            )
            return "Insufficient scope", 403

        try:
            target = await policy.community_from(request, cat)
        except Exception:
            logger.exception(
                "cat auth: community extractor failed route=%s", _rule(request)
            )
            target = None
        # Defence in depth: whatever the extractor says, a literal `community_id` path param
        # must also match the CAT's binding.
        view_cid = (request.view_args or {}).get("community_id")
        if (
            target is None
            or target != cat.community_id
            or (view_cid is not None and view_cid != cat.community_id)
        ):
            record_cat_decision(
                request,
                allowed=False,
                reason="community_mismatch",
                cat=cat,
                required=required,
            )
            return "Token not authorized for this community", 403

        record_cat_decision(
            request, allowed=True, reason="ok", cat=cat, required=required
        )
        return None
