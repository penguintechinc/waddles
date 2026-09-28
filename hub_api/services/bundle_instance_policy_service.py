"""Instance-wide permission policy layer (spec: instance policy, above the 3 consent tiers).

A GLOBAL admin (`platform:admin`) can allow/deny an entire permission
*type* -- a catalog id or family, e.g. `net.http.private-ip`,
`storage.objects` -- instance-wide (2026-09-28 decision). This applies to
every bundle regardless of its own per-app approval; it is a platform-
level kill-switch, never a per-bundle/per-admission decision. The layer
sits ABOVE the existing 3 tiers (global catalog approval -> tenant
restriction -> community grant, Sec3):

- A manifest requesting an instance-denied permission is rejected at
  GLOBAL catalog-approval time (`record_permission_requests`).
- A COMMUNITY can never grant an instance-denied permission
  (`grant_community_permissions`), even within an otherwise-approved
  ceiling.
- Flipping an existing `allow` (or unset) to `deny` cascades: every
  currently-active `community_permission_grants` row matching the denied
  type is revoked, each affected `(tenant, community, app)`'s grant
  version is bumped, and invalidation is published -- same all-or-nothing
  transaction shape as `bundle_permission_service.deactivate_permission()`.

Seeded default (migration 0033): `net.http.private-ip` = `deny` (opt-in
only) -- the one family capable of naming this platform's own internal
network. `_DEFAULT_DENIED_FAMILIES` mirrors that seed so an empty/fresh
policy table (e.g. a test fixture that skips the seed INSERT) still
fails closed on this specific family.

An optional `param_scope` can narrow a policy to one parameter value
(e.g. a single CIDR) -- kept as a secondary, best-effort feature; the
primary, and only mandatory, key is the permission *type* itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from penguin_dal import AsyncDB
from sqlalchemy import select

from services import bundle_audit
from services.bundle_permission_catalog import permission_family
from services.bundle_permission_service import _publish_invalidation, _write_grant_version_row

logger = logging.getLogger(__name__)

Action = Literal["allow", "deny"]

#: Fail-closed static mirror of migration 0033's seed row -- authoritative
#: only when the table has no explicit row for the family yet.
_DEFAULT_DENIED_FAMILIES = frozenset({"net.http.private-ip"})


@dataclass(slots=True, frozen=True)
class InstancePolicy:
    """One row of `instance_permission_policies`."""

    permission_key: str
    param_scope: str | None
    action: Action


def _param_scope_clause(table: Any, param_scope: str | None) -> Any:
    """`WHERE param_scope = ...` (or `IS NULL`) -- SQLAlchemy needs `.is_(None)`, not `== None`."""
    if param_scope is None:
        return table.c.param_scope.is_(None)
    return table.c.param_scope == param_scope


async def get_policy_action(
    install_dal: AsyncDB, *, permission_id: str, param_value: str | None = None
) -> Action:
    """Resolve the effective instance-policy action for `permission_id`.

    Priority: an exact `(family, param_value)`-scoped row, then a
    `(family, NULL)` (whole-type) row, then the fail-closed static
    default, else `allow`.
    """
    family = permission_family(permission_id)
    table = install_dal.metadata.tables["instance_permission_policies"]
    async with install_dal.engine.connect() as conn:
        if param_value is not None:
            scoped = (
                await conn.execute(
                    select(table.c.action).where(
                        (table.c.permission_key == family) & (table.c.param_scope == param_value)
                    )
                )
            ).scalar_one_or_none()
            if scoped is not None:
                return scoped  # type: ignore[no-any-return]
        unscoped = (
            await conn.execute(
                select(table.c.action).where(
                    (table.c.permission_key == family) & table.c.param_scope.is_(None)
                )
            )
        ).scalar_one_or_none()
        if unscoped is not None:
            return unscoped  # type: ignore[no-any-return]
    return "deny" if family in _DEFAULT_DENIED_FAMILIES else "allow"


async def is_instance_denied(
    install_dal: AsyncDB, *, permission_id: str, param_value: str | None = None
) -> bool:
    """`True` iff `permission_id` (or its family) is instance-denied right now."""
    action = await get_policy_action(
        install_dal, permission_id=permission_id, param_value=param_value
    )
    return action == "deny"


async def list_policies(install_dal: AsyncDB) -> tuple[InstancePolicy, ...]:
    """Every explicit instance-policy row (does not include the unseeded static default)."""
    table = install_dal.metadata.tables["instance_permission_policies"]
    async with install_dal.engine.connect() as conn:
        rows = (
            await conn.execute(select(table.c.permission_key, table.c.param_scope, table.c.action))
        ).all()
    return tuple(
        InstancePolicy(permission_key=r.permission_key, param_scope=r.param_scope, action=r.action)
        for r in rows
    )


async def set_instance_policy(
    install_dal: AsyncDB,
    *,
    permission_key: str,
    action: Action,
    param_scope: str | None = None,
    set_by: int | None,
    valkey_client: Any | None = None,
) -> int:
    """GLOBAL-admin-only: upsert one instance-policy row; a new `deny` cascades revocation.

    Returns the number of `community_permission_grants` rows the cascade
    revoked (always 0 for an `allow`, or a `deny` that was already in
    effect). The upsert, the cascade revoke, every affected grant-version
    bump, and the audit-trail insert all commit as ONE transaction;
    invalidation is published only after commit (same posture as
    `bundle_permission_service.deactivate_permission()`).
    """
    policies = install_dal.metadata.tables["instance_permission_policies"]
    grants = install_dal.metadata.tables["community_permission_grants"]
    versions = install_dal.metadata.tables["app_permission_grant_versions"]
    audit_table = install_dal.metadata.tables["instance_permission_policy_audit"]
    now = datetime.now(UTC)
    revoked_count = 0
    affected: set[tuple[int, int, str]] = set()
    published: list[tuple[int, int, str, int]] = []

    async with install_dal.engine.begin() as conn:
        scope_clause = _param_scope_clause(policies, param_scope)
        existing = (
            await conn.execute(
                select(policies.c.action).where(
                    (policies.c.permission_key == permission_key) & scope_clause
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            await conn.execute(
                policies.insert().values(
                    permission_key=permission_key,
                    param_scope=param_scope,
                    action=action,
                    set_by=set_by,
                    set_at=now,
                )
            )
        else:
            await conn.execute(
                policies.update()
                .where((policies.c.permission_key == permission_key) & scope_clause)
                .values(action=action, set_by=set_by, set_at=now)
            )

        if action == "deny" and existing != "deny":
            # A LIKE prefix match covers both a static id (family == the
            # whole permission_id) and a parameterized family
            # (`net.http.private-ip` matching `net.http.private-ip:10.0.0.0/16`).
            like_pattern = f"{permission_key}:%"
            grant_rows = (
                await conn.execute(
                    select(
                        grants.c.tenant_id,
                        grants.c.community_id,
                        grants.c.app_id,
                        grants.c.permission_id,
                    ).where(
                        (
                            grants.c.permission_id.like(like_pattern)
                            | (grants.c.permission_id == permission_key)
                        )
                        & grants.c.revoked_at.is_(None)
                    )
                )
            ).all()
            for row in grant_rows:
                await conn.execute(
                    grants.update()
                    .where(
                        (grants.c.community_id == row.community_id)
                        & (grants.c.app_id == row.app_id)
                        & (grants.c.permission_id == row.permission_id)
                    )
                    .values(revoked_by=set_by, revoked_at=now)
                )
                revoked_count += 1
                affected.add((row.tenant_id, row.community_id, row.app_id))

            for tenant_id, community_id, app_id in affected:
                remaining_rows = await conn.execute(
                    select(grants.c.permission_id).where(
                        (grants.c.community_id == community_id)
                        & (grants.c.app_id == app_id)
                        & grants.c.revoked_at.is_(None)
                    )
                )
                remaining = frozenset(r.permission_id for r in remaining_rows)
                new_version = await _write_grant_version_row(
                    conn,
                    versions,
                    tenant_id=tenant_id,
                    community_id=community_id,
                    app_id=app_id,
                    version="*",
                    permission_ids=remaining,
                )
                published.append((tenant_id, community_id, app_id, new_version))

        await conn.execute(
            audit_table.insert().values(
                permission_key=permission_key,
                param_scope=param_scope,
                previous_action=existing,
                new_action=action,
                cascaded_revocations=revoked_count,
                set_by=set_by,
                set_at=now,
            )
        )

    for tenant_id, community_id, app_id, grant_version in published:
        await _publish_invalidation(
            tenant_id=tenant_id,
            community_id=community_id,
            app_id=app_id,
            version="*",
            grant_version=grant_version,
            valkey_client=valkey_client,
        )
    await bundle_audit.record(
        install_dal,
        actor_id=set_by,
        action="instance_permission_policy_set",
        target_type="instance_permission_policies",
        target_id=permission_key,
        details={
            "param_scope": param_scope,
            "action": action,
            "cascaded_revocations": revoked_count,
        },
    )
    return revoked_count


__all__ = [
    "InstancePolicy",
    "get_policy_action",
    "is_instance_denied",
    "list_policies",
    "set_instance_policy",
]
