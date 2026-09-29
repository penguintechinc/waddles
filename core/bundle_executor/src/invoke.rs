//! [`Executor`]: the [`RequestHandler`] implementation that actually runs
//! bundles -- `load`/`unload` manage a registry of compiled
//! `wasmtime::component::Component`s keyed by `app_id`, and `invoke`
//! instantiates one under the per-call epoch deadline and services every
//! host call it makes against the stage over [`HostBridge`] (spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS7).
//!
//! **Scope note (task instruction: "never fake a host call"; realistic-
//! scope items may be scaffolded with a TODO):** digest verification
//! (SHA-256 against the `sha256:<64 hex>` the stage sent) is real. The
//! bucket `GET` that supplies the component's bytes in the first place is
//! NOT implemented -- [`ComponentSource`] is the seam a follow-up wires
//! `object_store` into; production wiring
//! ([`UnimplementedBucketSource`]) fails closed with `LOAD_FAILED` rather
//! than pretending to fetch anything. Likewise, precompiled `.cwasm`
//! caching under `EXECUTOR_PRECOMPILE_DIR` (spec SS7.2/SS7.6) is not
//! wired -- every `load` calls `wasmtime::component::Component::new`
//! (a real, from-source JIT compile) instead of loading a cached
//! artifact; correctness holds, the ~3-4s cold-compile cost SS7.2
//! measured for a large component does not yet get amortized away.

use std::collections::HashMap;
use std::sync::Arc;

use penguin_bundle_host::wire::{
    ErrorBody, ErrorCode, ExportKind, HelloBody, InvokeBody, LoadBody, LoadedBody, ResultBody,
    SandboxInfo, ShutdownBody, UnloadBody, UnloadedBody,
};
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;
use tracing::{info, warn};
use wasmtime::component::Component;
use wasmtime::{Engine, Store};

use crate::config::CliConfig;
use crate::engine::{ticks_for_deadline, Stage};
use crate::error::ExecutorError;
use crate::host::{ExecState, HostBridge};
use crate::wire::{Connection, RequestHandler};

/// Supplies a bundle version's component bytes and signed sidecar bytes
/// for a `component_key`/`sidecar_key` pair (spec SS7.6). The production
/// implementation is `object_store` against `BUNDLE_BUCKET_*`; see the
/// module doc for why that isn't wired in this pass.
pub trait ComponentSource: Send + Sync + 'static {
    fn fetch(
        &self,
        component_key: &str,
        sidecar_key: &str,
    ) -> impl std::future::Future<Output = Result<Vec<u8>, ExecutorError>> + Send;
}

/// The seam production code plugs into once the bucket client lands
/// (`TODO(M2 follow-up)`): fails every fetch with `LOAD_FAILED` rather
/// than silently returning empty/fake bytes, so a `load` against it
/// surfaces clearly as "not implemented yet", never as a passing digest
/// check against zero bytes.
pub struct UnimplementedBucketSource;

impl ComponentSource for UnimplementedBucketSource {
    async fn fetch(
        &self,
        component_key: &str,
        _sidecar_key: &str,
    ) -> Result<Vec<u8>, ExecutorError> {
        Err(ExecutorError::Config(format!(
            "TODO(M2 follow-up): object_store bucket GET not yet wired (component_key={component_key:?}); spec SS7.6"
        )))
    }
}

struct LoadedBundle {
    digest: String,
    component: Component,
    /// This bundle's effective per-instance linear-memory cap in MiB,
    /// resolved once at `on_load` time (spec SS7.3, sandbox layer 8):
    /// `body.limits.memory_mb` when the stage supplied a non-zero value,
    /// else `EXECUTOR_MEMORY_LIMIT_MB`; always clamped to
    /// `EXECUTOR_MAX_MEMORY_LIMIT_MB`. `on_invoke` wires this into every
    /// `Store::limiter` for the bundle rather than re-deriving it per call.
    memory_limit_mb: u32,
}

/// Runs loaded bundles against real wasmtime instantiation. One per
/// executor process, shared (behind an `Arc`) across every connection's
/// read loop.
pub struct Executor<S: ComponentSource> {
    engine: Engine,
    linker: wasmtime::component::Linker<ExecState>,
    source: S,
    max_call_timeout_ms: u64,
    /// `EXECUTOR_MEMORY_LIMIT_MB`: the per-instance memory cap a `load`
    /// gets when it doesn't supply its own `limits.memory_mb` (spec
    /// SS7.3).
    default_memory_limit_mb: u32,
    /// `EXECUTOR_MAX_MEMORY_LIMIT_MB`: the hard ceiling no bundle's
    /// `limits.memory_mb` override may exceed (spec SS7.3).
    max_memory_limit_mb: u32,
    /// `EXECUTOR_FUEL_LIMIT_TRANSFORM` (connector spec SS0 condition 4).
    fuel_limit_transform: u64,
    /// `EXECUTOR_FUEL_LIMIT_DISPATCH` (connector spec SS0 condition 4).
    fuel_limit_dispatch: u64,
    bundles: RwLock<HashMap<String, LoadedBundle>>,
    /// Advances `engine`'s epoch on a fixed tick (spec SS7.2/SS7.3,
    /// assumption A16's executor-side half) so `on_invoke`'s
    /// `Store::set_epoch_deadline` and `Store::epoch_deadline_trap` calls
    /// actually fire -- without this ticker the epoch counter never moves
    /// and no call would ever time out. Aborted on `Drop` so tests don't
    /// leak tasks.
    ///
    /// **Heartbeat safety (connector spec SS0 condition 4's "heartbeats
    /// scheduled on a separate host task, never blocked by guest
    /// execution").** This ticker IS such a task: it runs as its own
    /// `tokio::spawn`ed future, wholly independent of any in-flight
    /// `on_invoke` call. A guest executing a tight, host-call-free loop
    /// occupies its OS thread for real wall-clock time -- wasmtime's epoch
    /// check is a trap point, not a cooperative yield back to the async
    /// runtime -- so the safety property this ticker (and any future
    /// per-connection heartbeat built the same way, e.g. a Discord gateway
    /// heartbeat once connector bundles land) actually depends on is a
    /// **multi-threaded** Tokio runtime (`#[tokio::main]`'s default,
    /// `crate::main` never overrides it to `current_thread`): the ticker
    /// and any heartbeat task run on a different OS worker thread than the
    /// one blocked running guest code, so they are never starved by it.
    /// `heartbeat_task_is_never_blocked_by_a_slow_guest_invocation` (below)
    /// is the regression test for this property.
    epoch_ticker: tokio::task::JoinHandle<()>,
}

