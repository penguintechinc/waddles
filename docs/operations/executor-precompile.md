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

### Precompile signing keypair: what exists today

The keypair is NEW and separate from the bundle-approval key. This change adds no new generation
mechanism: key provisioning is whatever the chart already does, and the platform secret-store design
(auto-generate on first run, persisted outside the chart) is pending owner decisions. As it stands:

| Piece | Where | Who/what creates it |
|---|---|---|
| Private seed (`PRECOMPILE_SIGNING_PRIVATE_KEY`, base64 32 bytes) + `PRECOMPILE_SIGNING_KEY_ID` | Secret `<fullname>-executor-precompile-signing`, mounted as a FILE into the `executor-precompile` sign step only | chart `auto-provision-keys` Job on alpha/local (`global.deploymentTier`); beta/gamma/prod must pre-create it and set `autoProvisionedKeys.precompileSigning.externalSecret: true` |
| Public keys (`PRECOMPILE_SIGNING_PUBLIC_KEYS` = `{"<key_id>": "<base64 public key>"}`) | ConfigMap `<fullname>-executor-precompile-signing-public` | derived from the Secret by the same Job (re-derived on every run) |
| Executor consumption | env `PRECOMPILE_SIGNING_PUBLIC_KEYS` on `svc-bundles-executor` (Helm #1248 wires the ConfigMap) | — |
| Bucket write identity | `svc-bundles-precompiler` (`PRECOMPILER_S3_*`), Write only under `precompiled/` | chart secrets |

Owner checklist: (1) on alpha nothing to do once #1248's executor env lands; (2) elsewhere, supply the
Secret/ConfigMap above via the environment's secret mechanism; (3) let the backfill hook run, then confirm
`executor_precompile_fallback_total` stops increasing and `executor_load_duration_seconds{source="aot"}`
appears. Rotation: configure the new and previous `key_id` together in the public-key map, re-publish,
then drop the old id. Caveat for the Helm owner: the auto-provision Job re-derives the public ConfigMap from
the current private key on every run, which would drop a retained previous id.

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
