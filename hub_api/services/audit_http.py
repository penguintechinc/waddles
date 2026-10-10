"""HTTP-layer audit coverage: authz decisions, admin actions, privacy rights, session issuance.

Two chokepoints give hub-api's ~300 mutating routes audit coverage without each handler having to
remember to log:

* :func:`install_audit_hooks` registers one app-wide ``after_request`` hook. It reads what the
  request *already proved* -- the :class:`flask_core.authz.AuthzDecision` ``require_scope``
  published, the ``TenantContext`` ``tenant_middleware`` resolved, the response status -- and
  asks :func:`services.audit_events.classify_request` whether (and as what) it is auditable.
  Everything it records is derived from the URL **rule** (``/api/v1/tenant/<tenant_slug>/admins``),
  the method, the status and the required scope names -- never from the path, query string or
  body -- so no raw user input can reach the audit store.
* :func:`record_session_issued` is called from ``auth_service.create_session_token``, the single
  place every login path (password, OAuth/SSO, passkey, refresh) mints a session. Login requests
  carry no identity yet, so the HTTP hook cannot see them; the mint site can.

Fail-loud: when an *entitled* tenant's event cannot be written, the failure has already been
logged at ERROR and counted by :func:`services.audit_service.report_write_failure`; the hook then
answers the request with a generic 500 rather than returning a success the platform cannot
account for (NIST AU-5 "fail closed"). Un-entitled tenants are unaffected -- the service skips
them by policy before touching the store. The statutory privacy routes are audited like any
other, but the *rights themselves* are never gated: an erasure/DSAR works in every tier, and a
retry after an audit outage is idempotent (erasure answers ``already_deleted``).
"""

from __future__ import annotations

import logging
from typing import Any

from flask_core.authz import AuthzDecision, get_authz_decision
from flask_core.tenancy import TenantContext, get_tenant_context
from quart import Quart, Response, current_app, has_app_context, jsonify, request

from services.audit_events import (
    SEMANTIC_ROUTES,
    ActorKind,
    AuditAction,
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    RequestFacts,
    classify_request,
)
from services.audit_service import AuditService, AuditWriteError, report_write_failure
from services.current_user import get_optional_current_user_id

logger = logging.getLogger(__name__)

#: ``app.config`` key the :class:`AuditService` is published under (set in ``app.py::startup``).
AUDIT_SERVICE_CONFIG_KEY = "audit_service"

_AUDIT_FAILED_BODY = {
    "success": False,
    "error": {
        "message": "The action could not be recorded in the audit log",
        "code": "AUDIT_UNAVAILABLE",
    },
}


def get_service() -> AuditService | None:
    """The app's :class:`AuditService`, or ``None`` outside an app / before startup wired it."""
    if not has_app_context():
        return None
    service = current_app.config.get(AUDIT_SERVICE_CONFIG_KEY)
    return service if isinstance(service, AuditService) else None


def _subject_user_id(decision: AuthzDecision | None) -> int | None:
    """The authenticated caller's ``hub_users.id`` (from the published decision, else the JWT)."""
    if decision is not None and decision.subject is not None:
        try:
            parsed = int(decision.subject)
        except ValueError:
            # A non-numeric subject (machine/service identity) has no ``hub_users.id``; the event
            # is attributed without a user id. The subject value itself is never logged.
            logger.debug("audit hook: authz subject is not a hub_users id; recording without one")
            return None
        return parsed if parsed > 0 else None
    return get_optional_current_user_id(request)


def _facts(response: Response, decision: AuthzDecision | None, user_id: int | None) -> RequestFacts:
    """Collect the request/response facts the classifier needs (no user-supplied text)."""
    rule = request.url_rule.rule if request.url_rule is not None else None
    return RequestFacts(
        method=request.method,
        rule=rule,
        status_code=response.status_code,
        scope_checked=decision is not None,
        denied=decision is not None and not decision.allowed,
        authenticated=user_id is not None,
    )


