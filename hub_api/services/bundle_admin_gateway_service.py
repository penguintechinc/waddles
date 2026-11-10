"""Generic bundle-admin gateway: per-community config + admin-tool invocation + data views.

hub-api is the "site foreman": every app bundle's admin UI talks to these generic functions, so no
consumer feature lives in hub-api (docs/BUNDLE_ADMIN_GATEWAY.md). Everything is driven by the
ACTIVE bundle's manifest (`community_config_schema`, `admin_tools`, the target's `exported_tools`):

* config: validated against `community_config_schema`, stored versioned/append-only
  (`bundle_community_config`).
* admin tools: the caller's community scope is checked against the scope the manifest declares,
  args are validated against the target tool's `input_schema`, the call goes over the signed
  :mod:`bundle_tool_transport`, and the output is validated against `output_schema` before it is
  returned. Intent is audited BEFORE the call (fail closed), the outcome after.
* data views: `kind: read` admin tools (target must be `access: read`), paginated via `limit` /
  `cursor` when the tool declares them.

PII-free: logs and audit details carry ids, tool name, version and codes -- never config or args.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator
from penguin_dal import AsyncDB
from sqlalchemy.exc import IntegrityError

from services import bundle_audit, bundle_install_dal
from services.bundle_approval_service import _reparse_trusted
from services.bundle_manifest_v2 import (
    AdminTool,
    BundleManifestV2,
    ExportedTool,
)
from services.bundle_tool_transport import (
    ToolCall,
    ToolTransport,
    ToolTransportError,
    canonical_args,
)
from services.community_authz import CommunityMembership
from services.errors import ApiError, forbidden

logger = logging.getLogger(__name__)

#: Community scope -> hub-api community-role scopes that satisfy it (admin is a superset of mod).
SCOPE_SATISFIED_BY: dict[str, frozenset[str]] = {
    "community:moderate": frozenset({"community:manage_channels", "community:manage_members"}),
    "community:admin": frozenset({"community:manage_members"}),
}
MAX_CONFIG_BYTES = 64 * 1024
MAX_ARGS_BYTES = 64 * 1024
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 100
MAX_ERRORS_REPORTED = 20

AUDIT_CONFIG_SAVED = "bundle_admin.config_saved"
AUDIT_TOOL_INVOKED = "bundle_admin.tool_invoked"


@dataclass(slots=True, frozen=True)
class ActiveBundle:
    """The bundle version active for a community, with its validated manifest."""

    app_id: str
    version: str
    version_id: int
    manifest: BundleManifestV2


@dataclass(slots=True, frozen=True)
class ConfigSnapshot:
    """One saved config version (`version == 0` means never saved: `config` is empty)."""

    version: int
    config: dict[str, Any]
    updated_by: int | None
    created_at: datetime | None


@dataclass(slots=True, frozen=True)
class ConfigHistoryEntry:
    """Metadata of one saved config version (never the config body)."""

    version: int
    updated_by: int | None
    created_at: datetime | None


@dataclass(slots=True, frozen=True)
class ToolResult:
    """A validated tool output plus pass-through paging info for data views."""

    result: dict[str, Any]
    limit: int | None
    next_cursor: str | None


def _err(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(message=message, status_code=status, code=code, details=details)


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise a JSON column (dict on Postgres via the dal, str on some drivers)."""
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


async def load_active_bundle(
    install_dal: AsyncDB, *, tenant_id: int, community_id: int, app_id: str
) -> ActiveBundle:
    """Resolve the manifest of `app_id` as activated for this community (or tenant-wide).

    A community-level activation wins over the tenant-wide (`community_id = 0`) one. Raises a 404
    `bundle_not_active` when the bundle is not activated for the caller's scope -- the gateway
    never serves a bundle the community has not enabled.
    """
    rows = await bundle_install_dal.raw_sql_rows(
        install_dal,
        """
        SELECT v.id AS version_id, v.version AS version, u.manifest_json AS manifest_json
        FROM app_active_versions a
        JOIN app_versions v ON v.id = a.version_id
        JOIN app_version_uploads u ON u.app_version_id = v.id
        WHERE a.app_id = :app_id AND a.tenant_id = :tenant_id
          AND a.community_id IN (:community_id, 0)
        ORDER BY a.community_id DESC
        LIMIT 1
        """,
        {"app_id": app_id, "tenant_id": tenant_id, "community_id": community_id},
    )
    if not rows:
        raise _err(404, "bundle_not_active", "bundle is not active for this community")
    row = rows.first()
    if row is None:  # unreachable: `rows` is non-empty -- narrows the type
        raise _err(404, "bundle_not_active", "bundle is not active for this community")
    manifest = _reparse_trusted(_as_dict(row["manifest_json"]))
    return ActiveBundle(
        app_id=app_id,
        version=str(row["version"]),
        version_id=int(row["version_id"]),
        manifest=manifest,
    )


