"""At most ONE `provides: reputation-logic` app bundle per TENANT.

The reputation scoring logic (event types -> deltas, weights, cooldowns) is a swappable app
bundle; every community of a tenant uses the same one, chosen by the tenant admin through tenant
bundle availability. Two logic bundles in one tenant would double-score every event, so making a
second one available or active in the tenant is a typed 409 `reputation_logic_conflict` naming
the one already there.

(The reputation SERVICE bundle is a different thing: a global singleton, never a per-tenant
choice, and not governed here.)

The manifest field is `provides:` -- a string or a list of strings -- read from the raw stored
`manifest_json` (no schema change); the value is `reputation-logic`.
"""

from __future__ import annotations

from typing import Any

from penguin_dal import AsyncDB

from services.errors import ApiError

REPUTATION_LOGIC_CAPABILITY = "reputation-logic"
REPUTATION_LOGIC_CONFLICT_CODE = "reputation_logic_conflict"

__all__ = [
    "REPUTATION_LOGIC_CAPABILITY",
    "REPUTATION_LOGIC_CONFLICT_CODE",
    "provides_capabilities",
    "require_single_logic_active",
    "require_single_logic_available",
]


def provides_capabilities(manifest_json: Any) -> frozenset[str]:
    """The `provides:` values a raw manifest declares (a string or a list of strings)."""
    raw = manifest_json.get("provides") if isinstance(manifest_json, dict) else None
    if isinstance(raw, str):
        return frozenset({raw})
    if isinstance(raw, list):
        return frozenset(v for v in raw if isinstance(v, str))
    return frozenset()


def _conflict(active_app_id: str, where: str) -> ApiError:
    """The typed 409 naming the logic bundle that already holds the tenant's slot."""
    return ApiError(
        f"{active_app_id!r} is already this tenant's reputation logic bundle ({where}); "
        "remove it before adding another",
        409,
        REPUTATION_LOGIC_CONFLICT_CODE,
        {"active_app_id": active_app_id},
    )


async def _version_provides_logic(install_dal: AsyncDB, *, app_id: str, version: str) -> bool:
    """Whether the stored manifest of `app_id@version` declares reputation-logic."""
    upload = (
        await install_dal(
            (install_dal.app_version_uploads.app_id == app_id)
            & (install_dal.app_version_uploads.version == version)
        ).select()
    ).first()
    return upload is not None and REPUTATION_LOGIC_CAPABILITY in provides_capabilities(
        upload.manifest_json
    )


async def require_single_logic_active(
    install_dal: AsyncDB, *, tenant_id: int, app_id: str, manifest_json: Any
) -> None:
    """409 if `app_id` is a logic bundle and ANOTHER logic bundle is active anywhere in the tenant.

    Covers tenant-wide and every community of the tenant; the same app (an upgrade / another
    community of the same app) never conflicts with itself.
    """
    if REPUTATION_LOGIC_CAPABILITY not in provides_capabilities(manifest_json):
        return
    active = install_dal.app_active_versions
    rows = await install_dal((active.tenant_id == tenant_id) & (active.app_id != app_id)).select()
    for row in rows:
        version_row = (
            await install_dal(install_dal.app_versions.id == int(row.version_id)).select()
        ).first()
        if version_row is not None and await _version_provides_logic(
            install_dal, app_id=str(row.app_id), version=str(version_row.version)
        ):
            raise _conflict(str(row.app_id), "active")


async def require_single_logic_available(
    install_dal: AsyncDB, *, tenant_id: int, app_id: str
) -> None:
    """409 if `app_id` is a logic bundle and ANOTHER one is already available to the tenant."""
    install = (
        await install_dal(
            (install_dal.app_global_installs.app_id == app_id)
            & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
            & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
        ).select()
    ).first()
    if install is None or not await _version_provides_logic(
        install_dal, app_id=app_id, version=str(install.version)
    ):
        return
    avail = install_dal.bundle_tenant_availability
    rows = await install_dal(
        (avail.tenant_id == tenant_id) & (avail.app_id != app_id) & (avail.available == True)  # noqa: E712
    ).select()
    for row in rows:
        other = (
            await install_dal(
                (install_dal.app_global_installs.app_id == row.app_id)
                & (install_dal.app_global_installs.superseded_by == None)  # noqa: E711
                & (install_dal.app_global_installs.revoked_at == None)  # noqa: E711
            ).select()
        ).first()
        if other is not None and await _version_provides_logic(
            install_dal, app_id=str(row.app_id), version=str(other.version)
        ):
            raise _conflict(str(row.app_id), "available")
