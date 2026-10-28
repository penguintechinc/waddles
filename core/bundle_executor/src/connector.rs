//! Connector-world export invocation (connector spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S2.3, S2.6, S3.5;
//! issue #840 "B0").
//!
//! [`ConnectorHost`] is the always-pinned, pooled holder of ONE core
//! `waddle:connector@1.0.0` component. It closes the gap `engine.rs` named
//! ("the executor calls them post-instantiation via the generated
//! `Connector` accessors"): given a [`VerifiedManifest`] and the component
//! bytes it pre-links the component against the per-component `Linker`
//! ([`crate::engine::LinkerCache`]) once, then actually *calls*
//! `receiver.on-connect` / `on-frame` / `on-heartbeat-due` / `on-disconnect`.
//!
//! # Isolation model
//!
//! - **Fresh `Store` per call, destroyed after it.** The spec's "checked out
//!   for exactly one call; a Store involved in a trap is destroyed, never
//!   returned to the pool" is satisfied by the strictest reading: no `Store`
//!   is ever reused, so no guest-corrupted state can leak into a later,
//!   unrelated call. What the pool bounds is *concurrency* (a semaphore of
//!   `pool_size` permits), and what is shared is the compiled `Component`
//!   (read-only), so concurrency costs a `Store`, not a recompile.
//! - **Always pinned.** A `ConnectorHost` is owned by its creator for the
//!   process lifetime; it has no idle eviction and no LRU interaction
//!   (spec S2.3).
//! - **Bounded, three ways, via the one `Store` constructor** in
//!   `crate::invoke::new_bounded_store`: epoch deadline (wall clock),
//!   linear-memory cap, and -- armed here before instantiation -- wasmtime
//!   fuel (`EXECUTOR_CONNECTOR_FUEL_LIMIT`, spec S3.5). Whichever trips first
//!   aborts the call into a *typed* [`ConnectorCallError`]; a guest trap never
//!   panics the host and its blast radius is that one call (spec S2.6).
//! - **Guest on a blocking-pool thread**, driven with `Handle::block_on`,
//!   exactly like `crate::invoke`: a CPU-bound guest ties up one bounded
//!   blocking thread, never the async reactor (so a connection's heartbeat
//!   task on the multi-threaded runtime is never starved, spec S3.5).
//!
//! # What is NOT here (deliberate, typed, not silent)
//!
//! - Loading a connector *over the wire*. The four `receiver` exports are
//!   reachable over the host-API wire (`penguin-bundle-host` 0.3.0
//!   `ExportKind::Connector*`, routed by [`crate::connector_wire`]), but a
//!   connector is pinned in-process (`Executor::pin_connector`), never by a
//!   stage `load`: it is core-only and always-pinned, so a stage can neither
//!   load nor evict one. Which connectors a production executor pins at
//!   startup lands with the first real platform connector.
//! - Ed25519 artifact-signature verification (`crate::signing`, spec S3.2.1
//!   gate 1). [`VerifiedManifest`] is by construction the *output* of that
//!   gate; [`ConnectorHost::load`] additionally refuses any non-core
//!   namespace and any digest mismatch (gate 2 + integrity), fail closed.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tracing::{debug, error, warn};
use wasmtime::component::Component;
use wasmtime::{Engine, Store};

use crate::config::CliConfig;
use crate::engine::connector_world::{Connector, ConnectorPre};
use crate::engine::LinkerCache;
use crate::error::ExecutorError;
use crate::host::{ExecState, HostBridge};
use crate::invoke::{new_bounded_store, verify_digest};
use crate::manifest::{VerifiedManifest, WitWorld};
use crate::metrics::{executor_metrics, ExecutorMetrics};

/// The generated WIT types a connector call takes and returns, re-exported so
/// callers never reach into `engine::connector_world`'s generated tree.
pub use crate::engine::connector_world::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};

/// Per-call resource bounds for one connector (spec S3.5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConnectorLimits {
    /// Wasmtime fuel armed on every call (`EXECUTOR_CONNECTOR_FUEL_LIMIT`).
    pub fuel: u64,
    /// Wall-clock epoch deadline per call.
    pub deadline_ms: u64,
    /// Per-instance linear-memory cap, MiB.
    pub memory_limit_mb: u32,
    /// Concurrent in-flight calls (`EXECUTOR_CONNECTOR_POOL_SIZE`).
    pub pool_size: usize,
    /// How long a call waits for a free slot before `pool_exhausted`.
    pub pool_wait: Duration,
}