def caller_holds(membership: CommunityMembership, scope: str) -> bool:
    """True when `membership` satisfies the manifest-declared community `scope` (bypass = yes)."""
    if membership.bypass:
        return True
    return bool(membership.scopes & SCOPE_SATISFIED_BY.get(scope, frozenset()))


def caller_scope(membership: CommunityMembership) -> str | None:
    """The highest gateway scope the caller holds (`community:admin` > `community:moderate`)."""
    for scope in ("community:admin", "community:moderate"):
        if caller_holds(membership, scope):
            return scope
    return None


def _schema_errors(schema: dict[str, Any], instance: Any) -> list[dict[str, str]]:
    """Value-free validation errors: `{path, rule}` only (an echoed value could be sensitive)."""
    validator = Draft202012Validator(schema)
    found = sorted(validator.iter_errors(instance), key=lambda e: e.json_path)
    return [{"path": e.json_path, "rule": str(e.validator)} for e in found[:MAX_ERRORS_REPORTED]]


def _require_scope(membership: CommunityMembership, scope: str) -> None:
    if not caller_holds(membership, scope):
        raise forbidden(f"{scope} required")


def _find_tool(bundle: ActiveBundle, name: str, kind: str) -> AdminTool:
    for tool in bundle.manifest.admin_tools:
        if tool.name == name and tool.kind == kind:
            return tool
    raise _err(404, "admin_tool_not_found", f"no {kind} admin tool {name!r} on this bundle")


# --------------------------------------------------------------------------- config


def describe(bundle: ActiveBundle, membership: CommunityMembership) -> dict[str, Any]:
    """Descriptor the admin UI renders from: config schema + the tools the caller may use."""
    m = bundle.manifest
    return {
        "appId": bundle.app_id,
        "version": bundle.version,
        "configSchema": m.community_config_schema,
        "tools": [
            {
                "name": t.name,
                "kind": t.kind,
                "scope": t.scope,
                "permitted": caller_holds(membership, t.scope),
                "targetAppId": t.target_app_id,
            }
            for t in m.admin_tools
        ],
    }


async def get_config(
    install_dal: AsyncDB,
    bundle: ActiveBundle,
    membership: CommunityMembership,
    *,
    tenant_id: int,
    community_id: int,
) -> ConfigSnapshot:
    """Latest saved config for the community (moderator or admin); version 0 if never saved."""
    _require_scope(membership, "community:moderate")
    if bundle.manifest.community_config_schema is None:
        raise _err(404, "bundle_has_no_config", "bundle declares no community_config_schema")
    t = install_dal.bundle_community_config
    rows = await install_dal(
        (t.tenant_id == tenant_id) & (t.community_id == community_id) & (t.app_id == bundle.app_id)
    ).select(orderby=~t.version, limitby=(0, 1))
    if not rows:
        return ConfigSnapshot(version=0, config={}, updated_by=None, created_at=None)
    row = rows[0]
    return ConfigSnapshot(
        version=int(row.version),
        config=_as_dict(row.config),
        updated_by=row.updated_by,
        created_at=row.created_at,
    )


async def list_config_history(
    install_dal: AsyncDB,
    bundle: ActiveBundle,
    membership: CommunityMembership,
    *,
    tenant_id: int,
    community_id: int,
    limit: int,
) -> list[ConfigHistoryEntry]:
    """Newest-first saved versions (metadata only), bounded to `limit` <= 100."""
    _require_scope(membership, "community:moderate")
    limit = min(MAX_PAGE_LIMIT, max(1, limit))
    t = install_dal.bundle_community_config
    rows = await install_dal(
        (t.tenant_id == tenant_id) & (t.community_id == community_id) & (t.app_id == bundle.app_id)
    ).select(orderby=~t.version, limitby=(0, limit))
    return [ConfigHistoryEntry(int(r.version), r.updated_by, r.created_at) for r in rows]


