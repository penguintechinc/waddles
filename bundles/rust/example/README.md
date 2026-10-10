# `bundles/rust/example` -- WIT-conformance fixture (not an installable bundle)

This crate is a **test fixture**, not an installable app bundle. It exists to prove
`sdk/waddle-sdk-rs` against the normative WIT world (`wit/waddle-bundle`,
`waddle:bundle/stage@1.0.0`) end to end: it compiles to a WASI 0.2 component whose exports
are checked against that world in CI. It is never published, activated, seeded, or shipped to
tenants.

## Why there is no `bundle.yaml`

The missing `bundle.yaml` / `hub-manifest.yaml` is **deliberate**. Without them the hub-api
publish flow has nothing to ingest, and the crate is not listed in `bundles/core-bundles.yaml`,
so no seeder, install, or activation path can ever reach it.

| Question | Answer |
|---|---|
| Can it be installed from the marketplace or seeded? | No -- no manifest, no catalog entry |
| What does it demonstrate? | A `!welcome`-style process-stage handler (`kv` import) and the SDK's automatic `UNSUPPORTED_STAGE` action-stage stub |
| Where is the real installable Rust bundle to copy? | `bundles/rust/ping` (has `bundle.yaml` + `hub-manifest.yaml` + a catalog entry) |
| What runs it? | `.github/workflows/rust-waddle-sdk-rs.yml` (`example-bundle-component` job) |

## Not to be confused with the svc-* built-in handlers

The Python modules under `core/svc_ingest/builtin_handlers/`, `core/svc_process/builtin_handlers/`
and `core/svc_action/builtin_handlers/` are **built-in stage handlers** that run natively
inside their host services. They are neither installable bundles nor this fixture.

## Build and verify

```bash
cargo install cargo-component --version 0.21.1 --locked
cargo install wasm-tools --version 1.259.0 --locked
rustup target add wasm32-wasip2 wasm32-wasip1
cd bundles/rust/example
cargo component build --target wasm32-wasip2
wasm-tools validate target/wasm32-wasip2/debug/waddle_bundle_example_rust.wasm
```

See `sdk/waddle-sdk-rs/README.md` for the SDK itself and the pinned toolchain versions.
