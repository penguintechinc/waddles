# Bundle AOT precompile (`bundle-executor --precompile`)

Executor-scale plan task 4. Approved bundles are compiled ahead of time into signed `.cwasm`
artifacts so executors deserialize (milliseconds) instead of JIT-compiling (seconds to minutes).

## Flow

```
approve/activate/sign (hub-api)
  -> upload signed sidecar -> enqueue bundle_precompile_requests (once per digest)
executor-precompile CronJob (every 2 min)        executor-precompile-backfill Job (Helm hook)
  export: lease pending rows                       export: every signed, active version
  bundle-executor --precompile                     bundle-executor --precompile
  ack: delete done rows, record failures           ack --backfill: exit 1 on any failure
```

Per item the precompiler (`core/bundle_executor/src/precompile.rs`):

1. fetches `component_key` + `sidecar_key`, checks the sha256 digest and the platform
   bundle-approval signature (same checks as the runtime `load`);
2. compiles with the shared `engine_config()` (`src/engine_config.rs`), the same builder
   the executor uses;
3. signs with the dedicated precompile key and uploads
   `precompiled/<engine_fp>/<digest>.cwasm`, then `<...>.cwasm.sig` (the signature is the
   commit marker, so it is written last).

An artifact that already exists for this fingerprint and verifies is skipped, so re-runs are
cheap.

## Trust chain (security-sensitive)

`Component::deserialize` maps native code, so a forged `.cwasm` means code execution in the
executor. Before deserializing, the executor MUST call
`precompile::verify_precompiled(cwasm, sig, keys, digest, engine_fingerprint)`. It binds an
Ed25519 signature to (digest, engine fingerprint, sha256 of the artifact). Its domain
separator (`waddles-cwasm-sig-v1`) differs from the bundle-approval one.

| Item | Value |
|---|---|
| Signing key Secret | `<fullname>-executor-precompile-signing` (`PRECOMPILE_SIGNING_PRIVATE_KEY` base64 seed, `PRECOMPILE_SIGNING_KEY_ID`), auto-provisioned on alpha/local, mounted as a file into the precompile container only |
| Public keys ConfigMap | `<fullname>-executor-precompile-signing-public` (`PRECOMPILE_SIGNING_PUBLIC_KEYS` JSON), mounted by executors |
| Bucket identity | `svc-bundles-precompiler` (`PRECOMPILER_S3_*`): Read/List bundle bucket, Write only `precompiled/` |

## Operations

- Print a binary's fingerprint: `bundle-executor --precompile --print-engine-fingerprint`
- Dead letters: `SELECT app_id, version, attempts, last_error FROM bundle_precompile_requests WHERE attempts >= 5`
- Metrics: `executor_precompile_items_total{status}`, `executor_precompile_duration_seconds`,
  `waddles_hub_bundle_precompile_requests_total{op,outcome}`
- Backfill hook phase: `pipeline.hubApi.executorPrecompile.backfill.hookPhase`. The default is
  `post-install,post-upgrade`. Switch to `pre-upgrade` once the precompiler identity and key
  exist, so artifacts are built before an executor image rolls out.

## Existing bundles: backfill, storage, and what a miss does

**Where artifacts live.** SeaweedFS bucket `waddles-bundles` (`pipeline.rustDataPlane.bundleExecutor.bucketName`),
keys `precompiled/<engine_fp>/<digest_hex>.cwasm` and `<...>.cwasm.sig`. `<engine_fp>` is the engine
fingerprint of the executor image/config, so a new executor image starts with an empty prefix. The
`svc-bundles-precompiler` S3 identity can write only `precompiled/`; it can never overwrite a bundle
component or sidecar.

**How the ~160 existing core bundles get artifacts.** Two producers, both run `bundle-executor --precompile`
from the SAME image as the executor:

| Producer | Covers | When |
|---|---|---|
| `executor-precompile-backfill` Helm hook Job (`hub_api.cli.precompile_queue export --all-active`) | every signed, currently-active version (`app_active_versions`) — this is how all existing core bundles are covered | every `helm upgrade`/`install` (phase `post-install,post-upgrade`; set `pipeline.hubApi.executorPrecompile.backfill.hookPhase=pre-upgrade` once the key + identity exist so artifacts are warm BEFORE a new executor image rolls out) |
| `executor-precompile` CronJob (`export` leases `bundle_precompile_requests` rows) | digests approved/activated/signed after the queue table exists (enqueued once per digest) | every 2 minutes |

The queue only knows digests signed after migration `0108_bundle_precompile_requests`, and it is not
fingerprint-aware: after an executor image change (new fingerprint) only the backfill rebuilds the
prefix. The backfill is idempotent (an artifact that exists for this fingerprint and verifies is
skipped) and fails the release loudly if any bundle cannot be compiled (`ack --backfill` exits 1).
Run it by hand with `helm upgrade` (hook) or by creating a Job from the hook template.