impl ConnectorLimits {
    /// Derives the limits from the executor's validated [`CliConfig`].
    #[must_use]
    pub fn from_config(cfg: &CliConfig) -> Self {
        Self {
            fuel: cfg.executor_connector_fuel_limit,
            deadline_ms: cfg.executor_call_timeout_ms,
            memory_limit_mb: cfg.executor_memory_limit_mb,
            pool_size: cfg.executor_connector_pool_size as usize,
            pool_wait: Duration::from_millis(cfg.executor_pool_wait_ms),
        }
    }
}

/// Every way a connector export call can fail. A guest-returned
/// `frame-error` ([`Self::Guest`]) is the connector reporting a bad *frame*;
/// every other variant is the sandbox or host containing a fault, and the
/// host transport must treat it like a transport-level failure (tear the
/// connection down, back off -- spec S2.6).
#[derive(Debug, thiserror::Error)]
pub enum ConnectorCallError {
    /// The guest returned `result::err(frame-error)`.
    #[error("connector returned a frame-error: {0:?}")]
    Guest(FrameError),
    /// The guest's fuel budget was exhausted (spec S3.5 / Gemini cond. 4).
    #[error("connector call exhausted its fuel budget: {0}")]
    FuelExhausted(String),
    /// The wall-clock epoch deadline elapsed.
    #[error("connector call exceeded its deadline: {0}")]
    DeadlineExceeded(String),
    /// The per-instance memory cap denied a `memory.grow`.
    #[error("connector call hit its memory cap: {0}")]
    MemoryLimit(String),
    /// Any other guest trap (unreachable, illegal instruction, panic).
    #[error("connector guest trapped: {0}")]
    Trap(String),
    /// No slot freed within `pool_wait`.
    #[error("connector pool exhausted ({in_flight} in flight)")]
    PoolExhausted {
        /// The pool's capacity.
        in_flight: usize,
    },
    /// Instantiation failed without a recognizable trap (e.g. a missing
    /// import the `Linker` did not register).
    #[error("connector instantiation failed: {0}")]
    InstantiateFailed(String),
    /// A host invariant broke (fuel accounting off, blocking task died).
    /// Never counted against the guest.
    #[error("connector host invariant breach: {0}")]
    Internal(String),
}

impl ConnectorCallError {
    /// Stable, low-cardinality outcome label for metrics and logs.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Guest(_) => "guest_error",
            Self::FuelExhausted(_) => "fuel_exhausted",
            Self::DeadlineExceeded(_) => "deadline",
            Self::MemoryLimit(_) => "memory_limit",
            Self::Trap(_) => "trap",
            Self::PoolExhausted { .. } => "pool_exhausted",
            Self::InstantiateFailed(_) => "instantiate_failed",
            Self::Internal(_) => "internal",
        }
    }

    /// True when the sandbox contained a guest fault (trap / fuel / deadline /
    /// memory). The host transport circuit-breaks the connection on these;
    /// a plain [`Self::Guest`] frame-error does not.
    #[must_use]
    pub fn is_sandbox_trip(&self) -> bool {
        matches!(
            self,
            Self::FuelExhausted(_)
                | Self::DeadlineExceeded(_)
                | Self::MemoryLimit(_)
                | Self::Trap(_)
        )
    }
}

/// A successful call's value plus what it cost.
#[derive(Debug)]
pub struct CallReply<T> {
    /// The export's `ok` value.
    pub value: T,
    /// Fuel the call consumed (`budget - remaining`).
    pub fuel_used: u64,
}

/// Runs one connector export on a blocking-pool thread under a pool slot, a
/// fresh bounded `Store` and an armed fuel budget, then classifies and records
/// the outcome. A macro (not a closure-taking fn) because the guest call
/// borrows the freshly built `Store` and component instance, which a plain
/// `FnOnce` signature cannot express without boxing the future.
macro_rules! run_export {
    ($host:expr, $export:literal, |$conn:ident, $store:ident| $call:expr) => {{
        let inner = Arc::clone(&$host.inner);
        let permit = inner.acquire_slot($export).await?;
        let handle = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || {
            let _slot = permit;
            handle.block_on(async move {
                let started = Instant::now();
                let (mut store, $conn) = inner.fresh_instance($export, started).await?;
                let $store = &mut store;
                let outcome = $call.await;
                inner.finish($export, started, &store, outcome)
            })
        })
        .await
        .map_err(|e| ConnectorCallError::Internal(format!("guest task failed: {e}")))?
    }};
}