async def save_config(
    install_dal: AsyncDB,
    bundle: ActiveBundle,
    membership: CommunityMembership,
    *,
    tenant_id: int,
    community_id: int,
    actor_id: int,
    config: dict[str, Any],
    expected_version: int,
) -> ConfigSnapshot:
    """Validate `config` against the manifest schema and append `expected_version + 1`.

    Community-admin only. 422 `config_invalid` (value-free `{path, rule}` errors), 409
    `config_version_conflict` when another save landed first (the UNIQUE constraint closes the
    read-then-insert race). Intent is audited before the write (fail closed), the result after.
    """
    _require_scope(membership, "community:admin")
    schema = bundle.manifest.community_config_schema
    if schema is None:
        raise _err(404, "bundle_has_no_config", "bundle declares no community_config_schema")
    if len(json.dumps(config, separators=(",", ":"))) > MAX_CONFIG_BYTES:
        raise _err(413, "config_too_large", f"config exceeds {MAX_CONFIG_BYTES} bytes")
    errors = _schema_errors(schema, config)
    if errors:
        raise _err(
            422, "config_invalid", "config does not match the bundle schema", {"errors": errors}
        )

    current = await get_config(
        install_dal, bundle, membership, tenant_id=tenant_id, community_id=community_id
    )
    if current.version != expected_version:
        raise _err(
            409,
            "config_version_conflict",
            "config changed since it was loaded",
            {"currentVersion": current.version},
        )
    base = {
        "app_id": bundle.app_id,
        "community_id": community_id,
        "expected_version": expected_version,
    }
    await bundle_audit.record(
        install_dal,
        actor_id=actor_id,
        action=AUDIT_CONFIG_SAVED,
        target_type="bundle_community_config",
        target_id=bundle.app_id,
        details={**base, "phase": "attempt"},
        tenant_id=tenant_id,
    )
    schema_sha = hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    new_version = expected_version + 1
    try:
        await install_dal.bundle_community_config.async_insert(
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=bundle.app_id,
            version=new_version,
            config=config,
            schema_sha256=schema_sha,
            updated_by=actor_id,
        )
    except IntegrityError as exc:
        await bundle_audit.try_record(
            install_dal,
            actor_id=actor_id,
            action=AUDIT_CONFIG_SAVED,
            target_type="bundle_community_config",
            target_id=bundle.app_id,
            details={**base, "phase": "result", "outcome": "conflict"},
            tenant_id=tenant_id,
        )
        raise _err(409, "config_version_conflict", "config changed since it was loaded") from exc
    await bundle_audit.try_record(
        install_dal,
        actor_id=actor_id,
        action=AUDIT_CONFIG_SAVED,
        target_type="bundle_community_config",
        target_id=bundle.app_id,
        details={**base, "phase": "result", "outcome": "ok", "version": new_version},
        tenant_id=tenant_id,
    )
    return await get_config(
        install_dal, bundle, membership, tenant_id=tenant_id, community_id=community_id
    )


# --------------------------------------------------------------------------- tools


async def _resolve_tool(
    install_dal: AsyncDB,
    bundle: ActiveBundle,
    admin: AdminTool,
    *,
    tenant_id: int,
    community_id: int,
) -> ExportedTool:
    """The target bundle's exported tool an admin tool points at (target must be active too)."""
    if admin.target_app_id == bundle.app_id:
        target = bundle.manifest
    else:
        target = (
            await load_active_bundle(
                install_dal,
                tenant_id=tenant_id,
                community_id=community_id,
                app_id=admin.target_app_id,
            )
        ).manifest
    for tool in target.exported_tools:
        if tool.name == admin.tool:
            return tool
    raise _err(502, "admin_tool_target_missing", "the target bundle no longer exports this tool")


