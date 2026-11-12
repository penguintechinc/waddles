"""Leaf guard: refuse to make a globally DISABLED bundle available or active.

A super-admin disable (`bundle_global_state_service.disable_service`) sets
`app_catalog.status = 'disabled'`. Every path that would re-expose the bundle
(tenant availability, community / tenant-wide activation) calls this first, so a
disabled bundle cannot be re-entered until it is explicitly re-enabled. Kept as a
dependency-free leaf so both lifecycle services can import it without a cycle.
"""

from __future__ import annotations

from penguin_dal import AsyncDB

from services.errors import ApiError

STATUS_DISABLED = "disabled"


async def assert_bundle_globally_enabled(install_dal: AsyncDB, app_id: str) -> None:
    """Raise a typed 409 `bundle_disabled` when `app_id`'s catalog status is `disabled`."""
    row = (await install_dal(install_dal.app_catalog.app_id == app_id).select()).first()
    if row is not None and row.status == STATUS_DISABLED:
        raise ApiError(
            f"{app_id!r} is globally disabled by a platform administrator",
            409,
            "bundle_disabled",
        )
