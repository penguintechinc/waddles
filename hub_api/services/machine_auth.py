"""Machine-JWT route guard shared by hub-api's internal (data plane) blueprints.

One implementation of "valid short-lived service JWT, exact scope, operator-plane ``system``
tenant" so every internal route family fails closed identically. The ``system`` tenant claim is
stamped by the issuer from the allow-listed identity (never by the caller) and is required because
these routes deliberately span tenants.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, LiteralString

from flask_core.service_jwt import SYSTEM_TENANT, InvalidServiceToken, UnknownKeyId
from quart import current_app, request

from services.provider_connections.telemetry import log_failure

logger = logging.getLogger(__name__)


def machine_authenticated(
    required_scope: str,
    *,
    unconfigured_event: LiteralString,
    rejected_event: LiteralString,
    wrong_tenant_event: LiteralString,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build a decorator requiring a machine JWT holding ``required_scope`` from ``system``.

    Fails closed at every step and never says which check failed: no verifier configured -> 503
    (a deployment fault to be seen, never a fallback to a weaker mechanism), missing/invalid/
    wrong-scope token -> 401, valid token from a non-``system`` tenant -> 403.
    """

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(handler)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            verifier = current_app.config.get("SERVICE_JWT_VERIFIER")
            if verifier is None:
                logger.error(unconfigured_event)
                return {"error": "service authentication is not configured"}, 503
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return {"error": "unauthorized"}, 401
            try:
                claims = verifier.verify(header[len("Bearer ") :], required_scope=required_scope)
            except (UnknownKeyId, InvalidServiceToken) as exc:
                log_failure(logger, rejected_event, exc, level=logging.WARNING)
                return {"error": "unauthorized"}, 401
            if claims.get("tenant") != SYSTEM_TENANT:
                logger.warning(wrong_tenant_event)
                return {"error": "forbidden"}, 403
            return await handler(*args, **kwargs)

        return wrapper

    return decorator