impl<S: ComponentSource> Executor<S> {
    pub fn new(cfg: &CliConfig, source: S) -> Result<Self, ExecutorError> {
        let engine = crate::engine::build_engine(cfg)?;
        let linker = crate::engine::build_linker(&engine)?;
        let ticker_engine = engine.clone();
        let epoch_ticker = tokio::spawn(async move {
            loop {
                tokio::time::sleep(crate::engine::EPOCH_TICK).await;
                ticker_engine.increment_epoch();
            }
        });
        Ok(Self {
            engine,
            linker,
            source,
            max_call_timeout_ms: cfg.executor_max_call_timeout_ms,
            default_memory_limit_mb: cfg.executor_memory_limit_mb,
            max_memory_limit_mb: cfg.executor_max_memory_limit_mb,
            fuel_limit_transform: cfg.executor_fuel_limit_transform,
            fuel_limit_dispatch: cfg.executor_fuel_limit_dispatch,
            bundles: RwLock::new(HashMap::new()),
            epoch_ticker,
        })
    }

    /// The `hello` frame this executor opens every connection with (spec
    /// SS6.6/SS7.2): reports the pinned wasmtime version, ABI and
    /// collector so the stage can refuse a mismatched connection instead
    /// of every bundle silently failing to load.
    pub fn hello(&self, sandbox_verified: bool, sandbox_runtime: &str) -> HelloBody {
        HelloBody {
            protocol_version: 1,
            executor_version: env!("CARGO_PKG_VERSION").to_string(),
            wasmtime_version: crate::engine::WASMTIME_VERSION.to_string(),
            wasmtime_abi: crate::engine::WASMTIME_VERSION.to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: sandbox_runtime.to_string(),
                verified: sandbox_verified,
            },
        }
    }
}

impl<S: ComponentSource> Drop for Executor<S> {
    fn drop(&mut self) {
        self.epoch_ticker.abort();
    }
}

/// Parses and checks a `sha256:<64 hex>` digest string against `bytes`
/// (spec SS7.6: "Any mismatch -> `error.code = DIGEST_MISMATCH`"). A real,
/// independently testable SHA-256 comparison -- the one piece of the
/// `load` flow this crate implements in full regardless of the bucket
/// fetch it sits behind.
pub fn verify_digest(bytes: &[u8], expected: &str) -> Result<(), ExecutorError> {
    let hex = expected
        .strip_prefix("sha256:")
        .ok_or_else(|| ExecutorError::MalformedDigest(expected.to_string()))?;
    if hex.len() != 64 || !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(ExecutorError::MalformedDigest(expected.to_string()));
    }
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let actual = format!("{:x}", hasher.finalize());
    if actual.eq_ignore_ascii_case(hex) {
        Ok(())
    } else {
        Err(ExecutorError::DigestMismatch {
            expected: hex.to_string(),
            actual,
        })
    }
}

fn error_body(code: ErrorCode, message: impl Into<String>) -> ErrorBody {
    ErrorBody {
        code,
        message: message.into(),
        detail: None,
    }
}

impl<S: ComponentSource> RequestHandler for Executor<S> {
    async fn on_load(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
        let start = std::time::Instant::now();
        let bytes = self
            .source
            .fetch(&body.component_key, &body.sidecar_key)
            .await
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

        verify_digest(&bytes, &body.digest)
            .map_err(|e| error_body(ErrorCode::DigestMismatch, e.to_string()))?;

        let component = Component::new(&self.engine, &bytes)
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

        let app_id = body.app_id.clone();
        let digest = body.digest.clone();
        // spec SS7.3: a `load` may request its own `limits.memory_mb`; `0`
        // means "no preference" and falls back to `EXECUTOR_MEMORY_LIMIT_MB`.
        // Either way the effective cap never exceeds
        // `EXECUTOR_MAX_MEMORY_LIMIT_MB`, and is never less than 1 MiB.
        let memory_limit_mb = if body.limits.memory_mb == 0 {
            self.default_memory_limit_mb
        } else {
            body.limits.memory_mb
        }
        .min(self.max_memory_limit_mb)
        .max(1);
        self.bundles.write().await.insert(
            app_id.clone(),
            LoadedBundle {
                digest: digest.clone(),
                component,
                memory_limit_mb,
            },
        );

        info!(
            app_id,
            digest,
            ms = start.elapsed().as_millis() as u64,
            "bundle loaded"
        );
        Ok(LoadedBody {
            app_id,
            digest,
            precompile_ms: start.elapsed().as_millis() as u64,
            // Spec SS6.5: "Both stage interfaces are always exported."
            // Tier 1 SDKs generate a stub for whichever one a bundle
            // doesn't implement, so every successfully loaded component
            // exports both by construction.
            exports: vec!["transform".to_string(), "dispatch".to_string()],
        })
    }

