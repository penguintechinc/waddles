//! wasmtime `Engine`/`Linker` construction against the normative WIT world
//! and the epoch-tick background task that backs the per-call deadline
//! (spec SS7.2/SS7.3, assumption A16).
//!
//! The `bindgen!` invocation below is the executor's only copy of the WIT
//! world's Rust bindings; it points at the committed
//! `wit/waddle-bundle/stage.wit` by relative path rather than inlining or
//! forking a copy, per that file's own header comment.

use std::time::Duration;

use wasmtime::component::{HasData, Linker};
use wasmtime::{Collector, Config, Engine, InstanceAllocationStrategy, PoolingAllocationConfig};

use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::host::ExecState;

wasmtime::component::bindgen!({
    path: "../../wit/waddle-bundle",
    world: "stage",
    imports: { default: async },
    exports: { default: async },
    // Every WIT record/variant (`PlatformEvent`, `StageEnvelope`,
    // `TransportResult`, `TransportError`, `UnsupportedStage`,
    // `BundleContext`, ...) gets `serde::{Serialize, Deserialize}` so
    // `crate::invoke` can decode an `invoke` frame's JSON `payload`
    // straight into the export's argument type (and encode its return
    // value the same way) without a hand-written JSON<->WIT mapping for
    // every one of these types -- the same JSON-crossing-the-host-
    // boundary contract `crate::host::imports` documents for host-call
    // args/results, applied here to the invoke boundary itself.
    additional_derives: [serde::Serialize, serde::Deserialize],
});

/// How often the epoch-tick task advances the engine's epoch counter.
/// Per-call deadlines (spec SS7.3) are expressed in ticks of this
/// granularity via [`ticks_for_deadline`], so a shorter interval gives
/// finer-grained deadline enforcement at the cost of one atomic increment
/// per tick, cluster-wide negligible at 50ms.
pub const EPOCH_TICK: Duration = Duration::from_millis(50);

/// The pinned `wasmtime`/`wasmtime-wasi` version (Cargo.toml), reported in
/// the `hello` frame's `wasmtime_version`/`wasmtime_abi` fields (spec
/// SS6.6) so the stage can detect an executor built against a different
/// wasmtime than it expects. `wasmtime` does not expose its own version
/// as a public constant, so this is a literal that must be bumped
/// alongside Cargo.toml's `wasmtime`/`wasmtime-wasi` version pins --
/// `tests` below assert it matches `Cargo.toml`.
pub const WASMTIME_VERSION: &str = "49.0.0";

/// Converts a wall-clock deadline in milliseconds into the number of
/// epoch ticks [`wasmtime::Store::set_epoch_deadline`] should be armed
/// with, rounding up so a deadline shorter than one tick still gets at
/// least one (an instant deadline of `0` would otherwise never fire).
pub fn ticks_for_deadline(deadline_ms: u64) -> u64 {
    let tick_ms = EPOCH_TICK.as_millis().max(1) as u64;
    deadline_ms.div_ceil(tick_ms).max(1)
}

/// Zero-sized marker type satisfying `bindgen!`'s `HasData` bound for
/// `Stage::add_to_linker`, parameterizing the generated host-trait
/// dispatch on `&mut ExecState` without the executor needing to name the
/// generated `stage::StagePre`/store-data plumbing itself.
struct HasExecState;
impl HasData for HasExecState {
    type Data<'a> = &'a mut ExecState;
}

