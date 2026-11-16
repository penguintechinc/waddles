# Bundle object storage: per-tenant sealing (svc-process wiring)

Object bodies written through the bundle `object-storage` host capability are sealed per tenant
before they reach the bucket. `bundle_host_storage::seal` holds the crypto (AES-256-GCM under a subkey
of the tenant's at-rest DEK, AAD = tenant | community | owning bundle | key); this page covers how
svc-process wires it (`core/svc_process/src/bundle_storage.rs`, `kv_sealing.rs`).

## Fail-closed contract

| Situation | Result |
|---|---|
| Normal write | sealed; the bucket holds an envelope, never the plaintext |
| No hub-api key service configured (`HUB_API_GRPC_ENDPOINT` / `SERVICE_JWT_TOKEN_ENDPOINT`) | capability **not provisioned** at startup: ERROR log + `waddles_bundle_storage_capability_provisioned` = 0; bundles get `not_implemented` |
| Tenant DEK unavailable at runtime (broker down, revoked) | the put/get is refused (`tenant data key unavailable`); nothing is written |
| Kill-switch `waddles.disable-tenant-at-rest-encryption` ON | writes are **refused** (`waddles_bundle_storage_denied{reason="sealing_unavailable"}`); there is no plaintext write path. Reads of sealed objects keep opening |
| Sealed-tagged body that is not an envelope | integrity error |
| Plaintext body (pre-encryption object) | served only inside the legacy window (below), otherwise refused |

There is no ledger and no per-object migration state: sealing is a property of every write, and the
only plaintext allowance is a bounded read window.

## Legacy plaintext reads (bounded, opt-in)

`BUNDLE_STORAGE_LEGACY_PLAINTEXT_UNTIL` (chart: `bundleStorage.legacyPlaintextUntil`) is a unix-seconds
cutoff. An object whose **server-assigned** last-modified time is before it is served as legacy plaintext
with a WARN; anything modified at or after it is refused, so a plaintext object an attacker writes later
is never served. Unset (the default) means no plaintext object is ever served.

* record the instant sealing was first enabled and set the cutoff to it;
* a malformed value, or one in the future (beyond 5 minutes of clock skew), is refused at startup
  (`validate_startup`) and at provisioning;
* re-tagging a legacy object rewrites it server-side and moves its last-modified past the cutoff: migrate
  legacy objects by re-putting them, then unset the cutoff.

## DEK cache and rotation

The object sealing shares the kv sealing's `TtlCachedDekProvider` (same cache, same Valkey
rotation/revocation listener), so a DEK rotation or revocation evicts both surfaces together.