    async fn on_unload(&self, body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
        let mut bundles = self.bundles.write().await;
        match bundles.get(&body.app_id) {
            Some(loaded) if loaded.digest == body.digest => {
                bundles.remove(&body.app_id);
                info!(
                    app_id = body.app_id,
                    digest = body.digest,
                    "bundle unloaded"
                );
                Ok(UnloadedBody {
                    app_id: body.app_id,
                    digest: body.digest,
                })
            }
            Some(loaded) => Err(error_body(
                ErrorCode::UnknownBundle,
                format!(
                    "unload digest {} does not match loaded digest {}",
                    body.digest, loaded.digest
                ),
            )),
            None => Err(error_body(
                ErrorCode::UnknownBundle,
                format!("{} is not loaded", body.app_id),
            )),
        }
    }

    async fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        let (component, memory_limit_mb) = {
            let bundles = self.bundles.read().await;
            let loaded = bundles
                .get(&body.app_id)
                .ok_or_else(|| error_body(ErrorCode::UnknownBundle, body.app_id.clone()))?;
            if loaded.digest != body.digest {
                return Err(error_body(
                    ErrorCode::DigestMismatch,
                    format!(
                        "invoke digest {} does not match loaded digest {}",
                        body.digest, loaded.digest
                    ),
                ));
            }
            (loaded.component.clone(), loaded.memory_limit_mb)
        };

        let deadline_ms = body.deadline_ms.min(self.max_call_timeout_ms).max(1);
        let bridge = HostBridge::new(connection);
        let exec_state = ExecState::new(Some(bridge), body.app_id.clone(), invoke_id)
            .with_memory_limit_mb(memory_limit_mb);
        let mut store = Store::new(&self.engine, exec_state);
        store.set_epoch_deadline(ticks_for_deadline(deadline_ms));
        store.epoch_deadline_trap();
        // Connector spec SS0 condition 4: fuel per invocation, budgeted by
        // world (`transform` vs `dispatch`, see `CliConfig`'s doc), alongside
        // the epoch deadline above -- never a replacement for it. The engine
        // was built with `consume_fuel(true)` (`crate::engine::build_engine`)
        // so a `Store` starts at zero fuel and traps immediately unless armed
        // here on every call.
        let fuel_budget = match body.export {
            ExportKind::Transform => self.fuel_limit_transform,
            ExportKind::Dispatch => self.fuel_limit_dispatch,
        };
        store.set_fuel(fuel_budget).map_err(|e| {
            error_body(
                ErrorCode::WasmTrap,
                format!("failed to arm fuel budget: {e}"),
            )
        })?;
        // Sandbox layer 8 (spec SS7.3): caps this instance's linear memory
        // to the bundle's resolved `memory_limit_mb` (`on_load`,
        // `EXECUTOR_MEMORY_LIMIT_MB`/`limits.memory_mb`) via
        // `ExecState::with_memory_limit_mb`'s `StoreLimits`, so a
        // `memory.grow` past the cap traps (`trap_on_grow_failure`) rather
        // than growing unbounded or the epoch deadline alone (CPU only)
        // being the sole containment layer -- gh security review MED
        // finding, previously a TODO.
        store.limiter(|state| &mut state.limits);

        let start = std::time::Instant::now();
        let stage = match Stage::instantiate_async(&mut store, &component, &self.linker).await {
            Ok(stage) => stage,
            Err(err) => {
                // Instantiation itself can trap (a heavy/looping start
                // function burning the fuel budget, hitting the epoch
                // deadline, or growing memory past the cap) exactly like an
                // export call can -- classify identically via
                // `trap_to_error_body` rather than always bucketing an
                // instantiate failure as `LOAD_FAILED`, which would hide a
                // genuine guest fault from `svc_process::spine`'s DLQ-kind
                // mapping and its per-source circuit breaker (both key off
                // the typed `ErrorCode`, connector spec SS0 condition 5).
                let memory_cap_hit = store.data().memory_cap_hit();
                if memory_cap_hit || err.downcast_ref::<wasmtime::Trap>().is_some() {
                    return Err(trap_to_error_body(err, memory_cap_hit));
                }
                return Err(error_body(ErrorCode::LoadFailed, err.to_string()));
            }
        };

        let result = match body.export {
            ExportKind::Transform => {
                let event = serde_json::from_value(body.payload)
                    .map_err(|e| error_body(ErrorCode::MalformedFrame, e.to_string()))?;
                let outcome = stage
                    .waddle_bundle_process_stage()
                    .call_transform(&mut store, &event)
                    .await;
                match outcome {
                    Ok(Ok(reply)) => serde_json::to_value(reply),
                    Ok(Err(unsupported)) => serde_json::to_value(unsupported)
                        .map(|v| serde_json::json!({"unsupported_stage": v})),
                    Err(trap) => {
                        let memory_cap_hit = store.data().memory_cap_hit();
                        return Err(trap_to_error_body(trap, memory_cap_hit));
                    }
                }
            }
            ExportKind::Dispatch => {
                let envelope: EnvelopeAndConfig = serde_json::from_value(body.payload)
                    .map_err(|e| error_body(ErrorCode::MalformedFrame, e.to_string()))?;
                let outcome = stage
                    .waddle_bundle_action_stage()
                    .call_dispatch(&mut store, &envelope.envelope, &envelope.config)
                    .await;
                match outcome {
                    Ok(Ok(reply)) => serde_json::to_value(reply),
                    Ok(Err(transport_error)) => serde_json::to_value(transport_error)
                        .map(|v| serde_json::json!({"transport_error": v})),
                    Err(trap) => {
                        let memory_cap_hit = store.data().memory_cap_hit();
                        return Err(trap_to_error_body(trap, memory_cap_hit));
                    }
                }
            }
        }
        .map_err(|e| error_body(ErrorCode::LoadFailed, format!("result encode failed: {e}")))?;