/// An always-pinned connector component, pre-linked once, callable many times
/// concurrently. Cheap to clone (an `Arc`); every clone shares one pool.
#[derive(Clone)]
pub struct ConnectorHost {
    inner: Arc<Inner>,
}

struct Inner {
    engine: Engine,
    pre: ConnectorPre<ExecState>,
    app_id: String,
    digest: String,
    limits: ConnectorLimits,
    slots: Arc<Semaphore>,
    bridge: Option<Arc<HostBridge>>,
    metrics: Arc<ExecutorMetrics>,
}

impl ConnectorHost {
    /// Compiles and pre-links `component_bytes` for `manifest`, refusing (typed,
    /// fail closed) anything that is not a core-namespaced `connector`-world
    /// component whose bytes hash to `manifest.digest`.
    ///
    /// `bridge` services the component's `http`/`log`/`clock`/`flags`/
    /// `identity` imports; `None` means every host call fails
    /// `ConnectionUnavailable` (a connector that never calls an import, like
    /// Echo, needs none).
    pub async fn load(
        engine: &Engine,
        linkers: &LinkerCache,
        manifest: &VerifiedManifest,
        component_bytes: Vec<u8>,
        limits: ConnectorLimits,
        bridge: Option<Arc<HostBridge>>,
    ) -> Result<Self, ExecutorError> {
        if manifest.world != WitWorld::Connector {
            return Err(ExecutorError::ConnectorLoad(format!(
                "{} declares world {:?}, not `connector`",
                manifest.app_id, manifest.world
            )));
        }
        if !manifest.is_core_namespace() {
            return Err(ExecutorError::ConnectorLoad(format!(
                "{} is not a core-namespaced app_id; the connector world is core-only (spec S3.2.1)",
                manifest.app_id
            )));
        }
        if limits.pool_size == 0 || limits.fuel == 0 {
            return Err(ExecutorError::ConnectorLoad(
                "pool_size and fuel must both be > 0".to_string(),
            ));
        }
        verify_digest(&component_bytes, &manifest.digest)?;

        let linker = linkers.get_or_build(manifest)?;
        let compile_engine = engine.clone();
        let component =
            tokio::task::spawn_blocking(move || Component::new(&compile_engine, &component_bytes))
                .await
                .map_err(|e| ExecutorError::ConnectorLoad(format!("compile task failed: {e}")))?
                .map_err(|e| {
                    ExecutorError::ConnectorLoad(format!("component does not compile: {e}"))
                })?;
        let instance_pre = linker.instantiate_pre(&component).map_err(|e| {
            ExecutorError::ConnectorLoad(format!(
                "component does not link against the connector world: {e}"
            ))
        })?;
        let pre = ConnectorPre::new(instance_pre).map_err(|e| {
            ExecutorError::ConnectorLoad(format!("component lacks the connector exports: {e}"))
        })?;

        debug!(app_id = %manifest.app_id, pool_size = limits.pool_size, "connector pinned");
        Ok(Self {
            inner: Arc::new(Inner {
                engine: engine.clone(),
                pre,
                app_id: manifest.app_id.clone(),
                digest: manifest.digest.clone(),
                slots: Arc::new(Semaphore::new(limits.pool_size)),
                limits,
                bridge,
                metrics: executor_metrics(),
            }),
        })
    }

    /// Test seam: route this host's call metrics into `metrics`.
    #[must_use]
    pub fn with_metrics(self, metrics: Arc<ExecutorMetrics>) -> Self {
        let i = &*self.inner;
        Self {
            inner: Arc::new(Inner {
                engine: i.engine.clone(),
                pre: i.pre.clone(),
                app_id: i.app_id.clone(),
                digest: i.digest.clone(),
                limits: i.limits,
                slots: Arc::clone(&i.slots),
                bridge: i.bridge.clone(),
                metrics,
            }),
        }
    }

    /// The pinned component's `app_id`.
    #[must_use]
    pub fn app_id(&self) -> &str {
        &self.inner.app_id
    }

