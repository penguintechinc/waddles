"""Defence-in-depth guard: operator-only actions require a `system`-tenant caller.

The scope bundles already keep `tenants:admin` / `communities:admin` / `users:admin` away from
tenant admins (only the global bundle's `*:admin` holds them). This guard additionally requires
the VALIDATED tenant claim (never request data) to be the reserved `system` tenant, so a stray
scope grant or a federated operator session inside a customer tenant can never reach an
operator action such as a tier override or a license-pool edit.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, cast

from flask_core.api_utils import error_response
from flask_core.tenancy import get_tenant_context
from quart import request

from services.global_identity import SYSTEM_TENANT_SLUG

_DENIED_MESSAGE = "Operator action requires the system tenant"


def is_system_tenant_request() -> bool:
    """True when the current request's validated tenant claim is the `system` tenant."""
    ctx = get_tenant_context(request)
    return ctx is not None and ctx.tenant_slug == SYSTEM_TENANT_SLUG


def system_tenant_denied() -> tuple[dict[str, object], int]:
    """The 403 envelope returned for a non-system-tenant caller."""
    return cast(tuple[dict[str, object], int], error_response(_DENIED_MESSAGE, 403, "FORBIDDEN"))


def require_system_tenant(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Route decorator (place under `@tenant_middleware`): 403 unless the caller is `system`."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_system_tenant_request():
            return system_tenant_denied()
        return await fn(*args, **kwargs)

    return wrapper
