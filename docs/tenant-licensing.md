# Per-tenant licensing (issue #901, piece F)

Each tenant can carry its OWN license key. The key is the credential the license-pool provider
(`libs/flask_core/flask_core/license_pool.py`, see `docs/license-pool.md`) POSTs to the license
server to learn that tenant's team-axis pool (the single Premium slot pool).

## Storage

`tenant_license_keys` (alembic `0127_tenant_license_key`), one row per tenant:

| Column | Meaning |
|---|---|
| `license_key_sealed` | Vault ciphertext (`services/provider_connections/vault.py`, AES-256-GCM, AAD = `tenant-license-key:<tenant_id>` so a blob copied to another tenant fails to open) |
| `seal_version` | Vault seal version stamped at write (1 = AAD-bound) |
| `license_key_fingerprint` | First 8 hex of SHA-256(key); display and audit only |
| `license_key_set_at` / `license_key_set_by` | When / who (`hub_users.id`, SET NULL on erasure; classified in the erasure registry) |

A separate table (not columns on `tenants`) keeps the sealed secret out of the many `SELECT *` /
serialisation paths that touch `tenants`. The key is never returned, logged or audited; only the
fingerprint is.

## Routes

Tenant admin (`tenant:admin`, URL slug must equal the JWT tenant, mismatch = 403):

| Route | Effect |
|---|---|
| `GET /api/v1/tenant/<slug>/license` | `{configured, fingerprint, setAt}` |
| `PUT /api/v1/tenant/<slug>/license` `{licenseKey}` | write-only; validated 16-256 chars `[A-Za-z0-9._:-]`; audit `license.tenant_key_set` |
| `DELETE /api/v1/tenant/<slug>/license` | clears; audit `license.tenant_key_cleared` |

Operator (`tenants:admin`, global admin), keyed by numeric tenant id like `/superadmin/tenants/<id>`:

| Route | Effect |
|---|---|
| `GET /api/v1/superadmin/tenants/<id>/license-pools` | stored pool rows + `bypassDomain` |
| `PUT /api/v1/superadmin/tenants/<id>/license-pools/2` `{limit}` | writes `operator_set` row (`null` = not licensed, `-1` = unlimited, `>= 0`); audit `license.tenant_pool_set` |

The operator PUT is allowed ONLY when the request host is a hardcoded bypass domain
(`flask_core.entitlement.is_bypass_domain`); elsewhere it returns 409
`POOL_OPERATOR_SET_NOT_ALLOWED` (a licensed pool comes from the license server and cannot be
overridden by hand).

## Wiring the pool provider

```python
from services.tenant_license_service import get_tenant_license_key

provider = LicenseServerPoolProvider(
    PoolStore(async_dal, dal),
    lambda tenant_id: get_tenant_license_key(async_dal, dal, tenant_id),  # async resolver
    lambda: is_bypass_domain(current_request_host()),
    base_url=LICENSE_SERVER_URL,
)
```

`get_tenant_license_key` returns None when no key is set or the row cannot be opened (logs the
exception type only), so the pool provider treats an absent key as a Free tenant (`PoolInfo(limit=None)`, 402 `TENANT_TIER_REQUIRED` on a Premium allocation). Its resolver detects a stored-but-unreadable key (`get_license_status().configured`) and fails loud with `PoolUnavailable` (503) instead of reading it as Free.

## Not done yet (rest of #901)

Per-tenant TIER resolution (`LicenseGate.resolve_tier()` / `EntitlementClient.effective_tier`
still resolve one deployment-wide tier), per-tenant free-tier caps (#900) and SSO gating (#898),
and the license server's team axis in `/api/v2/entitlements` (separate repo).

## Key handling notes

- `seal_version` must be `>= 1` (CHECK in 0127 and enforced on read): 0 selects the legacy no-AAD open path, never valid here. The 0127 downgrade refuses while keys exist unless `ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE=1`.
- Setting/replacing/clearing a key evicts the tenant's cached license-server pool rows (see `license-pool.md`). The key routes are deliberately not feature-flag gated.
- Vault open failures return None from `get_tenant_license_key`, but the pool resolver turns "key stored yet unreadable" into a loud `PoolUnavailable`; only a genuinely absent key means Free. Database errors propagate and count as transient for the pool provider.
- The audit write after a key change is retried once, then fails loudly (ERROR + `waddles_audit_write_failed_total`).
