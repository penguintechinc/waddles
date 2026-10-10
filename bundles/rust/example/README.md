# example (Rust)

`waddle-bundle-example-rust` -- the Tier-1 Rust **SDK conformance example**: a process-stage
handler that rewrites a chat event into a greeting, counting greeted events via the `kv` host
import and logging via the `log` import. Exists to prove `waddle-sdk-rs` against the normative
`waddle:bundle/stage@1.0.0` WIT world end to end. **Not a command bundle and not shipped to
tenants.**

| | |
|---|---|
| Role | SDK example / CI build target (`.github/workflows/rust-waddle-sdk-rs.yml`) |
| Manifest | none -- no `bundle.yaml`, no `hub-manifest.yaml`, not in `bundles/core-bundles.yaml` |
| Command / verbs | none -- reacts to any event it is routed; no `!command` match, no verbs |
| PostHog flag | none (never activated; an example, not a feature) |
| Platforms | platform-agnostic: echoes `platform`/`event_type`/`actor`/`occurred_at` through |
| Stages | `process-stage.transform` only; `action-stage` is the SDK's automatic `UNSUPPORTED_STAGE` stub |

## Behavior

| Input payload | Output payload |
|---|---|
| `{"message": "hello"}`, actor `viewer-1`, 3rd event | `{"message":"hello","greeting":"welcome back, viewer-1! (seen 3 events)","seen_count":3}` |
| no actor | greeting uses `unknown` |
| unparseable / wrongly shaped payload | `message` degrades to `""` -- never fails the pipeline |

## Host imports used (what a manifest would declare)

| Import | Why |
|---|---|
| `kv` | `increment("welcome.seen")` -- one community-scoped counter of greeted events |
| `log` | one INFO line per transform: tenant id + running count only |
| `context` | reads the tenant id for the log line |

## Hygiene

- **kv key is colon-free and actor-free.** `welcome.seen` -- the host rejects `:` (gh-631) and a
  raw username must not sit in a key outside the API's PII boundary (gh-674). It used to be
  `welcome:seen:{actor}`, which every real host call rejected -- silently, because the
  transform swallows a kv error with `unwrap_or(0)`.
- **PII-free logs.** The log line carries `tenant` and `seen_count` only -- never the actor or
  the message text. The actor appears solely in the reply going back to the same channel.
- Known limitation: a `kv` error still degrades the count to `0` (the stage's only error type
  is `UnsupportedStage`, so a transform cannot surface it) -- acceptable for an example, but a
  real bundle should log the failure at ERROR and reply loudly.

## Build

```bash
cd bundles/rust/example
cargo component build --target wasm32-wasip2 --locked
wasm-tools validate target/wasm32-wasip2/debug/waddle_bundle_example_rust.wasm
wasm-tools component wit target/wasm32-wasip2/debug/waddle_bundle_example_rust.wasm
```

(With a shared `CARGO_TARGET_DIR` the component lands under that directory instead; the CI job
asserts the WIT exports `waddle:bundle/process-stage@1.0.0` and `action-stage@1.0.0`.)

Pinned toolchain: `rust-toolchain.toml` (Rust 1.97.1 + `wasm32-wasip2`/`wasm32-wasip1`),
`cargo-component 0.21.1`, `wasm-tools 1.259.0`. CI also runs `cargo fmt --check`,
`cargo clippy --target wasm32-wasip2 --all-targets --locked -- -D warnings` and
`cargo deny check`.

## Test

```bash
cd bundles/rust/example
cargo test --locked                       # host unit tests of the target-independent logic
cargo llvm-cov --locked --fail-under-lines 90
```

The SDK's capability functions (`kv`, `log::write`, `context`, the component export) only exist
on `wasm32`, so the stage impl and export are `#[cfg(target_arch = "wasm32")]`-gated (same split
as `bundles/rust/ping`); host tests cover `build_reply`, `log_fields`, `greeting` and the key
constant, and the gated glue is verified by the CI `cargo component build` job.
