//! wasmtime `Engine`/`Linker` construction against the normative WIT world
//! and the epoch-tick background task that backs the per-call deadline
//! (spec SS7.2/SS7.3, assumption A16).
//!
//! The `bindgen!` invocation below is the executor's only copy of the WIT
//! world's Rust bindings; it points at the committed
//! `wit/waddle-bundle/stage.wit` by relative path rather than inlining or
//! forking a copy, per that file's own header comment.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use wasmtime::component::{HasData, Linker};
use wasmtime::{Collector, Config, Engine, InstanceAllocationStrategy, PoolingAllocationConfig};

use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::host::ExecState;
use crate::manifest::{LinkerCacheKey, VerifiedManifest, WitWorld};

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

/// `world stage-next`'s generated bindings (issue #726: the foundation that
/// lets a bundle CALL a host import added after the frozen `stage` 1.0.0
/// contract). Every interface `stage-next` shares byte-for-byte with `stage`
/// is remapped (`with`) onto the `stage` bindings generated above, so the
/// one `Host` impl per shared interface on [`ExecState`] serves both worlds
/// and only the genuinely new imports (`overlay`, `reputation`, `economy`,
/// `identity`) get their own generated `Host` traits + `add_to_linker`.
pub mod stage_next_world {
    wasmtime::component::bindgen!({
        path: "../../wit/waddle-bundle",
        world: "stage-next",
        imports: { default: async },
        exports: { default: async },
        additional_derives: [serde::Serialize, serde::Deserialize],
        with: {
            "waddle:bundle/types@1.0.0": super::waddle::bundle::types,
            "waddle:bundle/context@1.0.0": super::waddle::bundle::context,
            "waddle:bundle/http@1.0.0": super::waddle::bundle::http,
            "waddle:bundle/kv@1.0.0": super::waddle::bundle::kv,
            "waddle:bundle/db@1.0.0": super::waddle::bundle::db,
            "waddle:bundle/relay@1.0.0": super::waddle::bundle::relay,
            "waddle:bundle/flags@1.0.0": super::waddle::bundle::flags,
            "waddle:bundle/log@1.0.0": super::waddle::bundle::log,
            "waddle:bundle/clock@1.0.0": super::waddle::bundle::clock,
            "waddle:bundle/process-stage@1.0.0": super::exports::waddle::bundle::process_stage,
            "waddle:bundle/action-stage@1.0.0": super::exports::waddle::bundle::action_stage,
        },
    });
}

/// Marker for the `stage-next`-only interfaces' `add_to_linker` (see
/// [`stage_next_world`]).
struct HasStageNextState;
impl HasData for HasStageNextState {
    type Data<'a> = &'a mut ExecState;
}

/// Links the `stage-next`-only host imports (`overlay`, `reputation`, `economy`,
/// `identity`) onto `linker`. A `stage` 1.0.0 component never declares these
/// imports, so registering them is inert for it; a `stage-next` component that
/// imports them instantiates and reaches the real `Host` impls in
/// `crate::host::stage_next_imports` / `stage_next_economy` /
/// `stage_next_identity`. Every call still passes through the stage-side
/// capability gate -- linking is reachability, never authority.
fn link_stage_next_imports(linker: &mut Linker<ExecState>) -> Result<(), ExecutorError> {
    use stage_next_world::waddle::bundle::{economy, identity, overlay, reputation};
    overlay::add_to_linker::<_, HasStageNextState>(linker, |s| s)?;
    reputation::add_to_linker::<_, HasStageNextState>(linker, |s| s)?;
    economy::add_to_linker::<_, HasStageNextState>(linker, |s| s)?;
    identity::add_to_linker::<_, HasStageNextState>(linker, |s| s)?;
    Ok(())
}

/// `waddle:connector@1.0.0`'s generated bindings, isolated in their own
/// module so this second `bindgen!` invocation's `waddle::{bundle,
/// connector}` module tree never collides with the `stage` world's own
/// top-level `waddle::bundle` tree generated just above (spec
/// `docs/superpowers/specs/2026-09-28-connector-bundles.md` S1/S3.2.1).
pub mod connector_world {
    wasmtime::component::bindgen!({
        path: "../../wit/waddle-connector",
        world: "connector",
        imports: { default: async },
        exports: { default: async },
        additional_derives: [serde::Serialize, serde::Deserialize],
    });
}

