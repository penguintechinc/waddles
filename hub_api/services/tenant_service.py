"""Core tenant entity CRUD -- ported from `tenantController.js`.

Mounted at `/api/v1/tenant/<tenant_slug>*` in Node (`routes/tenant.js`),
gated there by `requireAuth` + `resolveTenant` + `requireTenantAdmin`.

**Security fix vs. faithful Node behavior** (see `hub_api/PORTING.md`'s
"do not reproduce vulnerabilities" scope): Node's `routes/tenant.js` wires
`router.use(requireAuth)` (no tenant context yet) BEFORE the per-route
`resolveTenant` middleware that sets `req.tenant` from the URL's
`:tenantSlug` -- so `requireAuth`'s own `req.isTenantAdmin` resolution
(which needs `req.tenant` to already be set) never fires for any route in
this file, and only `req.user.isSuperAdmin` can ever satisfy
`requireTenantAdmin` today. That's a functional bug (legitimate
non-superadmin tenant admins are locked out), not the vulnerability class
this port cares about -- but it also means Node's real, working design
resolves "which tenant" from the URL path param, a shape that would be a
textbook IDOR (`security.md` Authentication & Authorization: "Client
cannot set tenant -- auth service only") once the ordering bug is fixed:
a caller could name ANY `:tenantSlug` in the URL and, if they happened to
hold a `tenant_admins` row for that specific tenant, act on it regardless
of which tenant issued their JWT.

This port does not reproduce that shape. `flask_core.tenancy.
tenant_middleware` resolves `TenantContext` exclusively from the caller's
OWN JWT `tenant` claim (never from a URL/body param) -- every function
below takes that resolved `tenant_id` as its ONLY source of "which
tenant", and `blueprints/v1/tenant.py` additionally 403s if the URL's
`tenant_slug` doesn't match `ctx.tenant_slug` before calling into this
module at all. Net effect: a tenant admin can only ever manage the tenant
their own token was issued for, full stop -- cross-tenant management (the
`superadminController.js` equivalent, `/api/v1/superadmin/tenants/*`) is
a different controller, out of this group's scope, not something this
endpoint grants a path to.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from services.errors import bad_request, forbidden, not_found

VALID_TENANT_ADMIN_ROLES = ("tenant-admin", "tenant-owner")


async def get_tenant(async_dal: Any, dal: Any, *, tenant_id: int) -> Any:
    """Return the full `tenants` row for `tenant_id`. Raises 404 if somehow missing.

    `tenant_id` is always `TenantContext.tenant_id` (already validated,
    active, existing) by the time this is called -- the 404 branch is
    defensive (a tenant deleted between JWT issuance and this request),
    not an expected path.
    """
    rows = await async_dal.select_async(dal(dal.tenants.id == tenant_id))
    if not rows:
        raise not_found("Tenant not found")
    return rows.first()


async def update_tenant(
    async_dal: Any,
    dal: Any,
    *,
    tenant_id: int,
    display_name: str | None,
    description: str | None,
    logo_url: str | None,
    config: dict[str, Any] | None,
) -> None:
    """Update core tenant fields. Raises 400 if no fields were provided."""
    update_fields: dict[str, Any] = {}
    if display_name is not None:
        update_fields["display_name"] = display_name
    if description is not None:
        update_fields["description"] = description
    if logo_url is not None:
        update_fields["logo_url"] = logo_url
    if config is not None:
        update_fields["config"] = config

    if not update_fields:
        raise bad_request("No updates provided")

    update_fields["updated_at"] = datetime.now(UTC)
    await async_dal.update_async(dal.tenants.id == tenant_id, **update_fields)


async def get_tenant_settings(async_dal: Any, dal: Any, *, tenant_id: int) -> list[Any]:
    """List all settings for a tenant, ordered by key."""
    rows = await async_dal.select_async(
        dal(dal.tenant_settings.tenant_id == tenant_id), orderby=dal.tenant_settings.key
    )
    return list(rows)


async def update_tenant_settings(
    async_dal: Any, dal: Any, *, tenant_id: int, settings: list[tuple[str, str | None]]
) -> None:
    """Upsert each `(key, value)` pair. Raises 400 on an empty list or an invalid key.

    pydal has no `ON CONFLICT DO UPDATE` builder primitive (and Gotcha #1
    in `hub_api/PORTING.md` rules out the raw-SQL upsert Node uses --
    `%s` placeholders 500 under sqlite) -- select-then-branch is the
    portable equivalent, one query pair per setting, matching the
    per-item loop `updateTenantSettings` already runs in Node.
    """
    if not settings:
        raise bad_request("settings must be a non-empty array of {key, value}")

    now = datetime.now(UTC)
    for key, value in settings:
        if not key or not key.strip():
            raise bad_request("Each setting must have a valid string key")

        existing = await async_dal.select_async(
            dal((dal.tenant_settings.tenant_id == tenant_id) & (dal.tenant_settings.key == key))
        )
        if existing:
            await async_dal.update_async(
                (dal.tenant_settings.tenant_id == tenant_id) & (dal.tenant_settings.key == key),
                value=value,
                updated_at=now,
            )
        else:
            await async_dal.insert_async(
                dal.tenant_settings,
                tenant_id=tenant_id,
                key=key,
                value=value,
                created_at=now,
                updated_at=now,
            )


async def get_tenant_communities(
    async_dal: Any, dal: Any, *, tenant_id: int, page: int, limit: int
) -> tuple[list[Any], int, int]:
    """List communities belonging to a tenant, paginated newest-first."""
    page = max(1, page)
    limit = min(100, max(1, limit))
    offset = (page - 1) * limit

    # `id > 0` excludes the tenant-wide sentinel community (id 0, seed_core_bundles.py).
    query = (dal.communities.tenant_id == tenant_id) & (dal.communities.id > 0)
    total = await async_dal.count_async(query)
    rows = await async_dal.select_async(
        dal(query),
        orderby=~dal.communities.created_at,
        limitby=(offset, offset + limit),
    )
    total_pages = math.ceil(total / limit) if limit else 0
    return list(rows), total, total_pages


async def get_tenant_modules(async_dal: Any, dal: Any, *, tenant_id: int) -> tuple[bool, list[Any]]:
    """Return `(all_modules_allowed, modules)` for a tenant.

    `allowed_module_ids IS NULL` means every published module is allowed
    (matches Node's `tenant.allowed_module_ids === null` branch); an empty
    list means none are; a non-empty list scopes to exactly those ids.
    """
    tenant = await get_tenant(async_dal, dal, tenant_id=tenant_id)
    allowed_ids = tenant.allowed_module_ids

    if allowed_ids is None:
        rows = await async_dal.select_async(
            dal(dal.hub_modules.is_published == True),  # noqa: E712 - pydal Field comparison
            orderby=dal.hub_modules.category | dal.hub_modules.display_name,
        )
        return True, list(rows)

    if len(allowed_ids) == 0:
        return False, []

    rows = await async_dal.select_async(
        dal(dal.hub_modules.id.belongs(allowed_ids)),
        orderby=dal.hub_modules.category | dal.hub_modules.display_name,
    )
    return False, list(rows)


async def update_tenant_modules(
    async_dal: Any, dal: Any, *, tenant_id: int, allowed_module_ids: list[int] | None
) -> None:
    """Set the allowed module id list (or `None` for "all modules"). Raises 400 on invalid ids."""
    if allowed_module_ids is not None:
        for module_id in allowed_module_ids:
            if module_id <= 0:
                raise bad_request("Each allowedModuleId must be a positive integer")

    await async_dal.update_async(
        dal.tenants.id == tenant_id,
        allowed_module_ids=allowed_module_ids,
        updated_at=datetime.now(UTC),
    )


async def get_tenant_admins(async_dal: Any, dal: Any, *, tenant_id: int) -> list[Any]:
    """List tenant admins, joined with `hub_users` for display fields.

    Selecting fields from BOTH `tenant_admins` and `hub_users` nests each
    result row under `row.tenant_admins.*`/`row.hub_users.*` (pydal Gotcha
    #6 in `hub_api/PORTING.md` -- selecting from 2+ tables together nests,
    unlike a single-table selection across a join condition).
    """
    query = (dal.tenant_admins.tenant_id == tenant_id) & (
        dal.hub_users.id == dal.tenant_admins.user_id
    )
    rows = await async_dal.select_async(
        dal(query),
        dal.tenant_admins.user_id,
        dal.tenant_admins.role,
        dal.tenant_admins.created_at,
        dal.hub_users.username,
        dal.hub_users.display_name,
        dal.hub_users.email,
        orderby=dal.tenant_admins.role | dal.tenant_admins.created_at,
    )
    return list(rows)


async def add_tenant_admin(
    async_dal: Any, dal: Any, *, tenant_id: int, user_id: int, role: str
) -> None:
    """Grant (or update) a tenant admin role for a user. Raises 400/404 as Node does."""
    if role not in VALID_TENANT_ADMIN_ROLES:
        raise bad_request(f"role must be one of: {', '.join(VALID_TENANT_ADMIN_ROLES)}")

    user_rows = await async_dal.select_async(dal(dal.hub_users.id == user_id))
    if not user_rows:
        raise not_found("User not found")

    existing = await async_dal.select_async(
        dal((dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user_id))
    )
    if existing:
        await async_dal.update_async(
            (dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user_id),
            role=role,
        )
    else:
        await async_dal.insert_async(
            dal.tenant_admins,
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            created_at=datetime.now(UTC),
        )


async def remove_tenant_admin(async_dal: Any, dal: Any, *, tenant_id: int, user_id: int) -> None:
    """Revoke a tenant admin role. Raises 404 if no such admin row exists."""
    deleted = await async_dal.delete_async(
        (dal.tenant_admins.tenant_id == tenant_id) & (dal.tenant_admins.user_id == user_id)
    )
    if not deleted:
        raise not_found("Tenant admin not found")


def require_matching_tenant(url_slug: str, ctx_slug: str) -> None:
    """Raise 403 unless the URL's `tenant_slug` matches the caller's own JWT tenant claim.

    See this module's docstring -- the one enforcement point closing the
    IDOR class Node's `:tenantSlug`-from-URL design is otherwise exposed
    to. Every route in `blueprints/v1/tenant.py` calls this before doing
    anything else.
    """
    if url_slug != ctx_slug:
        raise forbidden("Tenant mismatch")


__all__ = [
    "VALID_TENANT_ADMIN_ROLES",
    "add_tenant_admin",
    "get_tenant",
    "get_tenant_admins",
    "get_tenant_communities",
    "get_tenant_modules",
    "get_tenant_settings",
    "remove_tenant_admin",
    "require_matching_tenant",
    "update_tenant",
    "update_tenant_modules",
    "update_tenant_settings",
]