    /// The pinned component's content digest.
    #[must_use]
    pub fn digest(&self) -> &str {
        &self.inner.digest
    }

    /// Free slots right now (test/diagnostic introspection).
    #[must_use]
    pub fn available_slots(&self) -> usize {
        self.inner.slots.available_permits()
    }

    /// `receiver.on-connect`: the connector builds its handshake bytes (with
    /// opaque secret-ref handles still in place -- the host substitutes at
    /// send time, spec S2.1).
    pub async fn on_connect(
        &self,
        ctx: ConnectionCtx,
    ) -> Result<CallReply<HandshakePayload>, ConnectorCallError> {
        run_export!(self, "on-connect", |conn, store| {
            conn.waddle_connector_receiver()
                .call_on_connect(store, &ctx)
        })
    }

    /// `receiver.on-frame`: normalize one raw frame into zero or more events.
    pub async fn on_frame(
        &self,
        frame: RawFrame,
    ) -> Result<CallReply<Vec<NormalizedEvent>>, ConnectorCallError> {
        run_export!(self, "on-frame", |conn, store| {
            conn.waddle_connector_receiver()
                .call_on_frame(store, &frame)
        })
    }

    /// `receiver.on-heartbeat-due`: an optional bundle-supplied heartbeat body.
    pub async fn on_heartbeat_due(
        &self,
        ctx: ConnectionCtx,
    ) -> Result<CallReply<Option<Vec<u8>>>, ConnectorCallError> {
        run_export!(self, "on-heartbeat-due", |conn, store| {
            conn.waddle_connector_receiver()
                .call_on_heartbeat_due(store, &ctx)
        })
    }

    /// `receiver.on-disconnect`: tell the connector the connection ended. Has
    /// no WIT result, so it can only fail by trap/limit.
    pub async fn on_disconnect(
        &self,
        ctx: ConnectionCtx,
        reason: String,
    ) -> Result<CallReply<()>, ConnectorCallError> {
        run_export!(self, "on-disconnect", |conn, store| {
            async {
                conn.waddle_connector_receiver()
                    .call_on_disconnect(store, &ctx, &reason)
                    .await
                    .map(Ok)
            }
        })
    }
}

impl Inner {
    /// Waits up to `pool_wait` for a slot; a full pool is typed back-pressure,
    /// recorded, never an unbounded queue.
    async fn acquire_slot(
        &self,
        export: &'static str,
    ) -> Result<OwnedSemaphorePermit, ConnectorCallError> {
        let acquired = tokio::time::timeout(
            self.limits.pool_wait,
            Arc::clone(&self.slots).acquire_owned(),
        )
        .await;
        match acquired {
            Ok(Ok(permit)) => Ok(permit),
            Ok(Err(_closed)) => Err(ConnectorCallError::Internal(
                "connector pool semaphore closed".to_string(),
            )),
            Err(_elapsed) => {
                let err = ConnectorCallError::PoolExhausted {
                    in_flight: self.limits.pool_size,
                };
                self.metrics.record_connector_call(
                    &self.app_id,
                    export,
                    err.kind(),
                    Duration::ZERO,
                );
                warn!(app_id = %self.app_id, export, "connector pool exhausted");
                Err(err)
            }
        }
    }

    /// Builds the bounded `Store`, arms fuel (before instantiation, so a heavy
    /// start function is metered too) and instantiates.
    async fn fresh_instance(
        &self,
        export: &'static str,
        started: Instant,
    ) -> Result<(Store<ExecState>, Connector), ConnectorCallError> {
        let exec_state = ExecState::new(self.bridge.clone(), self.app_id.clone(), 0)
            .with_memory_limit_mb(self.limits.memory_limit_mb);
        let mut store = new_bounded_store(&self.engine, exec_state, self.limits.deadline_ms);
        if let Err(e) = store.set_fuel(self.limits.fuel) {
            // Fuel accounting off on an engine `build_engine` always enables it
            // for is a host invariant breach -- never blamed on the guest.
            error!(app_id = %self.app_id, export, error = %e,
                "invariant breach: could not arm the connector fuel budget");
            let err = ConnectorCallError::Internal(format!("failed to arm fuel budget: {e}"));
            self.record(export, &err, started);
            return Err(err);
        }
        match self.pre.instantiate_async(&mut store).await {
            Ok(conn) => Ok((store, conn)),
            Err(e) => {
                let err = self.classify(e, &store, true);
                self.record(export, &err, started);
                Err(err)
            }
        }
    }

