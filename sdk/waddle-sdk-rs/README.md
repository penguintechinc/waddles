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
| `command` | -- | Shared `!cmd [sub-module] [verb] args` / `--opt value` parser, durations, mentions, `$(name)` placeholders; golden-vector parity with Python -- see `docs/sdk-rs.md` |
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

## Reading the community's saved config

A community admin edits a bundle's settings in the admin UI (the bundle-admin gateway validates
them against the manifest `community_config_schema` and stores each save as a new version). At
invoke time the stage hands the bundle the **live** config of the invoking community -- the
manifest property `default`s overlaid with the latest saved version -- as a JSON object string in
`get-context().config_json`. Nothing to fetch, poll or cache in the bundle:

```rust
use waddle_sdk::BundleContext;

#[derive(serde::Deserialize, Default)]
#[serde(default)]
struct Config {
    default_duration_minutes: u32, // schema default (e.g. 60) is already merged in
    default_tz: String,
}

fn dispatch(ctx: BundleContext) -> Result<(), String> {
    // `ctx.config::<T>()` deserializes `config_json`; an empty `{}` means "no saved config and
    // no schema defaults" -- use your own `Default` rather than assuming keys exist.
    let cfg: Config = ctx.config().map_err(|e| e.to_string())?;
    let _ = cfg.default_duration_minutes;
    Ok(())
}
```

* Served only for the verified envelope scope (your own tenant/community) -- never another's.
* A save reaches running bundles within seconds (the stage rechecks a Valkey version key; a
  5-minute TTL is the backstop). If hub-api is unreachable the last-known config is served; if it
  was never loaded the stage logs an ERROR and passes `{}` -- treat a missing key as "use the
  schema default" in code too.
* Declare the schema (types, bounds, `default`s) under `community_config_schema:` in the manifest;
  see `bundles/rust/calendar` (`default_duration_minutes`) for a working example.
