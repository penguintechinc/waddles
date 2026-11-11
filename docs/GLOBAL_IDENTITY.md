# Global identity (system tenant) and the `default` tenant

There is no `hub_users.is_super_admin` flag. Global identity is derived from tenancy
(security.md Reserved Tenants):

| Concept | Definition |
|---|---|
| `system` tenant | Operator plane. Reserved slug; created by alembic `0096` / migration 081 / `global_identity.ensure_system_tenant` |
| Global user | Member of `system` (`tenant_members` row, or any admin row) |
| Global admin (super admin) | Tenant admin of `system` (`tenant_admins` row) |
| `default` tenant | Catch-all tenant (was slug `global` until alembic `0096` renamed it, keeping its id) |

All checks go through `hub_api/services/global_identity.py` (`is_global_admin`, `is_global_user`,
`global_admin_ids`, `grant_global_admin_sync`). Token issuance (`auth_service._scopes_for`) grants
the `global` admin scope bundle and the `super_admin` role claim **iff** the user is a global admin;
it re-reads the database on every mint and refresh. Every state-changing superadmin action also
re-validates the caller against the database (defense in depth). Fail closed: no `system` tenant, a
missing or deactivated user are all "not global".

The API field `isSuperAdmin` (login, `auth/me`, user lists) is unchanged but derived.

## Creating admins

`POST /api/v1/superadmin/users` requires `type`:

| `type` | `tenant` | Result |
|---|---|---|
| `global_admin` | must be omitted | member + admin of `system` |
| `tenant_admin` | tenant UUID (`public_id`) required, must exist and be active, not `system` | member + `tenant-owner` of that tenant |

Plain-user creation is not offered (422). Both paths are subject to the free-tier admin cap. The
response carries `user`, `type` and `tenant` (`id` = public UUID, `slug`, `displayName`).
Revoking the last global admin is refused (409).

## Bootstrap admin

`INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` (migration 081, `config/postgres/seed_admin.sql`,
helm `hub-admin-seed` job) create the initial admin as a member + `tenant-owner` of `system`.
Alembic `0096` moves every pre-existing `is_super_admin` user (and the `is_bootstrap` user) into
`system`, then drops the column; downgrade restores the column from `system` admins.

## Operational notes

- Sessions issued before `0096` carry tenant `global` in their refresh token; that tenant no longer
  exists, so those users must log in again (fails closed).
- `RUNNER_TENANT_SLUG`, `DEFAULT_TENANT_SLUG` and the data-plane stream scope now default to `default`;
  override env/values that pinned `global`.
