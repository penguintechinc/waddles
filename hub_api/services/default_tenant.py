"""Default-tenant resolution with loud failure -- the single guard for `DEFAULT_TENANT_SLUG`.

The platform's catch-all tenant is the reserved slug `default` (security.md Reserved Tenants &
Federation); `system` is the operator plane; there is NO reserved `global` tenant. The slug lives
in exactly one place, `flask_core.auth.DEFAULT_TENANT_SLUG`, and `HubAPIConfig.default_tenant_slug`
(env `DEFAULT_TENANT_SLUG`) overrides it per deployment.

If the configured slug does not match a real, active `tenants` row every pre-auth endpoint that
scopes to the default tenant is unusable. That is a deployment-configuration error, so it fails
LOUD here: an ERROR log naming the configured slug and which reserved slugs actually exist, and a
typed `DefaultTenantMissingError` the blueprints turn into a structured 5xx. Tenants are never
auto-created -- creating one here would paper over a bad seed or a wrong env var.
"""

from __future__ import annotations

import logging
from typing import Any

from flask_core.auth import DEFAULT_TENANT_SLUG

from services.global_identity import SYSTEM_TENANT_SLUG

logger = logging.getLogger(__name__)

#: Reserved tenant slugs reported in the ERROR log (security.md Reserved Tenants & Federation).
RESERVED_SLUGS: tuple[str, ...] = (DEFAULT_TENANT_SLUG, SYSTEM_TENANT_SLUG)


class DefaultTenantMissingError(LookupError):
    """The configured default tenant slug has no active `tenants` row (deployment-config error)."""

    def __init__(self, slug: str, existing_reserved: list[str]) -> None:
        """Record the configured `slug` and the reserved slugs that DO exist in the table."""
        self.slug = slug
        self.existing_reserved = existing_reserved
        super().__init__(
            f"default tenant '{slug}' missing or inactive "
            f"(existing reserved tenants: {existing_reserved or 'none'}; "
            f"expected the reserved catch-all slug '{DEFAULT_TENANT_SLUG}'"
            " -- check DEFAULT_TENANT_SLUG)"
        )


async def _existing_reserved_slugs(async_dal: Any, dal: Any) -> list[str]:
    """Return which of `RESERVED_SLUGS` are present in `tenants` (best effort, never raises)."""
    try:
        rows = await async_dal.select_async(dal(dal.tenants.slug.belongs(list(RESERVED_SLUGS))))
        return sorted(str(r.slug) for r in rows)
    except Exception:  # noqa: BLE001 - diagnostics only; the caller already has a failure to report
        logger.warning("could not list reserved tenant slugs for diagnostics", exc_info=True)
        return []


async def require_default_tenant(async_dal: Any, dal: Any, slug: str) -> Any:
    """Return the active `tenants` row for `slug`, or log an ERROR and raise.

    Raises `DefaultTenantMissingError` when the row is missing or inactive.

    The one place the default tenant row is looked up, so a bad `DEFAULT_TENANT_SLUG` surfaces the
    same loud, diagnosable failure at startup and on first use -- never an empty result.
    """
    rows = await async_dal.select_async(dal(dal.tenants.slug == slug))
    row = rows.first() if rows else None
    if row is None or not row.is_active:
        existing = await _existing_reserved_slugs(async_dal, dal)
        logger.error(
            "default tenant slug %r is missing or inactive in the tenants table; existing "
            "reserved slugs: %s (the reserved catch-all is %r, the operator plane is %r)",
            slug,
            existing or "none",
            DEFAULT_TENANT_SLUG,
            SYSTEM_TENANT_SLUG,
            extra={"event_type": "SYSTEM", "action": "default_tenant_check", "result": "FAILURE"},
        )
        raise DefaultTenantMissingError(slug, existing)
    return row


async def verify_default_tenant_at_startup(async_dal: Any, dal: Any, slug: str) -> str:
    """Check the default tenant once at hub-api startup; log, never crash, return the outcome.

    Returns `"ok"`, `"missing"` (ERROR already logged) or `"unverifiable"` (the `tenants` table is
    not readable yet, e.g. the schema is still being bootstrapped -- first use re-checks).
    Crashing here would make a legitimately waiting pod restart-loop, so the loud signals are the
    ERROR log plus the 5xx on first use.
    """
    try:
        await require_default_tenant(async_dal, dal, slug)
    except DefaultTenantMissingError:
        return "missing"
    except Exception:  # noqa: BLE001 - schema not ready / DB unreachable at boot
        try:
            dal.rollback()
        except Exception:  # noqa: BLE001 - nothing to roll back
            logger.debug("rollback after unverifiable default-tenant check failed", exc_info=True)
        logger.warning(
            "default tenant %r could not be verified at startup (tenants table not readable yet);"
            " it will be re-checked on first use",
            slug,
            exc_info=True,
        )
        return "unverifiable"
    logger.info("default tenant %r verified at startup", slug)
    return "ok"