async def _invoke(
    install_dal: AsyncDB,
    transport: ToolTransport,
    bundle: ActiveBundle,
    admin: AdminTool,
    membership: CommunityMembership,
    *,
    tenant_id: int,
    community_id: int,
    actor_id: int,
    args: dict[str, Any],
    read_only: bool,
) -> dict[str, Any]:
    """Shared authz -> validate -> audit -> call -> validate-output pipeline."""
    _require_scope(membership, admin.scope)
    actor_scope = caller_scope(membership)
    if actor_scope is None:  # unreachable: `_require_scope` above guarantees a scope
        raise forbidden(f"{admin.scope} required")
    tool = await _resolve_tool(
        install_dal, bundle, admin, tenant_id=tenant_id, community_id=community_id
    )
    if read_only and tool.access != "read":
        raise _err(502, "admin_tool_not_read", "a data view must target a read-only tool")
    args_json = canonical_args(args)
    if len(args_json) > MAX_ARGS_BYTES:
        raise _err(413, "args_too_large", f"args exceed {MAX_ARGS_BYTES} bytes")
    errors = _schema_errors(tool.input_schema, args)
    if errors:
        raise _err(422, "args_invalid", "args do not match the tool schema", {"errors": errors})

    base = {
        "app_id": bundle.app_id,
        "tool": admin.name,
        "community_id": community_id,
        "access": tool.access,
    }
    audited = tool.access == "write" or tool.risk == "dangerous"
    if audited:
        await bundle_audit.record(
            install_dal,
            actor_id=actor_id,
            action=AUDIT_TOOL_INVOKED,
            target_type="bundle_tool",
            target_id=bundle.app_id,
            details={**base, "phase": "attempt"},
            tenant_id=tenant_id,
        )
    call = ToolCall(
        tenant_id=tenant_id,
        community_id=community_id,
        actor_user_id=actor_id,
        actor_scope=actor_scope,
        caller_app_id=bundle.app_id,
        target_app_id=admin.target_app_id,
        tool=admin.tool,
        args_json=args_json,
    )
    outcome = "ok"
    try:
        result = await transport.invoke(call)
        out_errors = _schema_errors(tool.output_schema, result)
        if out_errors:
            outcome = "bad_output"
            logger.error(
                "bundle_admin.bad_output app_id=%s tool=%s", admin.target_app_id, admin.tool
            )
            raise _err(502, "bundle_tool_bad_output", "the bundle tool returned an invalid result")
        return result
    except ToolTransportError as exc:
        outcome = exc.code
        raise _err(exc.status, exc.code, exc.message) from exc
    finally:
        if audited:
            await bundle_audit.try_record(
                install_dal,
                actor_id=actor_id,
                action=AUDIT_TOOL_INVOKED,
                target_type="bundle_tool",
                target_id=bundle.app_id,
                details={**base, "phase": "result", "outcome": outcome},
                tenant_id=tenant_id,
            )


async def invoke_admin_action(
    install_dal: AsyncDB,
    transport: ToolTransport,
    bundle: ActiveBundle,
    membership: CommunityMembership,
    *,
    tool_name: str,
    tenant_id: int,
    community_id: int,
    actor_id: int,
    args: dict[str, Any],
) -> ToolResult:
    """Invoke a manifest-declared `kind: action` admin tool for the caller's community."""
    admin = _find_tool(bundle, tool_name, "action")
    result = await _invoke(
        install_dal,
        transport,
        bundle,
        admin,
        membership,
        tenant_id=tenant_id,
        community_id=community_id,
        actor_id=actor_id,
        args=args,
        read_only=False,
    )
    return ToolResult(result=result, limit=None, next_cursor=None)


async def read_data_view(
    install_dal: AsyncDB,
    transport: ToolTransport,
    bundle: ActiveBundle,
    membership: CommunityMembership,
    *,
    view_name: str,
    tenant_id: int,
    community_id: int,
    actor_id: int,
    args: dict[str, Any],
    limit: int | None,
    cursor: str | None,
) -> ToolResult:
    """Invoke a `kind: read` admin tool as a paginated data view.

    `limit` (clamped to 1..100, default 25) and `cursor` are forwarded only when the target
    tool's `input_schema` declares them; the tool's own `next_cursor` output is passed through.
    """
    admin = _find_tool(bundle, view_name, "read")
    props = _props(
        await _resolve_tool(
            install_dal, bundle, admin, tenant_id=tenant_id, community_id=community_id
        )
    )
    call_args = dict(args)
    used_limit: int | None = None
    if "limit" in props:
        used_limit = min(MAX_PAGE_LIMIT, max(1, limit if limit is not None else DEFAULT_PAGE_LIMIT))
        call_args["limit"] = used_limit
    if cursor is not None and "cursor" in props:
        call_args["cursor"] = cursor
    result = await _invoke(
        install_dal,
        transport,
        bundle,
        admin,
        membership,
        tenant_id=tenant_id,
        community_id=community_id,
        actor_id=actor_id,
        args=call_args,
        read_only=True,
    )
    next_cursor = result.get("next_cursor")
    return ToolResult(
        result=result,
        limit=used_limit,
        next_cursor=next_cursor if isinstance(next_cursor, str) else None,
    )


def _props(tool: ExportedTool) -> dict[str, Any]:
    props = tool.input_schema.get("properties")
    return props if isinstance(props, dict) else {}