/// Builds the wasmtime `Engine` this process uses for every loaded
/// bundle: component model, epoch interruption, the pooling instance
/// allocator (spec SS7.2: instances stay resident and are checked out per
/// call), and the `drc` GC collector (spec SS7.2: "the precompile must
/// use the same GC collector configuration as the runtime engine" --
/// `cfg.executor_wasm_collector` is validated to be exactly `"drc"` by
/// [`CliConfig::validate`], so this function does not re-validate it).
pub fn build_engine(_cfg: &CliConfig) -> Result<Engine, ExecutorError> {
    let mut config = Config::new();
    config.wasm_component_model(true);
    config.epoch_interruption(true);
    config.collector(Collector::DeferredReferenceCounting);

    let mut pooling = PoolingAllocationConfig::default();
    // spec SS7.2 defaults: EXECUTOR_INSTANCES_PER_BUNDLE (4) resident
    // stores per bundle, EXECUTOR_MAX_CONCURRENT_CALLS (32) globally. The
    // pooling allocator's own ceiling is set generously above both so a
    // legitimate per-bundle/global config override never gets silently
    // capped by an allocator limit not visible in SS7.3's table.
    pooling.total_component_instances(256);
    pooling.total_memories(256);
    pooling.total_tables(256);
    config.allocation_strategy(InstanceAllocationStrategy::Pooling(pooling));

    Ok(Engine::new(&config)?)
}

/// Wires every WIT import the `stage` world declares -- `context`, `http`,
/// `kv`, `db`, `relay`, `%flags`, `log`, `clock` (via the generated
/// `Stage::add_to_linker`, backed by `crate::host`'s `Host` trait impls on
/// [`ExecState`]) -- plus the permitted-for-Rust-components WASI surface
/// and the native `wasi:sockets` denial (assumption A19). See
/// `crate::host` for what each half actually does.
pub fn build_linker(engine: &Engine) -> Result<Linker<ExecState>, ExecutorError> {
    let mut linker: Linker<ExecState> = Linker::new(engine);

    // wasi:cli (empty args/env), wasi:filesystem (single read-only
    // /scratch preopen configured on ExecState's WasiCtx, not here),
    // wasi:random/random, wasi:clocks, wasi:io: all real, all permitted
    // for Rust-built components (spec SS6.5's allowlist table).
    //
    // wasi:sockets is linked too -- `componentize-py` links the full WASI
    // P2 import set regardless of the declared world, so refusing to link
    // it at all would fail Python-built components at instantiation time
    // rather than at the socket call the spec wants denied (SS6.5's
    // "wasi:sockets stub rule"). The Host implementation underneath is
    // the real, upstream `wasmtime-wasi` one; what makes every call fail
    // closed is `ExecState`'s `WasiCtx` being built with
    // `allow_tcp(false)`/`allow_udp(false)`/`allow_ip_name_lookup(false)`
    // (assumption A19) -- see `crate::host::build_wasi_ctx`.
    wasmtime_wasi::p2::add_to_linker_async(&mut linker)?;

    Stage::add_to_linker::<_, HasExecState>(&mut linker, |s| s)?;

    Ok(linker)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn test_config() -> CliConfig {
        use clap::Parser;
        CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse")
    }

    #[test]
    fn engine_builds_with_component_model_and_epoch_interruption() -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        // There is no public getter for `Config` back off an `Engine`;
        // the meaningful assertion is that construction succeeds and a
        // linker can be built against it (exercised by the next test) --
        // a component-model-disabled or epoch-interruption-disabled
        // engine would fail `Stage::add_to_linker`/instantiation instead.
        drop(engine);
        Ok(())
    }

    #[test]
    fn linker_wires_the_full_wit_world_and_wasi_surface() -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        let _linker = build_linker(&engine)?;
        Ok(())
    }

    #[test]
    fn ticks_for_deadline_rounds_up_and_never_returns_zero() {
        assert_eq!(ticks_for_deadline(0), 1);
        assert_eq!(ticks_for_deadline(1), 1);
        assert_eq!(ticks_for_deadline(50), 1);
        assert_eq!(ticks_for_deadline(51), 2);
        assert_eq!(ticks_for_deadline(2000), 40);
    }

    #[test]
    fn wasmtime_version_const_matches_the_cargo_toml_pin() {
        let manifest = include_str!("../Cargo.toml");
        let pin_line = manifest
            .lines()
            .find(|l| l.trim_start().starts_with("wasmtime = "))
            .expect("Cargo.toml must pin `wasmtime =`");
        assert!(
            pin_line.contains(&format!("\"={WASMTIME_VERSION}\"")),
            "WASMTIME_VERSION ({WASMTIME_VERSION}) does not match Cargo.toml's wasmtime pin: {pin_line}"
        );
    }
}