/// Zero-sized marker type satisfying `bindgen!`'s `HasData` bound for the
/// `connector` world's per-interface `add_to_linker` functions (spec
/// S3.2.1 gate 2: each of `identity`/`http`/`log`/`clock`/`%flags` is
/// linked individually, not via one aggregate `Connector::add_to_linker`
/// call, so `identity` can be omitted when a manifest does not grant
/// `connector.pii.read`).
struct HasConnectorExecState;
impl HasData for HasConnectorExecState {
    type Data<'a> = &'a mut ExecState;
}

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
pub const WASMTIME_VERSION: &str = "49.0.2";

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
    link_stage_next_imports(&mut linker)?;

    Ok(linker)
}

/// Builds a fresh, **per-component** `Linker` that registers only the host
/// functions the `manifest` world declares and, within the `connector`
/// world, only `identity.lookup` when [`VerifiedManifest::may_link_identity`]
/// passes -- replacing [`build_linker`]'s single shared/static instance
/// (spec `docs/superpowers/specs/2026-09-28-connector-bundles.md` S3.2.1
/// gate 2). Called by [`LinkerCache::get_or_build`], never invoked directly
/// on the hot path, so a fresh `Linker` here is amortized by that cache's
/// per-(digest, permission-set) key rather than rebuilt on every
/// instantiation.
///
/// A vendor component (or any component whose compiled bytes import a
/// function this function did not register) fails at `Component`
/// instantiation with wasmtime's standard "unknown import" error -- before
/// any guest code runs, identical failure shape to a `stage`-world
/// component's existing missing-capability behavior.
pub fn build_linker_for(
    engine: &Engine,
    manifest: &VerifiedManifest,
) -> Result<Linker<ExecState>, ExecutorError> {
    let mut linker: Linker<ExecState> = Linker::new(engine);

    // WASI surface is identical across worlds -- the native `wasi:sockets`
    // denial (assumption A19) is configured on `ExecState`'s `WasiCtx`, not
    // gated per-world here.
    wasmtime_wasi::p2::add_to_linker_async(&mut linker)?;

    match manifest.world {
        WitWorld::Stage => {
            Stage::add_to_linker::<_, HasExecState>(&mut linker, |s| s)?;
        }
        WitWorld::StageNext => {
            Stage::add_to_linker::<_, HasExecState>(&mut linker, |s| s)?;
            link_stage_next_imports(&mut linker)?;
        }
        WitWorld::Connector => {
            use connector_world::waddle::bundle::{clock, flags, http, log};
            use connector_world::waddle::connector::identity;

            // Reused `waddle:bundle@1.0.0` interfaces -- always available to
            // a `connector`-world component, same as every `stage` bundle's
            // http/log/clock/flags (spec S1's world declaration). `net.http`
            // host-allowlisting is enforced at the http-call-bridge layer,
            // not at link time, same as the `stage` world today.
            http::add_to_linker::<_, HasConnectorExecState>(&mut linker, |s| s)?;
            log::add_to_linker::<_, HasConnectorExecState>(&mut linker, |s| s)?;
            clock::add_to_linker::<_, HasConnectorExecState>(&mut linker, |s| s)?;
            flags::add_to_linker::<_, HasConnectorExecState>(&mut linker, |s| s)?;

            // Gate 2 (spec S3.2.1): `identity.lookup` links only for a
            // core-namespaced component granted `connector.pii.read`. A
            // component that does not pass this check never gets `identity`
            // registered on its `Linker` at all -- not merely a call that
            // returns `denied` -- so any compiled component that actually
            // imports `identity.lookup` fails to instantiate.
            if manifest.may_link_identity() {
                identity::add_to_linker::<_, HasConnectorExecState>(&mut linker, |s| s)?;
            }

            // `receiver`/`sender` are the world's EXPORTS (the guest
            // implements them) -- nothing to link; the executor calls them
            // post-instantiation via the generated `Connector` accessors,
            // out of this task's scope (host transport wiring, spec S2).
        }
    }

    Ok(linker)
}

