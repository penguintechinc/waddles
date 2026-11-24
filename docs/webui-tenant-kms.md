# Tenant encryption keys (BYOK) UI

Page: `admin/hub_module/frontend/src/pages/tenant/TenantKms.tsx`; client: `src/services/tenantKmsApi.ts`.
Backend: `hub_api/blueprints/v1/tenant_kms.py` (scope `compliance.kms:admin`, URL slug must equal the JWT tenant).

| Action | Route | Entitlement |
|--------|-------|-------------|
| View config + data-key summary | `GET /api/v1/tenant/<slug>/kms` | none |
| Record key (pending) / replace key | `PUT .../kms` | Enterprise |
| Activate (verify + re-wrap data keys) | `POST .../kms/activate` | Enterprise |
| Remove (re-wrap back to platform key) | `DELETE .../kms` | none |

- `403` with code `EXTERNAL_KMS_NOT_ENTITLED` renders an upgrade prompt, not an error.
- Remove requires typing `REMOVE`; replacing an active key requires typing `ROTATE`.
- Security: no secret inputs exist (the API takes identifiers only). The key identifier is shown masked
  (last 6 characters), inputs are cleared after submit, and no key material is logged or placed in URLs.
- The API has no separate "test" or "rotate" route: rotate = `PUT` a new key then `activate`.

## Rewrap outcome handling

The success banner is shown only when the rewrap summary has `ok: true` and no `failedVersions`. Otherwise (activate returning `ok: false`, or remove returning `409 REWRAP_INCOMPLETE`) the page shows a prominent warning that some data keys are still wrapped by the previous key and the external key must NOT be revoked or deleted yet.
