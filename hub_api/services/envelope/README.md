# `services/envelope` -- per-tenant envelope encryption + Enterprise external KMS (BYOK)

Implements `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` Sec2 (field AEAD),
Sec4 (key lifecycle) and Sec6 (BYOK). Operator + customer guide with per-provider setup:
**`docs/guides/external-kms-byok.md`**. This README is for people changing the code.

```
TenantEnvelopeService ──► KmsAdapter (Protocol) ──► PlatformKekAdapter   (baseline, local AES-GCM wrap)
   encrypt / decrypt                              ├► AwsKmsAdapter       (boto3 off-loop, assume-role + ExternalId)
   rotate_dek / rewrap                            ├► GcpKmsAdapter       (httpx, label proof-of-control)
   configure / activate / disable                 └► AzureKeyVaultAdapter (httpx, tag proof-of-control)
        │                          ▲
        │ keys/configs             │ built by KmsProviderRegistry (only providers the deployment enabled)
        ▼                          │
 PenguinDalEnvelopeRepository   providers.py  <-- ENVELOPE_KMS_PROVIDERS (default: none == flag OFF)
 keystore.tenant_encryption_keys / tenant_kms_configs   (migration 0054, RBAC from rbac-matrix.yaml)
```

| Module | Responsibility |
|---|---|
| `service.py` | the policy: cache + TTL, revocation vs outage, per-tenant isolation, never-persist-an-un-unwrappable-DEK, verified CAS re-wrap |
| `crypto.py` | pure primitives: HKDF subkey, AAD builder, AES-256-GCM seal/open, local wrap |
| `kms_adapter.py` | `KmsAdapter` Protocol, `CanonicalConfig`, `KmsProviderRegistry` |
| `aws_kms.py` / `gcp_kms.py` / `azure_key_vault.py` | one adapter per provider; classify errors into denied / transient / rejected / platform-credential |
| `_http.py` | shared httpx plumbing: no redirects, no env proxies, single-flight token cache, timeout + span + histogram |
| `providers.py` / `runtime.py` | settings parsing (fails at startup), registry assembly, process wiring (`app.config["envelope_runtime"]`) |
| `gate.py` | `compliance.external_kms` entitlement (flag AND Enterprise tier), fail closed |
| `repository.py` | the only SQL; every statement tenant-scoped and parameterised |
| `metrics.py` / `errors.py` / `models.py` | OTel instruments, error taxonomy, slotted frozen value objects |

## Invariants (each has a test -- change them deliberately)

1. **Baseline first.** No config == platform KEK; the gate and every provider are never consulted. BYOK only adds.
2. **No silent fallback.** Access-denied, pending, revoked, missing-config and entitlement-lapse paths raise
   `TenantKeyUnavailableError`/`ExternalKmsNotEntitledError`; the platform adapter is never called for new
   key material of a customer-wrapped tenant (`tests/envelope/test_service_regressions.py`).
3. **Entitlement gates NEW key material only.** Unwrapping existing DEKs and the exit ramp never need a licence.
4. **Revocation != outage != our own bad credential.** Three different reasons, three different behaviours.
5. **The wrap is context-bound** (tenant + purpose): AWS `EncryptionContext`, GCP AAD, Azure in-payload digest.
6. **Proof of control.** AWS ExternalId in the role trust policy; GCP label / Azure tag `waddles-external-id`.
7. **No key material anywhere but process memory.** `repr`s hide key-bearing fields; errors carry fixed strings
   and provider *codes* only; botocore's wire-level DEBUG loggers are pinned above DEBUG
   (`aws_kms.py`) because they would otherwise log the base64 plaintext DEK.
8. **Settings cannot disable a safety property** (`EnvelopeSettings.__post_init__`), and bad provider config
   fails at startup, not on a tenant's first request.

## Adding a provider

1. New adapter module implementing `KmsAdapter` (wrap/unwrap with the context, `verify()` = key shape +
   proof of control + round-trip probe) and a `validate_*` returning `CanonicalConfig`.
2. Map its errors with the shared taxonomy (`KmsAccessDeniedError` only for genuine customer revocation).
3. Add its id to `IMPLEMENTED_PROVIDERS`, platform settings to `providers.py`, one `registry.register` call.
4. A socket-level mock in `tests/envelope/kms_mocks.py` speaking the real wire protocol, and the same
   parametrized scenarios in `test_service_byok.py` (revocation, outage, exit ramp, no-log-leak).
5. Chart: provider id + credentials Secret in `_kms.tpl`'s validation and `kms.yaml`.

## Tests

`hub_api/tests/envelope/` -- see the module docstrings. Real Postgres (`test_repository_pg.py`) needs docker
(skipped without it, like the repo's other PG tests); the SeaweedFS E2E is opt-in
(`make test-seaweedfs-sse-kms`).

## Stream identity-field DEKs (`KeyService.GetStreamDek`, #442)

A second key lineage, `ingest-stream`, lives in the same `keystore.tenant_encryption_keys`
table (`purpose='ingest-stream'`), wrapped by the same KEK as the tenant's at-rest keys
(platform `TENANT_KEK_HEX`, or the tenant's BYOK key once activated). `build_envelope_runtime`
wires `runtime.stream_service` (lineage-separated subkeys) and registers the stream repository
as a sibling of the at-rest service, so BYOK activate / disable / re-wrap move both lineages
together -- a disable never strands stream DEKs under a dropped customer key.

`services/stream_dek_broker.py` serves them over internal gRPC only:

| Field | Contract |
|---|---|
| Scope | `key:stream-dek:read` -- granted only to `svc-ingest` + `svc-process` (`serviceJwt.identities`) and re-checked by SPIFFE service name |
| `tenant_id` | numeric tenant id (never a slug); tenant-bound tokens get only their own tenant, `system` any existing one |
| `purpose` | `ingest-stream` |
| `version` | `0` = active (provisioned on first use); `N` = that version (active or retired) |
| `caller_ephemeral_public_key` | 32 raw X25519 bytes, fresh per request |
| `sealed_dek` | `enc(32) || AES-256-GCM(ct||tag)`; `key||nonce = HKDF-SHA256(salt=None, ikm=X25519(eph, enc), info=ctx, 44)`, AAD = ctx |
| ctx | `"<service>|<tenant_id>|<purpose>|<version>"`, service = SPIFFE last segment (`svc-ingest`) |
| `version` (resp) | the DEK version = key id; ciphertext must carry it |
| `ttl_seconds` | caller cache TTL, `STREAM_DEK_TTL_SECONDS` (default 600, bounds 60..3600) |

Every decision (issued or refused) writes `keystore.stream_dek_issuances` (alembic 0101); a
sealed key is returned only after its `issued` row commits. Rotate with
`runtime.stream_service.rotate_dek(tenant_id)`; old versions stay retrievable.
