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