/// Caches a built [`Linker`] per `(digest, permission-set)` (task
/// requirement: "linking is cached per (digest, permission-set)") so
/// repeated instantiations of the same bundle version under the same
/// granted-permission set reuse one `Linker` rather than rebuilding it per
/// call. A permission-set change (e.g. a grant revoked) computes a
/// different [`LinkerCacheKey`] and builds a fresh entry -- never mutates
/// or reuses a `Linker` built under a stale grant.
pub struct LinkerCache {
    engine: Engine,
    entries: Mutex<HashMap<LinkerCacheKey, Arc<Linker<ExecState>>>>,
}

impl LinkerCache {
    #[must_use]
    pub fn new(engine: Engine) -> Self {
        Self {
            engine,
            entries: Mutex::new(HashMap::new()),
        }
    }

    /// Returns the cached `Linker` for `manifest`'s `(digest,
    /// permission-set)` key, building and inserting one via
    /// [`build_linker_for`] on a cache miss.
    pub fn get_or_build(
        &self,
        manifest: &VerifiedManifest,
    ) -> Result<Arc<Linker<ExecState>>, ExecutorError> {
        let key = manifest.linker_cache_key();
        // Lock poisoning here would mean a prior panic while holding the
        // lock -- this crate forbids `.unwrap()`/`.expect()` outside tests,
        // so a poisoned mutex falls back to a plain "engine unavailable"
        // error rather than propagating the poison panic further.
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| ExecutorError::Wasmtime("linker cache mutex poisoned".to_string()))?;
        if let Some(existing) = entries.get(&key) {
            return Ok(Arc::clone(existing));
        }
        let linker = Arc::new(build_linker_for(&self.engine, manifest)?);
        entries.insert(key, Arc::clone(&linker));
        Ok(linker)
    }

    /// Number of distinct `(digest, permission-set)` `Linker`s currently
    /// cached -- test-only introspection.
    #[cfg(test)]
    fn len(&self) -> usize {
        #[allow(clippy::unwrap_used)]
        self.entries.lock().unwrap().len()
    }
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

    fn manifest(digest: &str, world: WitWorld, perms: &[&str]) -> VerifiedManifest {
        VerifiedManifest::new(
            "waddles.core.test-app",
            digest,
            world,
            perms.iter().map(|p| p.to_string()).collect(),
        )
    }

    #[test]
    fn build_linker_for_stage_world_succeeds() -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        let m = manifest("sha256:aa", WitWorld::Stage, &[]);
        let _linker = build_linker_for(&engine, &m)?;
        Ok(())
    }

    #[test]
    fn build_linker_for_stage_next_world_succeeds() -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        let m = manifest("sha256:ee", WitWorld::StageNext, &[]);
        let _linker = build_linker_for(&engine, &m)?;
        Ok(())
    }

    #[test]
    fn build_linker_for_connector_world_succeeds_with_and_without_identity_grant(
    ) -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        let granted = manifest(
            "sha256:bb",
            WitWorld::Connector,
            &[crate::manifest::PERM_CONNECTOR_PII_READ],
        );
        let _linker = build_linker_for(&engine, &granted)?;

        let ungranted = manifest("sha256:cc", WitWorld::Connector, &[]);
        let _linker = build_linker_for(&engine, &ungranted)?;
        Ok(())
    }

    /// `LinkerCache` is a genuine per-(digest, permission-set) cache, not a
    /// build-every-time facade -- a cache hit never calls `build_linker_for`
    /// a second time for the same key.
    #[test]
    fn linker_cache_only_builds_once_per_key() -> Result<(), ExecutorError> {
        let engine = build_engine(&test_config())?;
        let cache = LinkerCache::new(engine);
        let m = manifest("sha256:dd", WitWorld::Stage, &[]);

        assert_eq!(cache.len(), 0);
        let a = cache.get_or_build(&m)?;
        assert_eq!(cache.len(), 1);
        let b = cache.get_or_build(&m)?;
        assert_eq!(cache.len(), 1, "a cache hit must not insert a second entry");
        assert!(Arc::ptr_eq(&a, &b));
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