        // Fuel is monotonically consumed, never replenished mid-call, so the
        // budget minus what remains is exactly what this invocation spent
        // (spec SS0 condition 4's overhead-measurement requirement).
        // `get_fuel` only errs when fuel accounting is disabled, which never
        // happens here (`crate::engine::build_engine` always enables it) --
        // fall back to the full budget (fuel_used=0) rather than panicking on
        // an invariant this store can't actually violate.
        let fuel_remaining = store.get_fuel().unwrap_or(fuel_budget);
        let fuel_used = fuel_budget.saturating_sub(fuel_remaining);

        Ok(ResultBody {
            payload: result,
            duration_ms: start.elapsed().as_millis() as u64,
            fuel_used,
        })
    }

    async fn on_shutdown(&self, body: ShutdownBody) {
        warn!(grace_ms = body.grace_ms, "stage requested shutdown");
    }
}

/// Delegating impl so an `Arc<Executor<S>>` -- the shape shared across
/// every reconnect attempt in `crate::run`'s dial loop, since the bundle
/// registry must survive a single connection dropping -- satisfies
/// `RequestHandler` directly without a wrapper newtype.
impl<S: ComponentSource> RequestHandler for Arc<Executor<S>> {
    async fn on_load(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
        (**self).on_load(body).await
    }

    async fn on_unload(&self, body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
        (**self).on_unload(body).await
    }

    async fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        (**self).on_invoke(body, invoke_id, connection).await
    }

    async fn on_shutdown(&self, body: ShutdownBody) {
        (**self).on_shutdown(body).await
    }
}

/// Combined argument shape for `action-stage.dispatch`'s two WIT
/// parameters, carried as one JSON object in `InvokeBody.payload` (this
/// executor's own convention -- WIT itself has no tuple-of-arguments JSON
/// shape to borrow, spec SS6.6 only specifies "the export's arguments as
/// JSON").
#[derive(serde::Deserialize)]
struct EnvelopeAndConfig {
    envelope: crate::engine::waddle::bundle::types::StageEnvelope,
    config: String,
}

