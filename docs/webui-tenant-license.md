# Web UI: tenant license and operator pools

## Tenant page `/tenant/:tenantSlug/license`
- Nav: "License" in the Tenant Admin group (needs `tenant:admin` + session tenant slug).
- `GET/PUT/DELETE /api/v1/tenant/<slug>/license` via `services/tenantLicenseApi.ts`.
- The key is write-only. The page shows only `configured`, `fingerprint`, `setAt`.
- Key field: `type=password`, `autocomplete=off`, `spellcheck=false`; value is trimmed, validated against the server rule (16-256 chars of letters, digits, `. _ : -`), cleared before the request is sent and again on error; never logged, never in URLs.
- Remove requires typing `REMOVE`.
- States: loading, not configured ("No license key: Free tier limits apply; team pools unavailable"), configured, 403, error with Retry.
- Honest note: license-server team-pool support is pending (`docs/license-pool.md`); a configured key can still show "pool unavailable" on the Community tiers page.

## Operator pools (Super Admin > Tenants > "License pools")
- Component `components/superadmin/TenantLicensePools.tsx`; flag gate unchanged (`waddles.webui.super_tenants`).
- `GET /api/v1/superadmin/tenants/<id>/license-pools` returns `bypassDomain`; editing is disabled when it is false or when a PUT returns 409 `POOL_OPERATOR_SET_NOT_ALLOWED`.
- `PUT .../license-pools/<2|3>` body `{limit}`: integer >= -1; "Unlimited" = -1; "Not licensed" = null.
- The pools response has no usage counts; usage lives on the tenant Community tiers page.