    /// Maps the raw wasmtime outcome of an export call to a typed result,
    /// records it, and reads back the fuel spent.
    fn finish<T>(
        &self,
        export: &'static str,
        started: Instant,
        store: &Store<ExecState>,
        outcome: wasmtime::Result<Result<T, FrameError>>,
    ) -> Result<CallReply<T>, ConnectorCallError> {
        let result = match outcome {
            Ok(Ok(value)) => {
                let remaining = store.get_fuel().map_err(|e| {
                    ConnectorCallError::Internal(format!("failed to read remaining fuel: {e}"))
                });
                remaining.map(|r| CallReply {
                    value,
                    fuel_used: self.limits.fuel.saturating_sub(r),
                })
            }
            Ok(Err(frame_error)) => Err(ConnectorCallError::Guest(frame_error)),
            Err(trap) => Err(self.classify(trap, store, false)),
        };
        match &result {
            Ok(_) => {
                self.metrics
                    .record_connector_call(&self.app_id, export, "ok", started.elapsed())
            }
            Err(e) => self.record(export, e, started),
        }
        result
    }

    fn record(&self, export: &'static str, err: &ConnectorCallError, started: Instant) {
        self.metrics
            .record_connector_call(&self.app_id, export, err.kind(), started.elapsed());
        if err.is_sandbox_trip() {
            warn!(app_id = %self.app_id, export, kind = err.kind(), "connector sandbox trip");
        } else {
            debug!(app_id = %self.app_id, export, kind = err.kind(), "connector call failed");
        }
    }

    /// Classifies a wasmtime error from the host-only signals (the memory-cap
    /// flag and the typed `Trap`), never by substring-matching guest-influenced
    /// text (same rule as `crate::invoke`'s trap classification).
    fn classify(
        &self,
        err: wasmtime::Error,
        store: &Store<ExecState>,
        instantiating: bool,
    ) -> ConnectorCallError {
        // The wasmtime error chain is deliberately NOT carried: a host-import
        // error or a guest `panic!` message in it can echo guest-supplied text
        // (a URL, a token), and this string crosses the invoke seam to a host
        // that logs. Only the trap class (a static wasmtime description)
        // survives -- see `scrubbed_diagnostic`.
        let chain = scrubbed_diagnostic(&err);
        if store.data().memory_cap_hit() {
            return ConnectorCallError::MemoryLimit(chain);
        }
        match err.downcast_ref::<wasmtime::Trap>() {
            Some(wasmtime::Trap::Interrupt) => ConnectorCallError::DeadlineExceeded(chain),
            Some(wasmtime::Trap::OutOfFuel) => ConnectorCallError::FuelExhausted(chain),
            Some(_) => ConnectorCallError::Trap(chain),
            None if instantiating => ConnectorCallError::InstantiateFailed(chain),
            None => ConnectorCallError::Trap(chain),
        }
    }
}