**What the executor does on a miss** (`SharedRuntime::try_aot` → `materialize`): any problem —
artifact or `.sig` absent (404), oversized, signature/fingerprint/hash mismatch, no
`PRECOMPILE_SIGNING_PUBLIC_KEYS`, malformed or bundle-key-reusing key config, deserialize error —
logs at ERROR, increments `executor_precompile_fallback_total{reason=fetch|signature|deserialize}`,
and compiles from the signed source under the compile limiter (digest + bundle signature verified
first). The executor never loads unverified bytes and never writes `precompiled/` itself. A cold
executor with an empty prefix therefore JIT-compiles, bounded by `EXECUTOR_MAX_CONCURRENT_COMPILES`.

### Verification order before `unsafe Component::deserialize`

1. bundle sidecar signature (`BUNDLE_SIGNING_PUBLIC_KEYS`) over `(app_id, version, digest)`;
2. detached `.sig` parsed: algorithm `ed25519`, `digest` equals the requested digest, `engine_fingerprint`
   equals this engine's, `sha256(cwasm)` equals the signed hash, `key_id` present in
   `PRECOMPILE_SIGNING_PUBLIC_KEYS`, `verify_strict` over the domain-separated payload
   (`waddles-cwasm-sig-v1`, distinct from the bundle `waddles-bundle-sig-v1`);
3. only then `Component::deserialize` (wasmtime additionally re-checks engine compatibility).

Key separation is enforced twice: the signing domains differ, and an executor whose
`PRECOMPILE_SIGNING_PUBLIC_KEYS` contains any public key also present in `BUNDLE_SIGNING_PUBLIC_KEYS`
disables AOT (ERROR at startup, every load falls back). Rotation: the key set is a map of
`key_id → public key`; configure the new and previous ids together, re-publish, then drop the old id.
Logs and errors carry key ids and paths only, never key material.

### Owner: provision the precompile signing keypair

The keypair is NEW (separate from the bundle-approval key). Nothing in CI or an agent generates it.
On `alpha`/`local` (`global.deploymentTier`) the chart's `auto-provision-keys` Job generates it if the
Secret is missing — no action needed. On beta/gamma/prod (or to pin your own key on alpha) the owner runs:

```bash
# 1. generate + install (private Secret for the publisher, public ConfigMap for executors).
#    Private key lives only in a 0600 temp file, shredded on exit; only the public key is printed.
make generate-precompile-signing-key KEY_ID=<key-id> \
     KUBE_CONTEXT=<ctx> NAMESPACE=waddlebot FULLNAME=<helm fullname, e.g. waddlebot>
#   writes Secret      <fullname>-executor-precompile-signing         (PRECOMPILE_SIGNING_PRIVATE_KEY, PRECOMPILE_SIGNING_KEY_ID)
#   writes ConfigMap   <fullname>-executor-precompile-signing-public  (PRECOMPILE_SIGNING_PUBLIC_KEYS = {"<id>":"<pub>"})
#   existing ids in the ConfigMap are kept, so a re-run is a rotation (new + previous accepted)

# 2. values (env values file): stop the chart auto-generating, pin the id
#    autoProvisionedKeys.precompileSigning.externalSecret: true
#    autoProvisionedKeys.precompileSigning.keyId: <key-id>

# 3. executors must read the public ConfigMap as PRECOMPILE_SIGNING_PUBLIC_KEYS
#    (Helm: configMapKeyRef <fullname>-executor-precompile-signing-public / PRECOMPILE_SIGNING_PUBLIC_KEYS)

# 4. publish artifacts under the new key: helm upgrade (backfill hook), then watch
kubectl -n waddlebot logs job/<fullname>-executor-precompile-backfill
#    verify: executor_precompile_fallback_total stops increasing; executor_load_duration_seconds{source="aot"} appears

# 5. after a rotation, remove the previous id once every artifact has been re-signed:
kubectl -n waddlebot edit configmap <fullname>-executor-precompile-signing-public
```

Caveat for the Helm owner: the chart's auto-provision Job re-derives and rewrites the public
ConfigMap from the Secret's private key on every run, which drops extra (previous) ids; keep the
Job out of the way (`externalSecret: true`) while a rotation window is open.

## Bounds (nothing grows without a cap)

| What | Cap / default | Env |
|---|---|---|
| Resident compiled-module bytes (LRU evicts idle unpinned modules; pinned/in-flight are never evicted, the cache goes over budget loudly instead) | 8192 MiB; `0` = unlimited (unreferenced modules dropped immediately; WARNs under a known container limit) | `EXECUTOR_MEMORY_BUDGET_MB` |
| Budget vs container limit | startup REFUSES `budget >= limit`; WARNs above 70% of the limit or when the limit is unknown | `EXECUTOR_CONTAINER_MEMORY_LIMIT_MB` (else cgroup `memory.max`) |
| Precompiled `.cwasm` fetch size | 256 MiB | `BUNDLE_MAX_PRECOMPILED_BYTES` |
| `.cwasm.sig` fetch size | 64 KiB (fixed) | — |
| Source component fetch size | 32 MiB | `BUNDLE_MAX_COMPONENT_BYTES` |
| Concurrent JIT compiles | `min(4, CPUs)` | `EXECUTOR_MAX_CONCURRENT_COMPILES` |
| Single-flight map | one entry per digest being resolved right now; removed when the attempt resolves | — |
| Stage reference map | one entry per loaded digest; removed at zero refs | — |
