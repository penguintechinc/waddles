# Tenant management in hub-webui

The webui is a thin presentation layer: every call goes to hub-api; authz is enforced server-side.

## Super admin: Tenants

- Sidebar: Super Admin > Tenants -> `/superadmin/tenants` (`SuperAdminTenants.tsx`).
- API: `/api/v1/superadmin/tenants` (scope `tenants:admin`): list/search/paginate, create, edit, deactivate (type the slug to confirm; the default tenant 0 cannot be deactivated).
- Gated by feature flag `waddles.webui.super_tenants` (PostHog -> ENV -> default). The Helm chart baselines it via `config.webuiSuperTenantsEnabled` -> `FLAG_WADDLES_WEBUI_SUPER_TENANTS` (chart default `true`; PostHog still overrides). With the flag off the page shows "Tenant management is not yet available."

## Tenant admin: Tenant Admin section

- Shown in the sidebar when the session holds scope `tenant:admin` AND the auth context carries the user's tenant slug (`useAuth().hasScope`, `useAuth().tenantSlug`; never role names, never user input).
- Entries: Overview, Settings, Admins, Communities, Bundles, Reputation, Discord App, Data Privacy, Ingest sources (all `tenant:admin`) and Encryption keys (BYOK) (scope `compliance.kms:admin`) under `/tenant/<slug>/...`. A user holding only `compliance.kms:admin` (plus a slug) sees the group with just the KMS item.
- Settings (`TenantSettings.jsx`): key/value editor over `GET/PUT /api/v1/tenant/<slug>/settings`.

## Requires from hub-api

`GET /api/v1/auth/me` returns `user.scopes: string[]` (the validated JWT `scope` claim) and `user.tenantSlug: string | null` (the active `tenants` row matching the JWT `tenant` claim, resolved server-side) for the Tenant Admin section to appear. A token with no resolvable tenant yields `scopes` as granted and `tenantSlug: null`, so the section stays hidden.

## Pages

| Route | Page | Scope | Doc |
|-------|------|-------|-----|
| `/tenant/<slug>/privacy` | Data Privacy (`TenantPrivacy.jsx`) | `tenant:admin` | [webui-tenant-privacy.md](webui-tenant-privacy.md) |
| `/tenant/<slug>/kms` | Encryption keys (BYOK) (`TenantKms.tsx`) | `compliance.kms:admin` | [webui-tenant-kms.md](webui-tenant-kms.md) |
| `/tenant/<slug>/ingest-sources` | Ingest sources (`TenantIngestSources.jsx`) | `tenant:admin` | [webui-tenant-ingest-sources.md](webui-tenant-ingest-sources.md) |