/// A PII-free, bounded description of a wasmtime failure: the typed trap's
/// static description when there is one, otherwise a fixed class string. The
/// full `{err:#}` chain is never returned because it can include text the
/// guest (or a host-import error echoing guest input) chose.
fn scrubbed_diagnostic(err: &wasmtime::Error) -> String {
    match err.downcast_ref::<wasmtime::Trap>() {
        Some(trap) => format!("wasm trap: {trap}"),
        None => {
            "non-trap failure (error chain withheld: it may carry guest-supplied text)".to_string()
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    /// regression: a host-import error / guest message echoing a secret must
    /// not survive into the diagnostic handed to the host.
    #[test]
    fn a_non_trap_error_chain_carrying_guest_text_is_withheld() {
        let err = wasmtime::Error::msg("fetch https://evil.example/?token=SECRET-123 failed")
            .context("host import failed");
        let diag = scrubbed_diagnostic(&err);
        assert!(
            !diag.contains("SECRET-123") && !diag.contains("evil.example"),
            "{diag}"
        );
        assert!(diag.contains("withheld"));
    }

    #[test]
    fn a_typed_trap_keeps_only_its_static_description() {
        let err = wasmtime::Error::from(wasmtime::Trap::UnreachableCodeReached);
        let diag = scrubbed_diagnostic(&err);
        assert!(diag.starts_with("wasm trap: "), "{diag}");
    }

    #[test]
    fn every_error_has_a_distinct_low_cardinality_kind() {
        let kinds = [
            ConnectorCallError::Guest(FrameError::Malformed("x".into())).kind(),
            ConnectorCallError::FuelExhausted(String::new()).kind(),
            ConnectorCallError::DeadlineExceeded(String::new()).kind(),
            ConnectorCallError::MemoryLimit(String::new()).kind(),
            ConnectorCallError::Trap(String::new()).kind(),
            ConnectorCallError::PoolExhausted { in_flight: 1 }.kind(),
            ConnectorCallError::InstantiateFailed(String::new()).kind(),
            ConnectorCallError::Internal(String::new()).kind(),
        ];
        let distinct: std::collections::BTreeSet<_> = kinds.iter().collect();
        assert_eq!(
            distinct.len(),
            kinds.len(),
            "examined {} kinds",
            kinds.len()
        );
    }

    #[test]
    fn only_sandbox_containment_is_a_trip_not_a_guest_frame_error() {
        assert!(!ConnectorCallError::Guest(FrameError::Backend("x".into())).is_sandbox_trip());
        assert!(!ConnectorCallError::PoolExhausted { in_flight: 1 }.is_sandbox_trip());
        assert!(!ConnectorCallError::Internal(String::new()).is_sandbox_trip());
        assert!(ConnectorCallError::FuelExhausted(String::new()).is_sandbox_trip());
        assert!(ConnectorCallError::DeadlineExceeded(String::new()).is_sandbox_trip());
        assert!(ConnectorCallError::MemoryLimit(String::new()).is_sandbox_trip());
        assert!(ConnectorCallError::Trap(String::new()).is_sandbox_trip());
    }

    /// Metrics are EMITTED by a real call, not merely defined: one ok and one
    /// guest-error `on-frame` against the real Echo component produce the
    /// documented counter + histogram series, labeled by export and outcome.
    #[tokio::test]
    async fn real_calls_emit_the_connector_counter_and_histogram() {
        use crate::engine::{build_engine, LinkerCache};
        use crate::metrics::test_support;
        use clap::Parser;
        use sha2::{Digest, Sha256};

        const ECHO: &[u8] = include_bytes!("../tests/fixtures/echo_connector.wasm");
        let app_id = "waddles.core.test.connector-metrics";
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse");
        let engine = build_engine(&cfg).expect("engine");
        let _ticker = crate::engine::spawn_epoch_ticker(&engine);
        let mut h = Sha256::new();
        h.update(ECHO);
        let manifest = VerifiedManifest::new(
            app_id,
            format!("sha256:{:x}", h.finalize()),
            WitWorld::Connector,
            std::collections::HashSet::new(),
        );
        let host = ConnectorHost::load(
            &engine,
            &LinkerCache::new(engine.clone()),
            &manifest,
            ECHO.to_vec(),
            ConnectorLimits::from_config(&cfg),
            None,
        )
        .await
        .expect("echo loads");
        let ctx = ConnectionCtx {
            platform: "echo".into(),
            source_id: "s".into(),
            connection_id: "c".into(),
            session_token: None,
        };
        let frame = |b: &[u8]| RawFrame {
            ctx: ctx.clone(),
            bytes: b.to_vec(),
            received_at: "2026-10-12T00:00:00.000Z".into(),
        };
        host.on_frame(frame(br#"{"source":"echo","text":"hi"}"#))
            .await
            .expect("ok frame");
        host.on_frame(frame(b"nope")).await.expect_err("bad frame");

        let h = test_support::shared();
        let calls = "waddles_executor_connector_calls_total";
        assert_eq!(
            h.count(
                calls,
                &[
                    ("app_id", app_id),
                    ("export", "on-frame"),
                    ("outcome", "ok")
                ]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                calls,
                &[
                    ("app_id", app_id),
                    ("export", "on-frame"),
                    ("outcome", "guest_error")
                ]
            ),
            1.0
        );
        assert!(
            h.count(
                "waddles_executor_connector_call_seconds",
                &[("app_id", app_id), ("outcome", "ok")]
            ) >= 1.0,
            "the latency histogram must have a sample"
        );
    }
}