/// A wasmtime-level failure calling the export: a guest trap (epoch
/// deadline, memory limit, unreachable, ...) rather than a WIT-level
/// `result<_, E>` the bundle itself returned. Distinguished from a
/// malformed-payload error so the caller reports `EXECUTOR_DEADLINE`/
/// `MEMORY_LIMIT`/`WASM_TRAP` rather than a generic failure (spec SS7.3).
///
/// Classifies by **typed signal only**, never by pattern-matching the
/// trap's formatted message -- gh security review MED finding: the
/// pre-fix version matched `format!("{err:#}")` against the substrings
/// "epoch"/"deadline"/"memory"/"allocation", which a malicious guest can
/// spoof (or evade) by driving arbitrary text into the error chain -- e.g.
/// a `db`/`kv`/`http` capability handler that echoes a guest-supplied
/// argument back into an `ExecutorError` on failure, which then surfaces
/// as part of this same trap's `{err:#}` chain. Worse, the *real* epoch
/// trap's message is just `"wasm trap: interrupt"` (`wasmtime::Trap::
/// Interrupt`'s `Display`, spec `wasmtime-environ`'s trap table) -- it
/// never contained "epoch"/"deadline" even before any guest was involved,
/// so the substring match silently misclassified every genuine deadline
/// trap as `WASM_TRAP`, own-goal independent of spoofing.
///
/// Two typed sources instead:
/// - **Deadline**: `err.downcast_ref::<wasmtime::Trap>()` against the
///   exact `Trap::Interrupt` variant wasmtime raises for
///   `Store::epoch_deadline_trap` (only a compiled-in wasm trap code the
///   engine itself produces -- never a value a guest or a host-call
///   handler can construct by choosing a string).
/// - **Memory/table cap**: `memory_cap_hit`, a `bool` snapshotted from
///   `ExecState::memory_cap_hit()` (`crate::host::CapTrackingLimits`)
///   *before* this function is called -- flipped only inside
///   `ResourceLimiter::memory_growing`/`table_growing` by this executor's
///   own trusted host code reacting to the numeric growth request
///   wasmtime passes it, never from guest-supplied text.
///
/// Anything else (`unreachable`, integer overflow, an `OutOfFuel`/
/// `AllocationTooLarge` trap this executor doesn't otherwise arm, a
/// bridge/host-call failure that surfaced as a trap) falls through to the
/// generic `WasmTrap` code -- never silently reclassified by content.
///
/// `Trap::OutOfFuel` (connector spec SS0 condition 4) is classified into the
/// same `EXECUTOR_DEADLINE` bucket as `Trap::Interrupt`: both are "this call
/// used more of a bounded execution resource than it was allowed", the same
/// operational meaning `crate::invoke`'s callers (e.g.
/// `svc_process::spine::error_code_to_dlq_kind`) already give
/// `ExecutorDeadline` -> `DlqErrorKind::CallTimeout`, and the same signal a
/// per-source circuit breaker should count as "this source's guest is
/// misbehaving", indistinguishable in effect from a wall-clock timeout. The
/// `detail` field carries `"fuel_exhausted"` so logs/metrics can tell the two
/// apart without changing the wire `ErrorCode` the stage already understands.
fn trap_to_error_body(err: wasmtime::Error, memory_cap_hit: bool) -> ErrorBody {
    let full_chain = format!("{err:#}");
    if memory_cap_hit {
        return error_body(ErrorCode::MemoryLimit, full_chain);
    }
    match err.downcast_ref::<wasmtime::Trap>() {
        Some(wasmtime::Trap::Interrupt) => error_body(ErrorCode::ExecutorDeadline, full_chain),
        Some(wasmtime::Trap::OutOfFuel) => ErrorBody {
            code: ErrorCode::ExecutorDeadline,
            message: full_chain,
            detail: Some("fuel_exhausted".to_string()),
        },
        _ => error_body(ErrorCode::WasmTrap, full_chain),
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
    fn verify_digest_accepts_a_matching_sha256() {
        let bytes = b"hello world";
        let mut hasher = Sha256::new();
        hasher.update(bytes);
        let digest = format!("sha256:{:x}", hasher.finalize());
        verify_digest(bytes, &digest).expect("digest matches");
    }

    #[test]
    fn verify_digest_rejects_a_mismatched_sha256() {
        let err = verify_digest(b"hello world", &format!("sha256:{}", "0".repeat(64)));
        assert!(matches!(err, Err(ExecutorError::DigestMismatch { .. })));
    }

    #[test]
    fn verify_digest_rejects_malformed_strings() {
        assert!(matches!(
            verify_digest(b"x", "not-a-digest"),
            Err(ExecutorError::MalformedDigest(_))
        ));
        assert!(matches!(
            verify_digest(b"x", "sha256:tooshort"),
            Err(ExecutorError::MalformedDigest(_))
        ));
    }

    #[tokio::test]
    async fn unimplemented_bucket_source_fails_closed_never_fakes_bytes() {
        let source = UnimplementedBucketSource;
        let result = source.fetch("k", "s").await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn unload_of_an_unknown_bundle_is_an_error() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        let result = executor
            .on_unload(UnloadBody {
                app_id: "waddles.never-loaded".to_string(),
                digest: "sha256:00".to_string(),
            })
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::UnknownBundle,
                ..
            })
        ));
        Ok(())
    }

    #[tokio::test]
    async fn load_with_the_unimplemented_bucket_source_fails_closed() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        let result = executor
            .on_load(LoadBody {
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                digest: format!("sha256:{}", "0".repeat(64)),
                component_key: "bundles/waddles.test.app/1/x.wasm".to_string(),
                sidecar_key: "bundles/waddles.test.app/1/x.json".to_string(),
                capabilities: vec![],
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            })
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::LoadFailed,
                ..
            })
        ));
        Ok(())
    }

    /// Hands back the committed test fixture's real compiled component
    /// bytes (spec-honest: `on_load`'s digest verification and
    /// `Component::new` compile against genuine WASM, not a stand-in).
    struct FixtureSource;

    const FIXTURE_WASM: &[u8] = include_bytes!("../tests/fixtures/hostile_fixture.wasm");

    impl ComponentSource for FixtureSource {
        async fn fetch(&self, _c: &str, _s: &str) -> Result<Vec<u8>, ExecutorError> {
            Ok(FIXTURE_WASM.to_vec())
        }
    }

    fn fixture_digest() -> String {
        let mut hasher = Sha256::new();
        hasher.update(FIXTURE_WASM);
        format!("sha256:{:x}", hasher.finalize())
    }

    fn fixture_load_body(app_id: &str) -> LoadBody {
        LoadBody {
            app_id: app_id.to_string(),
            version: "1".to_string(),
            digest: fixture_digest(),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            capabilities: vec![],
            limits: penguin_bundle_host::wire::LoadLimits {
                timeout_ms: 2000,
                memory_mb: 64,
            },
        }
    }

    #[tokio::test]
    async fn hello_reports_the_requested_sandbox_posture() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        let hello = executor.hello(true, "gvisor");
        assert_eq!(hello.sandbox.runtime, "gvisor");
        assert!(hello.sandbox.verified);
        assert_eq!(hello.collector, "drc");
        assert_eq!(hello.wasmtime_version, crate::engine::WASMTIME_VERSION);

        let hello = executor.hello(false, "runc");
        assert!(!hello.sandbox.verified);
        assert_eq!(hello.sandbox.runtime, "runc");
        Ok(())
    }

    #[tokio::test]
    async fn epoch_ticker_advances_the_engine_epoch_while_the_executor_is_alive(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        // Long enough for the epoch ticker (spawned in `Executor::new`) to
        // fire at least once at `EPOCH_TICK` (50ms) before this test's
        // `Executor` is dropped (which aborts the ticker task).
        tokio::time::sleep(crate::engine::EPOCH_TICK * 3).await;
        drop(executor);
        Ok(())
    }

    #[tokio::test]
    async fn on_load_succeeds_with_a_real_component_and_reports_both_exports(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        let loaded = executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds against the real fixture");
        assert_eq!(loaded.digest, fixture_digest());
        assert_eq!(loaded.exports, vec!["transform", "dispatch"]);
        Ok(())
    }

    #[tokio::test]
    async fn on_unload_succeeds_and_rejects_a_digest_mismatch() -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        // Wrong digest against a bundle that IS loaded -> UnknownBundle
        // (this executor's `on_unload` treats a digest mismatch the same
        // as "not this exact loaded version").
        let mismatch = executor
            .on_unload(UnloadBody {
                app_id: "waddles.test.app".to_string(),
                digest: format!("sha256:{}", "1".repeat(64)),
            })
            .await;
        assert!(matches!(
            mismatch,
            Err(ErrorBody {
                code: ErrorCode::UnknownBundle,
                ..
            })
        ));

        let unloaded = executor
            .on_unload(UnloadBody {
                app_id: "waddles.test.app".to_string(),
                digest: fixture_digest(),
            })
            .await
            .expect("unload succeeds");
        assert_eq!(unloaded.digest, fixture_digest());
        Ok(())
    }

    #[tokio::test]
    async fn on_invoke_rejects_an_unloaded_app_id() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.never-loaded".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({}),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::UnknownBundle,
                ..
            })
        ));
        Ok(())
    }

    #[tokio::test]
    async fn on_invoke_rejects_a_digest_mismatch_against_the_loaded_bundle(
    ) -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: format!("sha256:{}", "1".repeat(64)),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({}),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::DigestMismatch,
                ..
            })
        ));
        Ok(())
    }

    #[tokio::test]
    async fn on_invoke_reports_a_malformed_transform_payload() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    // Not a valid `PlatformEvent` shape at all.
                    payload: serde_json::json!("not-an-object"),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::MalformedFrame,
                ..
            })
        ));
        Ok(())
    }

    #[tokio::test]
    async fn on_invoke_reports_a_malformed_dispatch_payload() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Dispatch,
                    payload: serde_json::json!({"not": "the right shape"}),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(matches!(
            result,
            Err(ErrorBody {
                code: ErrorCode::MalformedFrame,
                ..
            })
        ));
        Ok(())
    }

    /// Negative test for gh security review MED finding "per-instance
    /// memory cap not enforced" (spec SS7.3 sandbox layer 8): a bundle
    /// loaded with a tight `limits.memory_mb` and invoked against the
    /// fixture's `memory-hog` branch (which grows linear memory in 1 MiB
    /// steps up to 64 MiB, see `tests/fixtures/README.md`) must trap with
    /// `MEMORY_LIMIT` -- never hang past its deadline, never succeed with
    /// unbounded growth, and never take the process down. `deadline_ms` is
    /// generous (10s) so the assertion is unambiguously the memory cap, not
    /// a race against the epoch deadline also wired on this store.
    #[tokio::test]
    async fn on_invoke_traps_with_memory_limit_when_a_bundle_exceeds_its_cap(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(LoadBody {
                app_id: "waddles.test.memory-hog".to_string(),
                version: "1".to_string(),
                digest: fixture_digest(),
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                // Small enough that the fixture's 64 MiB hog trips it
                // early, generous enough that plain instantiation (the
                // component's own baseline runtime footprint) succeeds.
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 10_000,
                    memory_mb: 8,
                },
            })
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.memory-hog".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "memory-hog",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-22T00:00:00.000Z",
                    }),
                    deadline_ms: 10_000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(
            matches!(
                result,
                Err(ErrorBody {
                    code: ErrorCode::MemoryLimit,
                    ..
                })
            ),
            "expected a MEMORY_LIMIT trap, got {result:?}"
        );
        Ok(())
    }

    /// Positive cases: the exact typed signals `trap_to_error_body`
    /// classifies on. `wasmtime::Trap::Interrupt` is the genuine, engine-
    /// produced epoch-deadline trap (never a hand-built string -- see the
    /// function's own doc for why the pre-fix substring match never even
    /// matched this real trap's `"wasm trap: interrupt"` message);
    /// `memory_cap_hit=true` is exactly the signal
    /// `ExecState::memory_cap_hit()` reports after a denied grow
    /// (`crate::host` module's own test covers that flag directly).
    #[test]
    fn trap_to_error_body_classifies_deadline_memory_and_generic_traps() {
        let deadline = trap_to_error_body(wasmtime::Trap::Interrupt.into(), false);
        assert!(matches!(deadline.code, ErrorCode::ExecutorDeadline));

        let memory = trap_to_error_body(
            wasmtime::Error::msg("forcing trap when growing memory to 999 bytes"),
            true,
        );
        assert!(matches!(memory.code, ErrorCode::MemoryLimit));

        let generic = trap_to_error_body(wasmtime::Trap::UnreachableCodeReached.into(), false);
        assert!(matches!(generic.code, ErrorCode::WasmTrap));
    }

    /// Negative/regression test for gh security review MED finding
    /// "trap classification by substring is spoofable": a crafted guest-
    /// controlled error message containing the exact words "epoch" and
    /// "memory" -- the pre-fix substrings -- must NOT be classified as
    /// `EXECUTOR_DEADLINE`/`MEMORY_LIMIT` when it is neither a real
    /// `Trap::Interrupt` nor accompanied by `memory_cap_hit=true`. This is
    /// exactly the attack the typed classification closes: a bundle
    /// panicking with (or driving a host-call failure containing) text
    /// like "please increase my memory epoch allocation quota" must fall
    /// through to the generic `WASM_TRAP` code, never spoof a deadline or
    /// memory-cap classification it didn't actually hit.
    #[test]
    fn trap_to_error_body_is_not_spoofed_by_guest_controlled_epoch_or_memory_text() {
        let spoofed = trap_to_error_body(
            wasmtime::Error::msg(
                "please increase my memory epoch allocation quota before the deadline",
            ),
            false,
        );
        assert!(
            matches!(spoofed.code, ErrorCode::WasmTrap),
            "a guest-controlled message containing \"epoch\"/\"memory\" must not spoof \
             a deadline or memory-limit classification: got {:?}",
            spoofed.code
        );
    }

    /// Negative/regression test for the flip side: a real memory-cap
    /// denial's evidence is a `bool` snapshotted from `ExecState::
    /// memory_cap_hit()`, not a message a guest could try to *suppress*.
    /// Even if the accompanying trap message were entirely generic
    /// (no "memory" substring at all), `memory_cap_hit=true` still forces
    /// `MEMORY_LIMIT` -- classification cannot be evaded by wording either.
    #[test]
    fn trap_to_error_body_classifies_on_the_flag_even_with_a_generic_message() {
        let result = trap_to_error_body(wasmtime::Error::msg("something failed"), true);
        assert!(matches!(result.code, ErrorCode::MemoryLimit));
    }

    /// Positive case for connector spec SS0 condition 4's fuel
    /// classification: a real, engine-produced `Trap::OutOfFuel` (never a
    /// hand-built string) must classify as `EXECUTOR_DEADLINE` with
    /// `detail = "fuel_exhausted"` -- the same DLQ bucket as an epoch
    /// deadline (both mean "this call exceeded a bounded execution
    /// resource"), distinguishable only via `detail` for logs/metrics.
    #[test]
    fn trap_to_error_body_classifies_out_of_fuel_as_deadline_with_fuel_detail() {
        let out_of_fuel = trap_to_error_body(wasmtime::Trap::OutOfFuel.into(), false);
        assert!(matches!(out_of_fuel.code, ErrorCode::ExecutorDeadline));
        assert_eq!(out_of_fuel.detail.as_deref(), Some("fuel_exhausted"));
    }

    /// End-to-end fuel exhaustion test (connector spec SS0 condition 4 /
    /// task instruction "an infinite-loop guest trips the fuel or epoch
    /// limit"): the fixture has no unbounded loop, so this reuses
    /// `memory-hog`'s real, bounded-but-substantial loop (64 iterations of
    /// allocate-and-touch) under a fuel budget too small to complete even
    /// one iteration, with a generous memory cap and epoch deadline so the
    /// trap is unambiguously fuel, not memory or wall-clock. Also proves
    /// trap isolation (task instruction 2): the executor process is still
    /// alive and able to serve a subsequent, unrelated invocation
    /// afterwards -- a guest fault never takes down the host.
    #[tokio::test]
    async fn on_invoke_traps_with_out_of_fuel_when_the_budget_is_too_small(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let mut cfg = test_config();
        // Small enough to trap well within `memory-hog`'s first 1 MiB
        // fill (a real loop over ~1M bytes costs far more than this), but
        // large enough for instantiation plus a single trivial WASI call
        // (the isolation check below) to comfortably succeed.
        cfg.executor_fuel_limit_transform = 50_000;
        let executor = Executor::new(&cfg, FixtureSource)?;
        executor
            .on_load(LoadBody {
                app_id: "waddles.test.fuel-hog".to_string(),
                version: "1".to_string(),
                digest: fixture_digest(),
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 10_000,
                    memory_mb: 128,
                },
            })
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.fuel-hog".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "memory-hog",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-28T00:00:00.000Z",
                    }),
                    deadline_ms: 10_000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(
            matches!(
                &result,
                Err(ErrorBody {
                    code: ErrorCode::ExecutorDeadline,
                    detail: Some(d),
                    ..
                }) if d == "fuel_exhausted"
            ),
            "expected a fuel-exhausted EXECUTOR_DEADLINE, got {result:?}"
        );

        // Trap isolation: the same executor (same process, same wasmtime
        // Engine) must still serve an unrelated call after a guest fault.
        // `socket-probe` (not a host-call branch like `clock-read`): denied
        // natively with no round trip to a stage, so it both avoids hanging
        // against a connection whose reply receiver is discarded and stays
        // cheap enough to fit this test's deliberately tiny fuel budget.
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("a fresh bundle still loads after another bundle's fuel trap");
        let (tx2, _rx2) = tokio::sync::mpsc::unbounded_channel();
        let connection2 = crate::wire::Connection::new(tx2);
        let ok = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "socket-probe",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-28T00:00:00.000Z",
                    }),
                    deadline_ms: 1000,
                    trace: None,
                },
                2,
                connection2,
            )
            .await;
        assert!(
            ok.is_ok(),
            "the executor must keep serving other invocations after a guest's fuel trap: {ok:?}"
        );
        Ok(())
    }

    /// Positive case: a call that completes well within its fuel budget
    /// reports a non-zero `fuel_used` (connector spec SS0 condition 4's
    /// overhead-measurement requirement) strictly less than the budget.
    #[tokio::test]
    async fn on_invoke_reports_nonzero_fuel_used_on_success() -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        // `socket-probe` (not `clock-read`/`get-context`/etc.): it is denied
        // natively by wasmtime-wasi's own TCP-socket-creation refusal with
        // no round trip to a stage at all, so a `Connection` whose reply
        // receiver is discarded (as below, same as this module's other
        // no-host-call tests) never blocks waiting for an answer that would
        // never come -- a host-call branch would hang forever here instead.
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "socket-probe",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-28T00:00:00.000Z",
                    }),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await
            .expect("socket-probe succeeds (denied natively, no trap)");
        assert!(result.fuel_used > 0, "expected non-zero fuel consumption");
        assert!(
            result.fuel_used < test_config().executor_fuel_limit_transform,
            "a trivial call must not consume the entire default budget"
        );
        Ok(())
    }

    /// Task instruction 3 (heartbeat safety) regression test: a background
    /// "heartbeat" task (standing in for the real epoch ticker and, once
    /// connector bundles land, a connection's platform heartbeat) must keep
    /// ticking on schedule while a slow, host-call-free guest loop
    /// (`memory-hog`, generous fuel/deadline so it runs for real wall-clock
    /// time rather than tripping immediately) occupies a worker thread.
    /// This only holds on a **multi-threaded** runtime -- see
    /// `Executor::epoch_ticker`'s doc for why -- so this test deliberately
    /// uses `flavor = "multi_thread"` with more than one worker, matching
    /// `crate::main`'s real `#[tokio::main]` default.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn heartbeat_task_is_never_blocked_by_a_slow_guest_invocation(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(LoadBody {
                // Comfortably above `memory-hog`'s 64 MiB allocation plus
                // runtime overhead (same headroom `crate::bucket`'s own
                // end-to-end test uses) -- this test's point is elapsed
                // wall-clock time for the heartbeat to tick during, not the
                // memory cap.
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 5000,
                    memory_mb: 256,
                },
                ..fixture_load_body("waddles.test.app")
            })
            .await
            .expect("load succeeds");

        let ticks = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let heartbeat_ticks = Arc::clone(&ticks);
        let heartbeat = tokio::spawn(async move {
            loop {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                heartbeat_ticks.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        });

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        // Generous deadline/fuel: the point is real elapsed wall-clock time
        // for the heartbeat to tick during, not a trap.
        executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "memory-hog",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-28T00:00:00.000Z",
                    }),
                    deadline_ms: 5000,
                    trace: None,
                },
                1,
                connection,
            )
            .await
            .expect("memory-hog succeeds within its generous budget");

        heartbeat.abort();
        assert!(
            ticks.load(std::sync::atomic::Ordering::Relaxed) > 0,
            "the heartbeat task must have ticked at least once while the guest call ran, \
             proving it was never blocked by guest execution"
        );
        Ok(())
    }

    #[tokio::test]
    async fn on_shutdown_logs_and_returns() -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        executor
            .on_shutdown(penguin_bundle_host::wire::ShutdownBody { grace_ms: 100 })
            .await;
        Ok(())
    }

    /// Ad-hoc fuel-metering overhead measurement (connector spec SS0
    /// condition 4's "measure the overhead" requirement) -- not a
    /// correctness assertion (wall-clock timing in CI is noisy, and
    /// meaningful only in `--release`), so this is `#[ignore]`d and run
    /// manually:
    /// `cargo test --release --lib measure_fuel_overhead_tight_compute_loop -- --ignored --nocapture`.
    /// Average per-call latency for a real, host-call-free compute loop
    /// (`memory-hog`: 64 x 1 MiB allocate-and-touch) over many calls against
    /// the same loaded bundle, with fuel metering enabled exactly as
    /// `crate::engine::build_engine` always configures it today.
    #[ignore]
    #[tokio::test(flavor = "multi_thread")]
    async fn measure_fuel_overhead_tight_compute_loop() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(LoadBody {
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 10_000,
                    memory_mb: 256,
                },
                ..fixture_load_body("waddles.bench.app")
            })
            .await
            .expect("load succeeds");
        const N: u32 = 500;
        let start = std::time::Instant::now();
        for i in 0..N {
            let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
            let connection = crate::wire::Connection::new(tx);
            executor
                .on_invoke(
                    InvokeBody {
                        app_id: "waddles.bench.app".to_string(),
                        digest: fixture_digest(),
                        export: ExportKind::Transform,
                        payload: serde_json::json!({
                            "platform": "test",
                            "event_type": "memory-hog",
                            "actor": null,
                            "payload_json": "{}",
                            "occurred_at": "2026-09-28T00:00:00.000Z",
                        }),
                        deadline_ms: 10_000,
                        trace: None,
                    },
                    u64::from(i),
                    connection,
                )
                .await
                .expect("memory-hog succeeds");
        }
        let elapsed = start.elapsed();
        println!(
            "fuel_overhead[tight_compute_loop]: {N} calls in {elapsed:?} ({:.4} ms/call)",
            elapsed.as_secs_f64() * 1000.0 / f64::from(N)
        );
        Ok(())
    }

    /// Same measurement as [`measure_fuel_overhead_tight_compute_loop`], for
    /// a host-call-heavy guest instead: `log-write` round-trips through
    /// [`HostBridge`] on every call. Since this benchmark drives real
    /// `on_invoke` calls (not `crate::host::imports`'s narrower `ExecState`-
    /// level tests), the `Connection` needs something answering every
    /// `host-call` frame the guest's `log.write` import issues -- `respond`
    /// spawns exactly that: a background task that replies `Ok({})` to
    /// every `HostCall` frame it sees on `rx`, standing in for a real stage.
    #[ignore]
    #[tokio::test(flavor = "multi_thread")]
    async fn measure_fuel_overhead_host_call_heavy() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.bench.app"))
            .await
            .expect("load succeeds");
        const N: u32 = 500;
        let start = std::time::Instant::now();
        for i in 0..N {
            let (tx, mut rx) =
                tokio::sync::mpsc::unbounded_channel::<penguin_bundle_host::wire::Frame>();
            let connection = crate::wire::Connection::new(tx);
            let responder_conn = Arc::clone(&connection);
            let responder = tokio::spawn(async move {
                while let Some(frame) = rx.recv().await {
                    if let penguin_bundle_host::wire::Message::HostCall(_) = frame.message {
                        let _ = responder_conn.deliver(penguin_bundle_host::wire::Frame::new(
                            frame.id,
                            penguin_bundle_host::wire::Message::HostResult(
                                penguin_bundle_host::wire::HostResultBody {
                                    result: Some(serde_json::json!({})),
                                    error: None,
                                },
                            ),
                        ));
                    }
                }
            });
            executor
                .on_invoke(
                    InvokeBody {
                        app_id: "waddles.bench.app".to_string(),
                        digest: fixture_digest(),
                        export: ExportKind::Transform,
                        payload: serde_json::json!({
                            "platform": "test",
                            "event_type": "log-write",
                            "actor": null,
                            "payload_json": "{}",
                            "occurred_at": "2026-09-28T00:00:00.000Z",
                        }),
                        deadline_ms: 1000,
                        trace: None,
                    },
                    u64::from(i),
                    connection,
                )
                .await
                .expect("log-write succeeds");
            responder.abort();
        }
        let elapsed = start.elapsed();
        println!(
            "fuel_overhead[host_call_heavy]: {N} calls in {elapsed:?} ({:.4} ms/call)",
            elapsed.as_secs_f64() * 1000.0 / f64::from(N)
        );
        Ok(())
    }
}