def _build_event(
    *,
    category: AuditCategory,
    action: str,
    outcome: AuditOutcome,
    user_id: int | None,
    ctx: TenantContext | None,
    decision: AuthzDecision | None,
    response: Response,
) -> AuditEvent:
    """Assemble the validated event for a classified request."""
    details: dict[str, Any] = {
        "method": request.method.upper(),
        "rule": request.url_rule.rule if request.url_rule is not None else "unmatched",
        "status": response.status_code,
    }
    if decision is not None and decision.required_scopes:
        details["required_scopes"] = list(decision.required_scopes)
    if decision is not None and not decision.allowed:
        details["reason"] = decision.reason
    return AuditEvent(
        category=category,
        action=action,
        outcome=outcome,
        actor_kind=ActorKind.USER if user_id is not None else ActorKind.EXTERNAL,
        actor_user_id=user_id,
        tenant_id=ctx.tenant_id if ctx is not None else None,
        tenant_slug=ctx.tenant_slug if ctx is not None else None,
        target_type="http_route",
        target_id=None,
        details=details,
    )


async def _audit_response(response: Response) -> Response:
    """``after_request`` body: classify the finished request and record it if auditable."""
    rule = request.url_rule.rule if request.url_rule is not None else None
    if rule is None or request.method == "OPTIONS":
        return response
    decision = get_authz_decision(request)
    # Cheap pre-filter: only requests that passed/failed a scope check, were refused with 403,
    # or sit on the semantic route map can possibly be auditable -- skip the JWT re-decode for
    # every other (mostly public GET) request.
    if (
        decision is None
        and response.status_code != 403
        and (request.method.upper(), rule) not in SEMANTIC_ROUTES
    ):
        return response
    user_id = _subject_user_id(decision)
    classification = classify_request(_facts(response, decision, user_id))
    if classification is None:
        return response
    service = get_service()
    if service is None:
        # Not wired (test harness that never ran startup). In production startup always
        # publishes the service; tests/test_audit_http.py pins that.
        logger.debug("audit hook: no AuditService wired; request not audited")
        return response
    try:
        event = _build_event(
            category=classification.category,
            action=classification.action,
            outcome=classification.outcome,
            user_id=user_id,
            ctx=get_tenant_context(request),
            decision=decision,
            response=response,
        )
        await service.record(event)
    except AuditWriteError:
        # The failure itself is already logged (ERROR + traceback) and counted by the service.
        logger.warning("audit hook: audit write failed; answering the request with a generic 500")
        return _audit_failure_response()
    except Exception as exc:
        # Building the event failed (a programming error): same treatment -- loud, then 500.
        report_write_failure(
            category=classification.category.value,
            action=str(classification.action),
            chain_id=None,
            attempts=0,
            exc=exc,
        )
        return _audit_failure_response()
    return response


def _audit_failure_response() -> Response:
    """Generic 500 for an unrecordable audited request (no internals, no PII)."""
    failed: Response = jsonify(_AUDIT_FAILED_BODY)
    failed.status_code = 500
    return failed


def install_audit_hooks(app: Quart) -> None:
    """Register the app-wide audit ``after_request`` hook (call once from ``create_app``)."""
    app.after_request(_audit_response)


async def record_session_issued(
    *,
    user_id: int | None,
    tenant_id: int | None,
    tenant_slug: str | None,
    auth_method: str,
    pending_link: bool = False,
) -> None:
    """Audit a session mint (login / SSO / passkey / refresh); raises on an unrecordable event.

    ``user_id`` is ``None`` for the temp-password pending-link session (no subject yet), which is
    recorded with ``actor_kind=unresolved``. A no-op only when no ``AuditService`` is wired (a
    harness that never ran startup); an un-entitled tenant is skipped inside the service.

    Raises:
        AuditWriteError: the event could not be recorded -- the caller must not issue the session.
    """
    service = get_service()
    if service is None:
        logger.debug("session audit: no AuditService wired; session issuance not audited")
        return
    # `tenant_slug` can be a login form's raw value; only a tenant that really exists may pick
    # the chain or reach the entitlement gate, so resolve it first. An unknown tenant's login
    # (the session is useless: tenant_middleware will 403 it) is recorded on the platform chain.
    known = await service.find_tenant(tenant_id=tenant_id, tenant_slug=tenant_slug)
    event = AuditEvent(
        category=AuditCategory.AUTHN,
        action=AuditAction.SESSION_ISSUED,
        outcome=AuditOutcome.SUCCESS,
        actor_kind=ActorKind.USER if user_id is not None else ActorKind.UNRESOLVED,
        actor_user_id=user_id,
        tenant_id=known[0] if known is not None else None,
        target_type="session",
        details={
            "auth_method": auth_method,
            "pending_link": pending_link,
            "tenant_known": known is not None,
        },
    )
    await service.record(event)
