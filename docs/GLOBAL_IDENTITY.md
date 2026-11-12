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

### The bootstrap admin is a platform account (non-person)

The seeded admin carries `hub_users.is_bootstrap` (migration 0065; a partial unique index allows at
most one). It is **not meant to be a real human's account**: do not register a person's e-mail or
PII on it and do not use it for day-to-day administration. Operators should create named global
admins for people (`POST /api/v1/superadmin/users`, `type: global_admin`) and keep the platform
account as break-glass only.

Enforced in the service layer (every API path), each refusal a typed 403
`PLATFORM_ACCOUNT_PROTECTED` and an audit record (actor, target uuid, action, `denied`, reason
`platform_account`):

| Attempt | Operation |
|---|---|
| `DELETE /superadmin/users/<id>` | `delete` |
| `PUT /superadmin/users/<id>` with `isActive: false`, `DELETE /platform/users/<id>` | `deactivate` |
| `POST /superadmin/users/<id>/super-admin-role` with `grant: false` | `demote` |
| self-service erasure, tenant DSAR erasure, the shared erasure core | `erase` |

The last-admin (409) guards stay in force for person accounts. The API exposes
`isPlatformAccount` on the user DTO; the Users page badges it and disables delete/demote.

## Operational notes

- Sessions issued before `0096` carry tenant `global` in their refresh token; that tenant no longer
  exists, so those users must log in again (fails closed).
- `RUNNER_TENANT_SLUG`, `DEFAULT_TENANT_SLUG` and the data-plane stream scope now default to `default`;
  override env/values that pinned `global`.
