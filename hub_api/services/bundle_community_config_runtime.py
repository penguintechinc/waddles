"""Runtime read of a bundle's live per-community config, for the data-plane stages.

The bundle-admin gateway (`bundle_admin_gateway_service`) stores each community's config
append-only in `bundle_community_config`; the highest `version` is live. A bundle invoked for
community C must see that config in its `get-context().config_json`, merged OVER the property
defaults its manifest `community_config_schema` declares -- this module computes that merge
(`BundleConfigService` serves it over gRPC) and publishes the "config changed" version key the
stages poll so an admin's save reaches a running bundle within seconds.

Ids only, no PII. Config bodies are never logged.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from penguin_dal import AsyncDB

from services import bundle_install_dal
from services.bundle_admin_gateway_service import ActiveBundle, load_active_bundle
from services.errors import ApiError

logger = logging.getLogger(__name__)

#: Valkey version-key namespace. Value = latest saved config version (decimal string).
VERSION_KEY_PREFIX: Final = "waddles:bundle_config:v"
VERSION_KEY_TTL_S: Final = 7 * 24 * 3600


def version_key(tenant_id: int, community_id: int, app_id: str) -> str:
    """The Valkey key whose value is the latest saved config version of one scope."""
    return f"{VERSION_KEY_PREFIX}:{tenant_id}:{community_id}:{app_id}"


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    """Merged live config of one `(tenant, community, app)` plus the saved version it reflects."""

    tenant_id: int
    community_id: int
    app_id: str
    version: int
    config: dict[str, Any]


class CommunityNotInTenantError(Exception):
    """The community does not belong to the tenant (a 403 at the transport)."""


class BundleNotActiveError(Exception):
    """The app has no activation for the scope (a NOT_FOUND at the transport)."""


def schema_defaults(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Top-level `properties.*.default` values of a `community_config_schema` (deep-copied)."""
    if not schema:
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    return {
        name: copy.deepcopy(spec["default"])
        for name, spec in props.items()
        if isinstance(spec, dict) and "default" in spec
    }


def merge_config(schema: dict[str, Any] | None, saved: dict[str, Any] | None) -> dict[str, Any]:
    """Schema defaults overlaid with the saved config (a saved key always wins)."""
    merged = schema_defaults(schema)
    merged.update(saved or {})
    return merged


async def community_in_tenant(install_dal: AsyncDB, tenant_id: int, community_id: int) -> bool:
    """True for the tenant-wide scope (0) or a community owned by `tenant_id`."""
    if community_id == 0:
        return True
    rows = await bundle_install_dal.raw_sql_rows(
        install_dal,
        "SELECT 1 AS ok FROM communities WHERE id = :cid AND tenant_id = :tid LIMIT 1",
        {"cid": community_id, "tid": tenant_id},
    )
    return bool(rows)


async def _latest_saved(
    install_dal: AsyncDB, tenant_id: int, community_id: int, app_id: str
) -> tuple[int, dict[str, Any]]:
    """`(version, config)` of the highest saved version, `(0, {})` when never saved."""
    t = install_dal.bundle_community_config
    rows = await install_dal(
        (t.tenant_id == tenant_id) & (t.community_id == community_id) & (t.app_id == app_id)
    ).select(orderby=~t.version, limitby=(0, 1))
    if not rows:
        return 0, {}
    raw = rows[0].config
    if isinstance(raw, str):
        raw = json.loads(raw)
    return int(rows[0].version), dict(raw) if isinstance(raw, dict) else {}


async def get_runtime_config(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int, app_id: str
) -> RuntimeConfig:
    """Merged live config for one scope.

    Raises `CommunityNotInTenantError` / `BundleNotActiveError`; the manifest's
    `community_config_schema` supplies the defaults (a bundle without one gets just what was
    saved, normally `{}`).
    """
    if not await community_in_tenant(install_dal, tenant_id, community_id):
        raise CommunityNotInTenantError
    try:
        bundle: ActiveBundle = await load_active_bundle(
            install_dal, tenant_id=tenant_id, community_id=community_id, app_id=app_id
        )
    except ApiError as exc:
        if exc.status_code == 404:
            raise BundleNotActiveError from exc
        raise
    version, saved = await _latest_saved(install_dal, tenant_id, community_id, app_id)
    return RuntimeConfig(
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        version=version,
        config=merge_config(bundle.manifest.community_config_schema, saved),
    )


async def publish_version(
    valkey: Any, *, tenant_id: int, community_id: int, app_id: str, version: int
) -> bool:
    """Best-effort: bump the scope's version key so stages reload. Never raises.

    A failure only delays propagation to the stage's TTL safety net, so it is logged (ids only)
    and reported by the return value, never surfaced to the admin saving the config.
    """
    try:
        await valkey.set(
            version_key(tenant_id, community_id, app_id), str(version), ex=VERSION_KEY_TTL_S
        )
    except Exception as exc:  # noqa: BLE001 - propagation is best-effort by design
        logger.warning(
            "bundle_config.publish_failed",
            extra={
                "tenant_id": tenant_id,
                "community_id": community_id,
                "app_id": app_id,
                "version": version,
                "error_type": type(exc).__name__,
            },
        )
        return False
    return True
