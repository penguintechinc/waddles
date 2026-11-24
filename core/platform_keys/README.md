# platform-keys

Data-plane **verifying-key cache** over `platform_vault.public_keys` (design T5 of the platform
secret store, see `docs/PLATFORM_VAULT.md`). Data-plane services verify signatures against PUBLIC
keys that hub-api generates and rotates; this crate keeps them fresh from the read-only replica so
a rotation needs no redeploy. Public material only: no sealed value, fingerprint or secret ever
reaches it.

## Behaviour contract

| Situation | Result |
|---|---|
| Startup | `KeyCache::seed(max_wait)` loads the whole view; bounded exponential backoff (100 ms -> 5 s) until every `required_types` entry has a usable key, else `SetupError::NotReady` |
| Readiness | `KeyCache::ready()` is false until each required type has a usable key (use it in the gRPC health probe) |
| Unknown kid | lazy-load that one kid; still missing -> `KeyError::UnknownKid` (fail closed) and a **30 s negative cache** |
| Poll | `spawn_poller()` replaces the snapshot every **60 s**; a retired key disappears at the next refresh |
| Store down | last-known keys are **kept**; `refresh_failures` counted; an uncached kid -> `KeyError::Unavailable` (not negative-cached) |
| Verify-only key | honoured until `verify_until = rotated_at + verify_window`, then `KeyError::Expired`; no `rotated_at` -> `Expired` |
| Stale snapshot | verify-only keys are refused with `KeyError::Stale` once the last successful read is older than 15 min (security review M6); active keys keep verifying |
| Bad kid | empty or > 128 bytes -> `UnknownKid` without a database call or a negative-cache entry |
| Concurrency | one in-flight lazy load; a burst of the same unknown kid costs one query |

## Key-source switch (`*_KEY_SOURCE=db|configmap`)

`KeySource::resolve(explicit, disable_flag_on)` is evaluated once at startup. An explicit value
wins; a typo is an error. Unset: the kill-switch flag `waddles.disable-db-platform-keys` decides
(OFF/unseen = **db**, ON = configmap). There is no runtime fallback: with `db` selected a database
that cannot be read fails readiness and fails lookups, it never consults the ConfigMap.

## Usage

```rust
let loader = Arc::new(PgKeyLoader::new(std::env::var("PLATFORM_KEYS_DATABASE_URL")?, Duration::from_secs(5)));
let cache = Arc::new(KeyCache::new(loader, CacheConfig::new(vec![ARTIFACT_SIGNING_TYPE], Duration::from_secs(24 * 3600)))?);
cache.seed(Duration::from_secs(60)).await?;
let (stop, _poller) = cache.spawn_poller();
let key = cache.get(ARTIFACT_SIGNING_TYPE, kid).await?; // -> KeyRecord { public_material, .. }
```

`PgKeyLoader` runs two fixed statements against `platform_vault.public_keys` (the kid is a bound
parameter). The connecting role needs only schema `USAGE` + `SELECT` on the view, which the
`svc_*` roles already hold from migration `0109_platform_vault`. The `postgres` feature (default)
pulls sea-orm; the credential-less `core/bundle_executor` uses `default-features = false` and
receives its loader from an embedder via `Executor::with_key_cache`.

## Telemetry

OTel metrics (global meter `platform_keys`; labels are `key_type` and a closed `reason` set only,
never a kid): `platform_keys_cache_hits_total`, `..._cache_misses_total`,
`..._refresh_failures_total`, `..._unknown_kid_total`, `..._rejected_total{reason}`,
`..._refreshes_total`, histogram `..._refresh_duration_seconds`. Span `platform_keys.refresh`.
`tracing` logs carry key type / kid (sanitised, truncated) / version only.

## Tests

```
cargo test                      # state machine (fake loader + manual clock) + real-Postgres tests
cargo llvm-cov --fail-under-lines 90
cargo clippy --all-targets -- -D warnings && cargo deny check
```

The Postgres tests need `TEST_DATABASE_URL` (owner login) and FAIL, never skip, without it.
