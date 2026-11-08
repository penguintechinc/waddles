# Tenant at-rest encryption: bundle `kv` values

Phase 1 of `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`.
Bundle `kv` **values** are AES-256-GCM sealed under the tenant's DEK by the
data plane (svc-process, svc-action), below the WIT boundary: bundles never see
keys or ciphertext, and `kv` WIT is unchanged. Valkey (RDB/AOF, replicas) only
ever holds ciphertext for sealed values.

```
hub-api (keystore: KEK-wrapped DEK per tenant, versioned)
   |  gRPC KeyService.GetTenantDek  (TLS, machine JWT scope keys:tenant-dek:read, tenant claim "system")
   v  response sealed to a fresh per-call X25519 key (HPKE-lite)
svc-process / svc-action  --- DEK in memory only (TTL cache, zeroize on drop) ---
   |  HKDF("waddles-bundle-data-v1") subkey -> AES-256-GCM, AAD = tenant_id|community_id|app_id|"kv"|key|dek_version
   v
Valkey  bundlekv:{tenant}:{community}:{app}:data:{key} = magic|alg|dek_version|nonce|ciphertext
```

## What is and is not encrypted

| Item | Sealed | Notes |
|---|---|---|
| `kv.set` / `kv.get` values | yes | AAD binds numeric tenant + community + app + guest key, so a ciphertext moved to another key, app, community or tenant fails to open (`KvError::Integrity`) |
| guest keys | no | charset-restricted and PII-free by rule; they drive the live index and quotas |
| `kv.increment` counters | no | `INCRBY` cannot operate on ciphertext; canonical decimal integers stay readable by `kv.get` even when plaintext is otherwise refused. Do not store sensitive data in counters |
| CAS digest (#1139) | n/a | the Lua scripts hash whatever is stored, i.e. the ciphertext: no plaintext hash is exposed and the version is opaque. Rewriting the same plaintext yields a new version (random nonce); do not rely on versions for "unchanged" detection |

Key namespace today is name-keyed (`bundlekv:{tenant slug}:{community name}:...`);
the numeric-id migration (spec section 4, T9) is separate. Sealing already uses
the **numeric** ids for the DEK lookup and AAD, so a rename does not break
decryption of sealed values.

## Behaviour contract

- **Fail closed.** DEK unavailable (broker down, revoked, unknown tenant/version),
  or a scope without numeric ids: `set` and `get` of sealed values return
  `KvError::KeyUnavailable` (collapses to `kv.error::backend` for the guest).
  There is no plaintext write path on key trouble.
- **Kill-switch** `waddles.disable-tenant-at-rest-encryption` (PostHog flag via
  the license client): unseen / OFF / license server unreachable = encryption ON.
  ON = operator-chosen emergency rollback to plaintext **writes** (WARN logged
  per write, `bundle_crypto_ops_total{op="seal",result="disabled"}`); reads still
  open envelopes, and plaintext is accepted while it is ON. Never flips
  automatically on a key error. License bypass domains do not turn it ON.
- **Legacy plaintext reads** (values written before sealing shipped) are a
  migration allowance, accepted only when **both** hold: operator config
  `KV_LEGACY_PLAINTEXT_READS_ALLOWED` is `true` (default `true` during migration;
  planned flip to `false`), **and** the scope has no *sealed-since marker*
  (`bundlekv:...:sealed`, persisted in Valkey on the scope's first sealed write).
  Otherwise a non-envelope value is `KvError::Integrity`, which stops plaintext
  planted by anyone with raw Valkey access. Every accepted legacy read logs WARN
  with `app_id` and a key hash (never the key or value) and counts
  `bundle_crypto_legacy_plaintext_reads_total`.
- **Rotation.** Every envelope header carries its `dek_version`; old versions stay
  readable via `GetTenantDek(dek_version=N)`; new writes use the active version.
  An unknown newer version triggers exactly one broker call, then fails closed.
- **Revocation race.** The DEK cache stores a fetched DEK only if no invalidation
  for that tenant landed while the fetch was in flight (generation counter).

## Configuration

| Setting | Env | Default |
|---|---|---|
| hub-api gRPC endpoint | `HUB_API_GRPC_ENDPOINT` | unset = sealing refuses every op (`key_unavailable`), ERROR logged at startup |
| machine-JWT bootstrap | `SERVICE_JWT_TOKEN_ENDPOINT`, `SERVICE_JWT_SA_TOKEN_PATH` | as other hub_client users |
| gRPC CA bundle | `HUB_API_GRPC_CA_FILE` | system roots if unset |
| legacy plaintext reads | `KV_LEGACY_PLAINTEXT_READS_ALLOWED` | `true` (migration window) |
| kv Valkey | `VALKEY_KV_URL` (dedicated `noeviction` instance) | unchanged |
| invalidation stream | spine Valkey, `keys:tenant-dek:invalidate` | listener spawned per stage |

The broker connection is lazy: the first DEK request connects and a failed
attempt retries on the next request, so hub-api being down at pod start does not
wedge the stage. Caller identity: `sub` must be `svc-process` / `svc-action`.

## Telemetry (no key material, low-cardinality labels)

`bundle_crypto_ops_total{op,surface,result}`, `bundle_crypto_op_duration_seconds`
(histogram), `bundle_crypto_dek_fetch_total{result}`,
`bundle_crypto_dek_cache_hits_total`, `bundle_crypto_key_unavailable_total{surface}`,
`bundle_crypto_legacy_plaintext_reads_total{surface}`. Logs carry `app_id`,
`op`, `surface`, `result`; never keys, DEKs, plaintext, or raw guest keys.

## Runbook

| Symptom | Cause / action |
|---|---|
| kv ops fail with `key_unavailable`, `bundle_crypto_key_unavailable_total` rising | broker unreachable or tenant has no key. Check hub-api `GetTenantDek` (`UNAVAILABLE` = key provider down/revoked, `NOT_FOUND` = no key: run the tenant-key backfill, `PERMISSION_DENIED` = caller `sub`/scope/tenant claim wrong, `RESOURCE_EXHAUSTED` = 5 rps burst 30). Cached DEKs keep serving up to their TTL (30-600 s) |
| `integrity` errors on reads | ciphertext moved/tampered, wrong tenant/community/app/key, or plaintext in a sealed scope. Investigate raw Valkey writes; the log names `app_id` and a key hash |
| must stop encrypting right now | set `waddles.disable-tenant-at-rest-encryption` ON in PostHog. Plaintext writes begin immediately, reads keep working; turn OFF to resume sealing. Pre-existing sealed values stay sealed |
| rotate a tenant DEK | rotate in hub-api; `dek-rotated` invalidation evicts caches; old ciphertext reads via the version in its header |
| revoke a tenant | revoke in hub-api: broker returns `UNAVAILABLE`/`NOT_FOUND`, stages evict on the invalidation signal; sealed values become unreadable (crypto-shred) |
| flip legacy reads OFF | when `bundle_crypto_legacy_plaintext_reads_total` has been flat for a full TTL window, set `KV_LEGACY_PLAINTEXT_READS_ALLOWED=false` and roll the stages |
| a counter key reads back an integrity error | it holds non-integer plaintext in a sealed scope: it was written by a bundle build that wrote plaintext, or planted; rewrite it via `kv.set` |

## Tests

`core/bundle_host_crypto/tests` (Python/Rust golden vectors for the envelope,
blind index and the tenant-DEK seal, rotation, cross-tenant, outage),
`core/bundle_host_kv/src/seal*_tests.rs` (policy, marker, WARN shape),
`core/bundle_host_kv/tests/sealed_valkey.rs` (real Valkey: raw `GET` shows
ciphertext, round trip, cross-tenant/key swap fails, missing key refuses,
rotation, legacy policy, counters, kill-switch). Regenerate vectors with
`core/bundle_host_crypto/scripts/gen_{golden,hpke}_vector.py`.

## Deferred (later phases)

db v2 column modes, numeric `KvScope` + sweeper + per-tenant `migrated` marker,
queue/stream payload sealing, economy/reputation sealed balances, object bodies
(spec sections 3-4, 7). The `waddles:usage` stream and the interaction store
are unchanged here.
