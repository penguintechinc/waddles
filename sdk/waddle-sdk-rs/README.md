# waddle-sdk-rs

Tier-1 Rust SDK for Waddles app bundles: idiomatic bindings over the
normative WIT world `waddle:bundle/stage@1.0.0`
(`wit/waddle-bundle/stage.wit`, imported by relative path, never forked).

Deliberately **thinner** than `waddle-sdk` (Python) -- no `penguin-dal`
facade, no compatibility obligation to a prior API. See
`docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS4.12/SS6.5.

## Layout

| Module | WIT interface | Notes |
|---|---|---|
| `types` | `types` | `PlatformEvent`, `StageEnvelope`, `TransportResult`, `TransportError`, `UnsupportedStage` -- host-testable, no `wit_bindgen` dependency |
| `context` | `context` | `BundleContext` + typed `config::<T>()` accessor |
| `http` | `http` | `Request` builder, `Response` with `.text()`/`.json::<T>()` |
| `kv` | `kv` | `get`/`set`/`delete`/`increment`, `get_json`/`set_json` |
| `db` | `db` | `Value`, `Rows`, `execute` -- no query builder (spec Q7) |
| `relay` | `relay` | `push::<T: Serialize>` |
| `flags` | `%flags` | `Tier` enum, `enabled`/`tier` |
| `log` | `log` | `Level`, `Fields` builder, `write` |
| `clock` | `clock` | `now_millis`/`now_rfc3339`/`monotonic_nanos`, host-testable `elapsed_ms` |
| `stage` | `process-stage`/`action-stage` | `ProcessStage`/`ActionStage` traits + [`export_stage!`] macro |
| `bindings_glue` | -- | `wasm32`-only: the single `wit_bindgen::generate!` call + WIT<->idiomatic conversions |

## Authoring a bundle

```rust
use waddle_sdk::{ActionStage, PlatformEvent, ProcessStage, UnsupportedStage};

struct MyBundle;

impl ProcessStage for MyBundle {
    fn transform(event: PlatformEvent) -> Result<Option<PlatformEvent>, UnsupportedStage> {
        Ok(Some(event))
    }
}

// Required even for the default stub -- see stage.rs's doc comment.
impl ActionStage for MyBundle {}

waddle_sdk::export_stage!(MyBundle);
```

See `bundles/rust/example` for a complete, building example.

## Building a bundle to a component

```bash
cargo install cargo-component --version 0.21.1 --locked
cargo install wasm-tools --version 1.259.0 --locked
rustup target add wasm32-wasip2 wasm32-wasip1
cd bundles/rust/example
cargo component build --target wasm32-wasip2
wasm-tools component wit "$CARGO_TARGET_DIR/wasm32-wasip2/debug/waddle_bundle_example_rust.wasm"
```

Toolchain versions are pinned to those proven by
`spikes/bundle-compiler-sandbox` (branch `spike/bundle-compiler-sandbox`,
Rust built in 0.40s in that spike).

## Coverage carve-out

`bindings_glue.rs` and every capability module's actual host-call function
(`http::send`, `kv::get`, `db::execute`, etc.) only compile under
`#[cfg(target_arch = "wasm32")]` -- there is no wasmtime-hosted test
harness for this crate, so `cargo test` on a host target never touches
them. They are exercised for real by `bundles/rust/example`'s `cargo
component build` + `wasm-tools component wit` check, the same way
`core/svc_process`'s `main.rs` is excluded from its own coverage gate as a
thin, mechanically-verified-by-a-downstream-build seam. Everything else
(type conversions, JSON payload helpers, error mapping, stage stub
defaults) is host-tested at ~97% line coverage (`cargo llvm-cov`).
