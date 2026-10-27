# Whitelabel Branding (Professional)

Per-tenant login-page branding is a **Professional-tier** feature. Free tenants always get the stock Waddles branding; Professional and Enterprise tenants get their own.

| | |
|---|---|
| Feature contract | `tenancy.whitelabel` (`libs/core_platform_module/features.py`) |
| Flag | `waddles.tenancy.whitelabel` (PostHog) |
| Minimum tier | Professional |
| Gate | Two-gate entitlement (PostHog flag **and** license tier) — **fails closed**: either gate off or unreachable resolves to default branding / `402`, never the reverse |

## What counts as branding

Stored on the tenant row: `tenants.logo_url`, and `tenants.config.theme` / `tenants.config.welcomeMessage`. The tenant display name and slug are identity, not branding, and are always returned.

## Where it is gated

| Surface | Behavior |
|---|---|
| `GET /api/v1/auth/tenant/<slug>` (pre-auth; the login page's data source) | **Application gate.** Entitled → sanitized custom `logoUrl` / `config.theme` / `config.welcomeMessage` and `whitelabeled: true`. Not entitled → those fields are `null`, `whitelabeled: false`. The gate is evaluated only when custom branding is actually stored. |
| `PUT /api/v1/tenant/<slug>` | **Write gate.** *Setting* a new non-empty logo/theme/welcome message without entitlement → `402 FEATURE_NOT_ENABLED`. Re-sending the stored value, clearing branding, and any non-branding edit are never gated, so a downgraded tenant can still remove branding and edit other settings. |
| `LoginPage.jsx` | Applies custom logo / name / welcome message **only** when the response says `whitelabeled === true`; any error keeps stock branding. The client never decides entitlement. |

## Sanitization

Custom values are validated when served: the logo must be `https://…` or a root-relative `/path` (never `http:`, `javascript:`, `data:`, or protocol-relative `//host`); text is stripped of control characters and length-capped (theme 100, welcome message 500, logo URL 2048). An invalid field falls back to its default; if nothing valid remains, `whitelabeled` is `false`.

## Code

- `hub_api/services/branding_service.py` — gate + sanitization + write-change detection
- `hub_api/blueprints/v1/auth.py::tenant_login_info`, `blueprints/v1/tenant.py::update_tenant` — the two call sites
- `admin/hub_module/frontend/src/pages/auth/tenantBranding.js` — client resolver
- Tests: `hub_api/tests/test_branding_whitelabel.py`, `libs/core_platform_module/tests/test_whitelabel_bulk_dsar_contracts.py`, `admin/hub_module/frontend/src/pages/auth/__tests__/LoginPage.branding.test.jsx`
