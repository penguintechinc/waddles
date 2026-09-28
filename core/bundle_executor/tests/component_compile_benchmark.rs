//! Release-profile `Component::new` compile-time comparison between the
//! C#-toolchain spike fixture (`csping.wasm`) and an existing Rust
//! fixture (`hostile_fixture.wasm`), both compiled against the same
//! `wasmtime::Engine` this executor actually runs
//! (`crate::engine::build_engine`). Exists because the C# spike's
//! headline ~104s compile-time finding was originally measured under
//! `cargo test`'s default (debug) profile, which leaves Cranelift itself
//! unoptimized and overstates real-world compile cost -- this file
//! isolates just the `Component::new` call, with no wire-protocol/tokio
//! overhead, and is meant to be run with `--release`:
//!
//! ```bash
//! cargo test --release --test component_compile_benchmark -- --ignored --nocapture
//! ```
//!
//! Numbers from a real run are recorded in
//! `bundles/csharp/csping/README.md` ("Cold-load (compile) time").

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::time::Instant;

use bundle_executor::config::CliConfig;
use bundle_executor::engine::build_engine;
use wasmtime::component::Component;

const CSPING_WASM: &[u8] = include_bytes!("fixtures/csping.wasm");
const HOSTILE_FIXTURE_WASM: &[u8] = include_bytes!("fixtures/hostile_fixture.wasm");

fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

/// Fresh `Engine` per measurement (matches `Executor::new`'s own
/// one-engine-per-process shape closely enough for a compile-time
/// comparison; engine construction itself is not what is being timed).
fn compile_time_ms(bytes: &[u8]) -> u128 {
    let engine = build_engine(&test_config()).expect("engine builds");
    let start = Instant::now();
    let _component = Component::new(&engine, bytes).expect("component compiles");
    start.elapsed().as_millis()
}

// Ignored by default: this is an informational timing report (no
// correctness assertion -- the meaningful output is the printed
// milliseconds, not a pass/fail), and both fixtures compiling adds
// nontrivial wall-clock time to every `cargo test` run regardless of
// profile. Run explicitly:
//   cargo test --release --test component_compile_benchmark -- --ignored --nocapture
#[test]
#[ignore = "informational timing report; run explicitly with --release for a meaningful number"]
fn release_profile_component_compile_time_csping_vs_hostile_fixture() {
    let csping_ms = compile_time_ms(CSPING_WASM);
    let hostile_ms = compile_time_ms(HOSTILE_FIXTURE_WASM);
    eprintln!(
        "[release] csping.wasm ({} bytes) Component::new: {csping_ms}ms",
        CSPING_WASM.len()
    );
    eprintln!(
        "[release] hostile_fixture.wasm ({} bytes) Component::new: {hostile_ms}ms",
        HOSTILE_FIXTURE_WASM.len()
    );
}
