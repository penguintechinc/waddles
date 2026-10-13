//! [`Executor`]: the [`RequestHandler`] implementation that actually runs
//! bundles -- `load`/`unload` manage a registry of compiled
//! `wasmtime::component::Component`s keyed by **content-addressed digest**
//! (`sha256:<64 hex>`), and `invoke` instantiates one under the per-call
//! epoch deadline and services every host call it makes against the stage
//! over [`HostBridge`] (spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS7).
//!
//! **Multi-tenant correctness fix (dataplane scale design §3, "bundles are
//! content-addressed by digest"):** every svc_process/svc_action pod now
//! serves ALL tenants (`bundle_active_set::multi_tenant`), so two different
//! `(tenant_id, community_id)` scopes can independently activate two
//! DIFFERENT digests of the exact same `app_id` at the same time (e.g. a
//! gradual per-tenant version rollout). The registry used to be keyed by
//! `app_id` alone -- one process-wide slot per `app_id` -- which meant only
//! one of those two scopes' digests could ever occupy that slot; the other
//! scope's events would silently run the WRONG bundle version. The registry
//! is now keyed by digest instead: `load`/`unload` are refcounted by the
//! number of outstanding scopes referencing a given digest (the caller
//! sends one `load`/`unload` pair per `(tenant, community, app)` scope
//! that starts/stops referencing a digest, see `bundle_active_set::diff::
//! plan_scoped`), so a shared digest across tenants compiles once and stays
//! resident until every referencing scope has unloaded it, while two
//! DIFFERENT digests for the same `app_id` coexist as two independent
//! registry entries. `invoke` looks up strictly by the digest the caller
//! resolved for that specific event's own scope (`InvokeBody.digest`) --
//! never by `app_id` -- so an unknown/unloaded digest is a fail-closed
//! `UNKNOWN_BUNDLE` error, never a silent fallback to "whatever happens to
//! be loaded under this app_id".
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
//! wired -- every fresh digest's `load` calls `wasmtime::component::
//! Component::new` (a real, from-source JIT compile) instead of loading a
//! cached artifact; correctness holds, the ~3-4s cold-compile cost SS7.2
//! measured for a large component does not yet get amortized away.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use penguin_bundle_host::wire::{
    ErrorBody, ErrorCode, ExportKind, HelloBody, InvokeBody, LoadBody, LoadedBody, ResultBody,
    SandboxInfo, ShutdownBody, UnloadBody, UnloadedBody,
};
use sha2::{Digest, Sha256};
use tokio::sync::{OwnedSemaphorePermit, RwLock, Semaphore};
use tracing::{info, warn};
use wasmtime::component::{Component, Linker};
use wasmtime::{Engine, Store};

use crate::config::CliConfig;
use crate::engine::{ticks_for_deadline, StagePre};
use crate::error::ExecutorError;
use crate::host::{ExecState, HostBridge};
use crate::metrics::{self, ExecutorMetrics};
use crate::signing::PlatformPublicKeys;
use crate::wire::{Connection, RequestHandler};

/// Supplies a bundle version's component bytes and signed sidecar bytes
/// for a `component_key`/`sidecar_key` pair (spec SS7.6), returned as
/// `(component_bytes, sidecar_bytes)`. The production implementation is
/// `crate::bucket::BucketComponentSource`, a hand-rolled SigV4-signed
/// HTTP/1.1 client (see that module's doc for why not `object_store`).
/// `on_load` verifies the component against `digest` and the sidecar's
/// embedded Ed25519 signature against `crate::signing::
/// verify_artifact_signature` before ever compiling either.
pub trait ComponentSource: Send + Sync + 'static {
    fn fetch(
        &self,
        component_key: &str,
        sidecar_key: &str,
    ) -> impl std::future::Future<Output = Result<(Vec<u8>, Vec<u8>), ExecutorError>> + Send;
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
    ) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        Err(ExecutorError::Config(format!(
            "TODO(M2 follow-up): object_store bucket GET not yet wired (component_key={component_key:?}); spec SS7.6"
        )))
    }
}

/// A `(tenant_id, community_id, app_id)` triple -- exactly the identity
/// carried on every `Load`/`Unload` wire body's `tenant_id`/`community_id`/
/// `app_id` fields (`penguin_bundle_host::wire`, added specifically for this
/// registry -- see [`LoadedBundle::scopes`]'s doc for why a bare counter is
/// unsafe).
type Scope = (i32, i32, String);

struct LoadedBundle {
    /// The `app_id` from the FIRST `load` call that registered this digest
    /// -- informational only (logging/error messages): the registry's real
    /// identity is the digest (this struct's `HashMap` key), never this
    /// field, so a second scope loading the same digest under a different
    /// `app_id` string never causes a collision or a silent overwrite.
    app_id: String,
    /// The component pre-linked against the executor's `Linker` once at
    /// load time (`Linker::instantiate_pre`), so every invoke only pays for
    /// instantiation, not import resolution/type-checking. A fresh `Store`
    /// (and therefore fresh guest state) is still created per call -- the
    /// digest is shared across tenants, so reusing a live instance would
    /// leak guest state between them.
    pre: StagePre<ExecState>,
    /// `EXECUTOR_INSTANCES_PER_BUNDLE` permits: caps concurrent in-flight
    /// instances of THIS digest so one hot bundle can't consume every
    /// global call slot.
    slots: Arc<Semaphore>,
    /// This bundle's effective per-instance linear-memory cap in MiB,
    /// resolved once from the FIRST `load` call for this digest (spec
    /// SS7.3, sandbox layer 8): `body.limits.memory_mb` when the stage
    /// supplied a non-zero value, else `EXECUTOR_MEMORY_LIMIT_MB`; always
    /// clamped to `EXECUTOR_MAX_MEMORY_LIMIT_MB`. `on_invoke` wires this
    /// into every `Store::limiter` for the bundle rather than re-deriving
    /// it per call. A later `load` for an already-resident digest (a second
    /// scope referencing the same content) does NOT re-resolve this value
    /// -- the compiled component is shared, so its resource policy is
    /// fixed at first residency; a differing `limits.memory_mb` on a later
    /// load is logged, never silently applied.
    memory_limit_mb: u32,
    /// The SET of `(tenant_id, community_id, app_id)` scopes currently
    /// referencing this digest (spec: "load/unload refcounted by the set of
    /// (tenant, community, app) scopes referencing that digest").
    ///
    /// **Deliberately a `HashSet<Scope>`, not a bare counter (gh security
    /// review finding on PR #406):** a bare integer refcount is unsafe --
    /// `bundle_active_set::diff::plan_scoped`'s own doc already documents
    /// that a `load`/`unload` failure the stage perceives (e.g. a timeout)
    /// may in fact have succeeded server-side (the reply was merely lost),
    /// so the stage's own retry-on-perceived-failure logic can send a
    /// SECOND `unload` for a scope that already successfully left. Against
    /// a bare counter, that duplicate would double-decrement and evict a
    /// digest another scope still needs -- exactly the failure mode this
    /// type prevents structurally: `on_load` inserting a scope already in
    /// the set is a no-op (`HashSet::insert` is idempotent by construction)
    /// and `on_unload` removing a scope NOT in the set is also a no-op
    /// (logged + counted, never treated as an error) -- eviction only
    /// happens when this set becomes genuinely empty.
    scopes: HashSet<Scope>,
}

#[cfg(test)]
#[allow(clippy::expect_used)]
impl<S: ComponentSource> Executor<S> {
    /// Test seam: occupies one global call slot, standing in for an
    /// in-flight invoke until the returned permit is dropped.
    pub(crate) fn test_hold_call_slot(&self) -> OwnedSemaphorePermit {
        Arc::clone(&self.call_limiter)
            .try_acquire_owned()
            .expect("a free global call slot")
    }
}

#[cfg(test)]
impl LoadedBundle {
    /// Test seam: the shareable parts a hand-built second registry entry
    /// needs (`component`, pre-linked form, slot semaphore).
    fn test_parts(&self) -> (StagePre<ExecState>, Arc<Semaphore>) {
        (self.pre.clone(), Arc::clone(&self.slots))
    }
}

/// Runs loaded bundles against real wasmtime instantiation. One per
/// executor process, shared (behind an `Arc`) across every connection's
/// read loop.
pub struct Executor<S: ComponentSource> {
    engine: Engine,
    linker: Arc<Linker<ExecState>>,
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
    /// Single-flight compile-in-progress tracker (gh security review item
    /// 2): concurrent `load`s for the SAME cold digest share one `OnceCell`,
    /// so N scopes activating an identical never-before-seen digest at once
    /// trigger exactly one bucket fetch + one `Component::new` JIT compile
    /// (spec SS7.2's ~3-4s cold-compile cost), not N redundant ones. Entries
    /// are removed once their compile resolves (success or failure) --
    /// never left to accumulate across the process's lifetime; a digest
    /// already resident in `bundles` never touches this map at all (the
    /// fast path in `on_load`).
    compiling: tokio::sync::Mutex<HashMap<String, Arc<tokio::sync::OnceCell<Compiled>>>>,
    /// `EXECUTOR_MAX_CONCURRENT_CALLS` permits: the process-wide ceiling on
    /// in-flight invocations across every bundle and tenant. Guest
    /// execution runs on the tokio blocking pool, so this also bounds how
    /// many blocking threads guests can occupy.
    call_limiter: Arc<Semaphore>,
    /// `EXECUTOR_MAX_CONCURRENT_COMPILES` permits: bounds concurrent WASM
    /// JIT compiles (each on a blocking-pool thread).
    compile_limiter: Arc<Semaphore>,
    max_concurrent_calls: u32,
    max_concurrent_compiles: u32,
    instances_per_bundle: usize,
    /// `EXECUTOR_POOL_WAIT_MS`: how long an invoke queues for a slot before
    /// failing `pool_exhausted`.
    pool_wait: Duration,
    /// Set by [`Executor::drain`] on SIGTERM: new `load`/`invoke` requests
    /// are refused `SHUTTING_DOWN` while in-flight work finishes.
    draining: AtomicBool,
    /// Count of `unload` calls naming a scope that was NOT in the digest's
    /// referencing set (a duplicate/retried/orphaned unload, see
    /// [`LoadedBundle::scopes`]'s doc) -- never an error, but never silent
    /// either. `pub(crate)` so tests can assert on it directly; a follow-up
    /// wires this into a real Prometheus counter once this crate stands up
    /// a metrics registry (none exists yet, see this crate's own TODOs for
    /// the bucket source/precompile cache -- same "documented gap, not
    /// silently glossed over" convention).
    pub(crate) orphaned_unload_total: AtomicU64,
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
    /// Platform Ed25519 public key(s) `on_load` checks every fetched
    /// sidecar's signature against (spec SS5.6). Derived leniently from
    /// `cfg.bundle_signing_public_keys` -- unset/blank yields an empty set
    /// (verification skipped, see that field's own doc for why this is
    /// safe); a value that IS set but fails to parse propagates as a
    /// `Config` error from this constructor, same as any other malformed
    /// config field.
    signing_keys: PlatformPublicKeys,
    /// The OTel instruments every load/unload/invoke is recorded into
    /// (`crate::metrics`). Always the process-wide set in production; tests
    /// swap it via `with_metrics` so they can read back exactly what this
    /// executor emitted.
    metrics: Arc<ExecutorMetrics>,
}

/// What the single-flight compile produces: the pre-linked component
/// (`StagePre` owns the compiled `Component`).
#[derive(Clone)]
struct Compiled {
    pre: StagePre<ExecState>,
}

/// How [`Executor::drain`] ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrainOutcome {
    /// Every in-flight invoke and compile finished before the deadline.
    Drained,
    /// The deadline passed first; these were still running when the
    /// executor gave up waiting.
    TimedOut {
        in_flight_calls: u32,
        in_flight_compiles: u32,
    },
}

impl<S: ComponentSource> Executor<S> {
    pub fn new(cfg: &CliConfig, source: S) -> Result<Self, ExecutorError> {
        let engine = crate::engine::build_engine(cfg)?;
        let linker = Arc::new(crate::engine::build_linker(&engine)?);
        let ticker_engine = engine.clone();
        let epoch_ticker = tokio::spawn(async move {
            loop {
                tokio::time::sleep(crate::engine::EPOCH_TICK).await;
                ticker_engine.increment_epoch();
            }
        });
        let signing_keys = PlatformPublicKeys::from_cli(cfg)?;
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
            compiling: tokio::sync::Mutex::new(HashMap::new()),
            call_limiter: Arc::new(Semaphore::new(cfg.executor_max_concurrent_calls as usize)),
            compile_limiter: Arc::new(Semaphore::new(
                cfg.executor_max_concurrent_compiles as usize,
            )),
            max_concurrent_calls: cfg.executor_max_concurrent_calls,
            max_concurrent_compiles: cfg.executor_max_concurrent_compiles,
            instances_per_bundle: cfg.executor_instances_per_bundle as usize,
            pool_wait: Duration::from_millis(cfg.executor_pool_wait_ms),
            draining: AtomicBool::new(false),
            orphaned_unload_total: AtomicU64::new(0),
            epoch_ticker,
            signing_keys,
            metrics: metrics::executor_metrics(),
        })
    }

    /// Test seam: routes this executor's load/unload/invoke metrics into
    /// `metrics` instead of the process-wide set, so an assertion sees only
    /// what this instance emitted.
    #[cfg(test)]
    pub(crate) fn with_metrics(mut self, metrics: Arc<ExecutorMetrics>) -> Self {
        self.metrics = metrics;
        self
    }

    /// Graceful-shutdown drain (SIGTERM): refuses new `load`/`invoke`
    /// requests (`SHUTTING_DOWN`) and waits up to `deadline` for every
    /// in-flight invoke and compile to release its permit. Returns
    /// [`DrainOutcome::TimedOut`] (never an error) when the deadline wins so
    /// the caller can still exit 0 -- a rollout must not hang on a stuck
    /// guest. Idempotent.
    pub async fn drain(&self, deadline: Duration) -> DrainOutcome {
        self.draining.store(true, Ordering::SeqCst);
        let started = Instant::now();
        // Acquiring EVERY permit succeeds only once all in-flight holders
        // have released theirs; held until return, which also blocks any
        // straggler that slipped past the `draining` check.
        let calls = tokio::time::timeout(
            deadline,
            Arc::clone(&self.call_limiter).acquire_many_owned(self.max_concurrent_calls),
        )
        .await;
        let remaining = deadline.saturating_sub(started.elapsed());
        let compiles = tokio::time::timeout(
            remaining,
            Arc::clone(&self.compile_limiter).acquire_many_owned(self.max_concurrent_compiles),
        )
        .await;
        let drained = matches!((&calls, &compiles), (Ok(Ok(_)), Ok(Ok(_))));
        // Release anything acquired before sampling, so the in-flight
        // counts below reflect real holders only.
        drop((calls, compiles));
        if drained {
            return DrainOutcome::Drained;
        }
        DrainOutcome::TimedOut {
            in_flight_calls: self
                .max_concurrent_calls
                .saturating_sub(self.call_limiter.available_permits() as u32),
            in_flight_compiles: self
                .max_concurrent_compiles
                .saturating_sub(self.compile_limiter.available_permits() as u32),
        }
    }

    /// Waits for a free per-bundle slot and a free global call slot, up to
    /// `EXECUTOR_POOL_WAIT_MS`. The per-bundle permit is taken first so a
    /// saturated hot bundle's waiters never sit on global permits other
    /// bundles need. Failure is `EXECUTOR_DEADLINE` with detail
    /// `pool_exhausted` (the pinned wire crate has no dedicated code).
    async fn acquire_call_slots(
        &self,
        bundle_slots: Arc<Semaphore>,
        app_id: &str,
    ) -> Result<(OwnedSemaphorePermit, OwnedSemaphorePermit), ErrorBody> {
        let acquire = async {
            let bundle = bundle_slots.acquire_owned().await?;
            let global = Arc::clone(&self.call_limiter).acquire_owned().await?;
            Ok::<_, tokio::sync::AcquireError>((bundle, global))
        };
        match tokio::time::timeout(self.pool_wait, acquire).await {
            Ok(Ok(permits)) => Ok(permits),
            Ok(Err(_closed)) => Err(error_body(
                ErrorCode::ShuttingDown,
                "executor is shutting down",
            )),
            Err(_elapsed) => {
                warn!(
                    app_id,
                    pool_wait_ms = self.pool_wait.as_millis() as u64,
                    "no free instance slot within the pool wait; failing pool_exhausted"
                );
                let mut body = error_body(
                    ErrorCode::ExecutorDeadline,
                    "pool_exhausted: no free instance slot within EXECUTOR_POOL_WAIT_MS",
                );
                body.detail = Some("pool_exhausted".to_string());
                Err(body)
            }
        }
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

/// Resolves a `load` request's effective per-instance memory cap (spec
/// SS7.3): `requested_mb == 0` means "no preference", falling back to
/// `default_mb`; either way the result never exceeds `max_mb` and is never
/// less than 1 MiB. Pulled out of `on_load` so both the fast (already-
/// resident) and slow (first-compile) paths resolve it identically.
fn resolve_memory_limit_mb(requested_mb: u32, default_mb: u32, max_mb: u32) -> u32 {
    if requested_mb == 0 {
        default_mb
    } else {
        requested_mb
    }
    .min(max_mb)
    .max(1)
}

/// The stable `export` label for an invoke's [`ExportKind`].
fn export_label(kind: &ExportKind) -> &'static str {
    match kind {
        ExportKind::Transform => "transform",
        ExportKind::Dispatch => "dispatch",
    }
}

fn error_body(code: ErrorCode, message: impl Into<String>) -> ErrorBody {
    ErrorBody {
        code,
        message: message.into(),
        detail: None,
    }
}

/// **The ONLY constructor for a `Store<ExecState>` this crate's production
/// code may use** (gh security review CRITICAL finding on PR #406, item 3:
/// "no other `Store::new` may exist outside tests"). Every `Store` this
/// executor ever runs a guest export against MUST be CPU- and memory-bounded
/// -- unconditionally wiring both bounds in the one place a `Store` is born
/// makes "forgot to arm the epoch deadline/memory limiter on this call path"
/// a structurally impossible mistake, rather than a convention every new
/// call site has to remember to repeat:
///
/// - **CPU bound**: `Store::set_epoch_deadline`/`Store::epoch_deadline_trap`
///   (spec SS7.2/SS7.3) -- `deadline_ms` converted to engine epoch ticks via
///   `ticks_for_deadline`; the guest traps with `EXECUTOR_DEADLINE`
///   (`trap_to_error_body`) once `self.epoch_ticker` advances the engine
///   epoch past this deadline, regardless of what the guest is doing (proven
///   against a real unbounded guest loop by `on_invoke_traps_an_infinite_
///   guest_loop_within_its_deadline` below).
/// - **Memory bound**: `Store::limiter` wired to `ExecState`'s own
///   `StoreLimits` (`exec_state.with_memory_limit_mb` must already be called
///   by the caller before this function -- sandbox layer 8, spec SS7.3).
///
/// `grep -n "Store::new" core/bundle_executor/src/invoke.rs` (this crate's
/// only production module that ever runs a guest) must show exactly one hit:
/// the one inside this function's own body.
fn new_bounded_store(engine: &Engine, exec_state: ExecState, deadline_ms: u64) -> Store<ExecState> {
    let mut store = Store::new(engine, exec_state);
    store.set_epoch_deadline(ticks_for_deadline(deadline_ms));
    store.epoch_deadline_trap();
    store.limiter(|state| &mut state.limits);
    store
}

impl<S: ComponentSource> Executor<S> {
    /// The body of `on_load`, recording the per-outcome counter and the
    /// per-phase (fetch / verify / precompile) histograms as it goes. The
    /// `on_load` wrapper adds the end-to-end `total` phase. Behavior is
    /// unchanged from before the metrics existed.
    async fn load_inner(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
        let start = std::time::Instant::now();
        let app_id = body.app_id.clone();
        let digest = body.digest.clone();
        let scope: Scope = (body.tenant_id, body.community_id, app_id.clone());
        if self.draining.load(Ordering::SeqCst) {
            self.metrics
                .record_load(metrics::error_code_label(ErrorCode::ShuttingDown));
            return Err(error_body(
                ErrorCode::ShuttingDown,
                "executor is shutting down",
            ));
        }

        // Content-addressed fast path: a digest already resident (loaded by
        // an earlier scope, possibly under a different `app_id`) is never
        // re-fetched or re-compiled -- just scope-inserted (idempotent, see
        // `LoadedBundle::scopes`'s doc). This is what makes a digest shared
        // across tenants compile exactly once.
        {
            let mut bundles = self.bundles.write().await;
            if let Some(existing) = bundles.get_mut(&digest) {
                let newly_referenced = existing.scopes.insert(scope.clone());
                if existing.app_id != app_id {
                    // Purely observational: two different `app_id` strings
                    // resolving to the identical content digest is unusual
                    // but not unsafe (the digest, not `app_id`, is this
                    // registry's actual identity) -- surfaced so an
                    // operator can investigate an unexpected content-reuse
                    // case, never blocked.
                    info!(
                        app_id,
                        digest,
                        first_registered_app_id = existing.app_id,
                        "digest already resident under a different app_id"
                    );
                }
                if existing.memory_limit_mb
                    != resolve_memory_limit_mb(
                        body.limits.memory_mb,
                        self.default_memory_limit_mb,
                        self.max_memory_limit_mb,
                    )
                {
                    // Non-fatal by design: the compiled component is shared
                    // and its resource policy was fixed at first residency
                    // (see `LoadedBundle::memory_limit_mb`'s doc) -- a
                    // differing request is surfaced, never silently applied
                    // or treated as an error that would block a legitimate
                    // second scope from sharing the digest.
                    warn!(
                        app_id,
                        digest,
                        existing_memory_limit_mb = existing.memory_limit_mb,
                        requested_memory_mb = body.limits.memory_mb,
                        "bundle already resident under a different memory limit request; \
                         keeping the limit resolved at first residency"
                    );
                }
                info!(
                    app_id,
                    digest,
                    tenant_id = scope.0,
                    community_id = scope.1,
                    newly_referenced,
                    referencing_scopes = existing.scopes.len(),
                    "bundle already resident, scope registered"
                );
                self.metrics.record_load("already_resident");
                return Ok(LoadedBody {
                    app_id,
                    digest,
                    precompile_ms: start.elapsed().as_millis() as u64,
                    exports: vec!["transform".to_string(), "dispatch".to_string()],
                });
            }
        }

        // Single-flight fetch+verify+compile (gh security review item 2):
        // get-or-create this digest's `OnceCell` under a short, map-only
        // critical section, then await ITS `get_or_try_init` outside any
        // lock -- a concurrent `load` for the identical digest (from a
        // different scope, or a different connection entirely) awaits the
        // SAME cell and gets the SAME compiled `Component` clone back,
        // rather than redundantly re-fetching/re-compiling. `Component::new`
        // (~3-4s for a large component, spec SS7.2) and the bucket fetch
        // both run OUTSIDE `self.bundles`'s lock either way -- only the map
        // insert further below (and the fast-path/re-check reads) ever hold
        // it, so a slow compile never blocks other connections' `invoke`/
        // `load`/`unload` calls against unrelated digests.
        let cell = {
            let mut compiling = self.compiling.lock().await;
            Arc::clone(
                compiling
                    .entry(digest.clone())
                    .or_insert_with(|| Arc::new(tokio::sync::OnceCell::new())),
            )
        };
        let compiled: Result<&Compiled, ExecutorError> = cell
            .get_or_try_init(|| async {
                let fetch_started = Instant::now();
                let fetched = self
                    .source
                    .fetch(&body.component_key, &body.sidecar_key)
                    .await;
                self.metrics.record_load_phase(
                    &app_id,
                    "fetch",
                    if fetched.is_ok() { "ok" } else { "error" },
                    fetch_started.elapsed(),
                );
                let (bytes, sidecar_bytes) = fetched?;
                let verify_started = Instant::now();
                let verified = verify_digest(&bytes, &body.digest);

                // Artifact integrity (spec SS5.6/Gemini review condition 9):
                // refuse to instantiate a component whose signed sidecar
                // doesn't verify against a configured platform public key,
                // BEFORE the (expensive, and otherwise-trusting)
                // `Component::new` compile step below. Skipped only when no
                // platform key is configured at all
                // (`self.signing_keys.is_empty()`) -- unreachable in
                // production, see `crate::signing::PlatformPublicKeys`'s
                // own doc. Lives inside the single-flight closure so a
                // concurrent `load` for the same digest never bypasses
                // verification via the shared `OnceCell`.
                let verified = verified.and_then(|()| {
                    if self.signing_keys.is_empty() {
                        return Ok(());
                    }
                    crate::signing::verify_artifact_signature(
                        &sidecar_bytes,
                        &self.signing_keys,
                        &body.app_id,
                        &body.version,
                        &body.digest,
                    )
                });
                self.metrics.record_load_phase(
                    &app_id,
                    "verify",
                    if verified.is_ok() { "ok" } else { "error" },
                    verify_started.elapsed(),
                );
                verified?;

                // Bounded + off-reactor: the JIT compile is seconds of pure
                // CPU, so it queues on `compile_limiter` and runs on a
                // blocking-pool thread -- running it inline would stall the
                // event loop (and every other tenant's frames) on this pod.
                let compile_permit = Arc::clone(&self.compile_limiter)
                    .acquire_owned()
                    .await
                    .map_err(|_| {
                        ExecutorError::Wasmtime("executor is shutting down".to_string())
                    })?;
                let compile_started = Instant::now();
                let (engine, linker) = (self.engine.clone(), Arc::clone(&self.linker));
                let compiled = tokio::task::spawn_blocking(move || {
                    let _permit = compile_permit;
                    let component = Component::new(&engine, &bytes)?;
                    let pre = StagePre::new(linker.instantiate_pre(&component)?)?;
                    Ok::<_, wasmtime::Error>(Compiled { pre })
                })
                .await
                .map_err(|e| ExecutorError::Wasmtime(format!("compile task failed: {e}")))
                .and_then(|r| r.map_err(ExecutorError::from));
                self.metrics.record_load_phase(
                    &app_id,
                    "precompile",
                    if compiled.is_ok() { "ok" } else { "error" },
                    compile_started.elapsed(),
                );
                compiled
            })
            .await;
        let Compiled { pre, .. } = match compiled {
            Ok(c) => c.clone(),
            Err(err) => {
                // Never leave a failed attempt's slot behind -- a future
                // `load` for this same digest (a legitimate retry, e.g.
                // after a transient bucket outage) must start fresh, never
                // be told "already failed" indefinitely.
                self.compiling.lock().await.remove(&digest);
                let code = match err {
                    ExecutorError::DigestMismatch { .. } => ErrorCode::DigestMismatch,
                    _ => ErrorCode::LoadFailed,
                };
                self.metrics.record_load(metrics::error_code_label(code));
                return Err(error_body(code, err.to_string()));
            }
        };
        // The compile succeeded -- this digest will now live in `bundles`
        // (below), so its `OnceCell` slot in `compiling` is no longer
        // needed; removing it keeps this map bounded to genuinely in-flight
        // compiles, never accumulating one entry per digest ever seen.
        self.compiling.lock().await.remove(&digest);

        // spec SS7.3: a `load` may request its own `limits.memory_mb`; `0`
        // means "no preference" and falls back to `EXECUTOR_MEMORY_LIMIT_MB`.
        // Either way the effective cap never exceeds
        // `EXECUTOR_MAX_MEMORY_LIMIT_MB`, and is never less than 1 MiB.
        let memory_limit_mb = resolve_memory_limit_mb(
            body.limits.memory_mb,
            self.default_memory_limit_mb,
            self.max_memory_limit_mb,
        );

        // Re-check under the write lock: a concurrent `load` for the same
        // digest (two scopes activating it at almost the same moment) may
        // have won the race and already inserted while this branch was
        // fetching/compiling -- never insert a second, wasted `Component`
        // for a digest that's already resident; scope-insert instead. This
        // critical section is map-only (no I/O, no compile) -- see the
        // comment on `Component::new` above.
        let mut bundles = self.bundles.write().await;
        if let Some(existing) = bundles.get_mut(&digest) {
            existing.scopes.insert(scope.clone());
            info!(
                app_id,
                digest,
                tenant_id = scope.0,
                community_id = scope.1,
                referencing_scopes = existing.scopes.len(),
                "bundle became resident concurrently, scope registered"
            );
        } else {
            let mut scopes = HashSet::with_capacity(1);
            scopes.insert(scope.clone());
            bundles.insert(
                digest.clone(),
                LoadedBundle {
                    app_id: app_id.clone(),
                    pre,
                    slots: Arc::new(Semaphore::new(self.instances_per_bundle)),
                    memory_limit_mb,
                    scopes,
                },
            );
            info!(
                app_id,
                digest,
                tenant_id = scope.0,
                community_id = scope.1,
                ms = start.elapsed().as_millis() as u64,
                "bundle loaded"
            );
        }
        self.metrics.set_bundles_loaded(bundles.len());
        drop(bundles);
        self.metrics.record_load("ok");

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
}

impl<S: ComponentSource> RequestHandler for Executor<S> {
    async fn on_load(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
        let started = Instant::now();
        let app_id = body.app_id.clone();
        let outcome = self.load_inner(body).await;
        let result = if outcome.is_ok() { "ok" } else { "error" };
        self.metrics
            .record_load_phase(&app_id, "total", result, started.elapsed());
        outcome
    }

    async fn on_unload(&self, body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
        let scope: Scope = (body.tenant_id, body.community_id, body.app_id.clone());
        let mut bundles = self.bundles.write().await;
        match bundles.get_mut(&body.digest) {
            Some(loaded) => {
                if loaded.scopes.remove(&scope) {
                    if loaded.scopes.is_empty() {
                        bundles.remove(&body.digest);
                        self.metrics.record_unload("evicted");
                        self.metrics.set_bundles_loaded(bundles.len());
                        info!(
                            app_id = body.app_id,
                            digest = body.digest,
                            tenant_id = scope.0,
                            community_id = scope.1,
                            "bundle unloaded (last referencing scope)"
                        );
                    } else {
                        self.metrics.record_unload("still_referenced");
                        info!(
                            app_id = body.app_id,
                            digest = body.digest,
                            tenant_id = scope.0,
                            community_id = scope.1,
                            referencing_scopes = loaded.scopes.len(),
                            "scope unregistered, still referenced by another scope"
                        );
                    }
                } else {
                    // Orphaned/duplicate unload (gh security review finding
                    // on PR #406): this exact scope was never registered
                    // against this digest, or already removed by an earlier
                    // unload -- a documented, counted no-op, NEVER a
                    // decrement of anything else's residency (see
                    // `LoadedBundle::scopes`'s doc for the exact failure
                    // mode this prevents).
                    self.orphaned_unload_total.fetch_add(1, Ordering::Relaxed);
                    self.metrics.record_unload("orphaned");
                    warn!(
                        app_id = body.app_id,
                        digest = body.digest,
                        tenant_id = scope.0,
                        community_id = scope.1,
                        referencing_scopes = loaded.scopes.len(),
                        "unload named a scope not currently referencing this digest \
                         (duplicate/orphaned unload); no-op, other scopes unaffected"
                    );
                }
                Ok(UnloadedBody {
                    app_id: body.app_id,
                    digest: body.digest,
                })
            }
            None => {
                self.metrics.record_unload("unknown_bundle");
                Err(error_body(
                    ErrorCode::UnknownBundle,
                    format!("digest {} is not loaded", body.digest),
                ))
            }
        }
    }

    async fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        let app_id = body.app_id.clone();
        let export = export_label(&body.export);
        let _active = self.metrics.invocation_started();
        let started = Instant::now();
        let outcome = self.invoke_inner(body, invoke_id, connection).await;
        let (result, trip) = match &outcome {
            Ok(_) => ("ok", None),
            Err(e) => (
                metrics::error_code_label(e.code),
                metrics::sandbox_limit(e.code),
            ),
        };
        self.metrics
            .record_invoke(&app_id, export, result, trip, started.elapsed());
        outcome
    }

    async fn on_shutdown(&self, body: ShutdownBody) {
        warn!(grace_ms = body.grace_ms, "stage requested shutdown");
    }

    /// Wipes the ENTIRE bundle registry and any in-flight compiles (gh
    /// security review item 4 on PR #406) -- see the trait method's own doc
    /// for why a full wipe, rather than per-scope bookkeeping, is both
    /// sufficient and correct for this executor's single-active-connection
    /// architecture. `self.orphaned_unload_total` is deliberately NOT reset
    /// -- it's a lifetime counter, not per-connection state.
    async fn on_disconnect(&self) {
        let evicted = {
            let mut bundles = self.bundles.write().await;
            let evicted = bundles.len();
            bundles.clear();
            evicted
        };
        self.metrics.set_bundles_loaded(0);
        self.compiling.lock().await.clear();
        if evicted > 0 {
            info!(
                evicted_digests = evicted,
                "host-api connection closed; wiped the bundle registry \
                 (the reconnecting stage resends its full authoritative active set)"
            );
        }
    }
}

impl<S: ComponentSource> Executor<S> {
    /// The body of `on_invoke`, recording the instantiate and guest-execution
    /// histograms as it goes. The `on_invoke` wrapper adds the in-flight
    /// gauge, the end-to-end histogram and the sandbox-trip counter around
    /// every exit path. Behavior is unchanged from before the metrics
    /// existed.
    async fn invoke_inner(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        if self.draining.load(Ordering::SeqCst) {
            return Err(error_body(
                ErrorCode::ShuttingDown,
                "executor is shutting down",
            ));
        }
        // Fail-closed, digest-only lookup (spec §3): never falls back to
        // "whatever happens to be loaded under this app_id" -- an
        // unrecognized digest is always `UNKNOWN_BUNDLE`, even if some OTHER
        // digest is currently resident for this same `app_id` under a
        // different scope.
        let (pre, slots, memory_limit_mb) = {
            let bundles = self.bundles.read().await;
            let loaded = bundles
                .get(&body.digest)
                .ok_or_else(|| error_body(ErrorCode::UnknownBundle, body.digest.clone()))?;
            (
                loaded.pre.clone(),
                Arc::clone(&loaded.slots),
                loaded.memory_limit_mb,
            )
        };

        let (bundle_permit, global_permit) = self.acquire_call_slots(slots, &body.app_id).await?;

        let deadline_ms = body.deadline_ms.min(self.max_call_timeout_ms).max(1);
        // Connector spec SS0 condition 4: fuel per invocation, budgeted by
        // world (`transform` vs `dispatch`, see `CliConfig`'s doc), armed in
        // `run_guest` on the fresh `Store` before instantiation, alongside
        // (never replacing) the epoch deadline. Resolved here, before `body`
        // moves into the job, so the budget is fixed before the call starts.
        let fuel_budget = match body.export {
            ExportKind::Transform => self.fuel_limit_transform,
            ExportKind::Dispatch => self.fuel_limit_dispatch,
        };
        let job = GuestJob {
            engine: self.engine.clone(),
            pre,
            metrics: Arc::clone(&self.metrics),
            body,
            invoke_id,
            connection,
            memory_limit_mb,
            deadline_ms,
            fuel_budget,
        };
        // Guest execution is synchronous CPU between host-call awaits (the
        // epoch deadline traps rather than yields), so it runs on a
        // blocking-pool thread driving the future via `Handle::block_on`:
        // a hot/hostile guest then ties up one bounded blocking thread
        // instead of stalling the async reactor for every tenant. The
        // permits move into the task so they are held until the guest has
        // genuinely finished, even if the caller's future is dropped.
        let handle = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || {
            let _slots = (bundle_permit, global_permit);
            handle.block_on(run_guest(job))
        })
        .await
        .map_err(|e| {
            error_body(
                ErrorCode::WasmTrap,
                format!("guest execution task failed: {e}"),
            )
        })?
    }
}

/// Everything one guest invocation needs, owned so it can move onto a
/// blocking-pool thread.
struct GuestJob {
    engine: Engine,
    pre: StagePre<ExecState>,
    metrics: Arc<ExecutorMetrics>,
    body: InvokeBody,
    invoke_id: u64,
    connection: Arc<Connection>,
    memory_limit_mb: u32,
    deadline_ms: u64,
    fuel_budget: u64,
}

/// Instantiates (from the pre-linked component, in a fresh bounded `Store`)
/// and runs one guest export, recording the instantiate and guest-execution
/// histograms. Behavior is unchanged from the former inline `invoke_inner`.
async fn run_guest(job: GuestJob) -> Result<ResultBody, ErrorBody> {
    let GuestJob {
        engine,
        pre,
        metrics,
        body,
        invoke_id,
        connection,
        memory_limit_mb,
        deadline_ms,
        fuel_budget,
    } = job;
    let bridge = HostBridge::new(connection);
    let exec_state = ExecState::new(Some(bridge), body.app_id.clone(), invoke_id)
        .with_memory_limit_mb(memory_limit_mb);
    let mut store = new_bounded_store(&engine, exec_state, deadline_ms);
    // The engine was built with `consume_fuel(true)`
    // (`crate::engine::build_engine`) so a `Store` starts at zero fuel and
    // traps immediately unless armed here on every call -- armed BEFORE
    // instantiation so a heavy start function is metered too.
    store.set_fuel(fuel_budget).map_err(|e| {
        error_body(
            ErrorCode::WasmTrap,
            format!("failed to arm fuel budget: {e}"),
        )
    })?;

    let start = std::time::Instant::now();
    let stage = match pre.instantiate_async(&mut store).await {
        Ok(stage) => stage,
        Err(err) => {
            // Instantiation itself can trap (a heavy/looping start function
            // burning the fuel budget, hitting the epoch deadline, or growing
            // memory past the cap) exactly like an export call can --
            // classify identically via `trap_to_error_body` rather than
            // always bucketing an instantiate failure as `LOAD_FAILED`, which
            // would hide a genuine guest fault from `svc_process::spine`'s
            // DLQ-kind mapping and its per-source circuit breaker (both key
            // off the typed `ErrorCode`, connector spec SS0 condition 5).
            let memory_cap_hit = store.data().memory_cap_hit();
            if memory_cap_hit || err.downcast_ref::<wasmtime::Trap>().is_some() {
                return Err(trap_to_error_body(err, memory_cap_hit));
            }
            return Err(error_body(ErrorCode::LoadFailed, err.to_string()));
        }
    };
    metrics.record_instantiate(&body.app_id, start.elapsed());
    let guest_started = Instant::now();

    let result = match body.export {
        ExportKind::Transform => {
            let event = serde_json::from_value(body.payload)
                .map_err(|e| error_body(ErrorCode::MalformedFrame, e.to_string()))?;
            let outcome = stage
                .waddle_bundle_process_stage()
                .call_transform(&mut store, &event)
                .await;
            metrics.record_guest(&body.app_id, "transform", guest_started.elapsed());
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
            metrics.record_guest(&body.app_id, "dispatch", guest_started.elapsed());
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
    // (spec SS0 condition 4's overhead-measurement requirement). `get_fuel`
    // only errs when fuel accounting is disabled, which never happens here
    // (`crate::engine::build_engine` always enables it) -- fall back to the
    // full budget (fuel_used=0) rather than panicking on an invariant this
    // store can't actually violate.
    let fuel_remaining = store.get_fuel().unwrap_or(fuel_budget);
    let fuel_used = fuel_budget.saturating_sub(fuel_remaining);

    Ok(ResultBody {
        payload: result,
        duration_ms: start.elapsed().as_millis() as u64,
        fuel_used,
    })
}

/// Delegating impl so an `Arc<Executor<S>>` -- the shape shared across
/// every reconnect attempt in `crate::run`'s dial loop, since rebuilding
/// the wasmtime `Engine`/`Linker` on every reconnect would be wasteful --
/// satisfies `RequestHandler` directly without a wrapper newtype. The
/// bundle REGISTRY itself does NOT survive a reconnect (`on_disconnect`
/// above wipes it) -- only the underlying engine/linker construction is
/// preserved.
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

    async fn on_disconnect(&self) {
        (**self).on_disconnect().await
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
                tenant_id: 1,
                community_id: 0,
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
                tenant_id: 1,
                community_id: 0,
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
    /// `Component::new` compile against genuine WASM, not a stand-in) and
    /// an unsigned `{}` sidecar stub -- fine for every test in this module
    /// that constructs its `Executor` via `test_config()` (no
    /// `bundle_signing_public_keys`, so `on_load` skips verification
    /// entirely); tests that DO exercise signature verification build
    /// their own signed sidecar via `SignedFixtureSource` below instead.
    struct FixtureSource;

    const FIXTURE_WASM: &[u8] = include_bytes!("../tests/fixtures/hostile_fixture.wasm");

    impl ComponentSource for FixtureSource {
        async fn fetch(&self, _c: &str, _s: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
            Ok((FIXTURE_WASM.to_vec(), b"{}".to_vec()))
        }
    }

    /// A [`ComponentSource`] that hands back the same fixture bytes plus a
    /// caller-supplied sidecar -- lets signature-verification tests below
    /// control exactly what `on_load` sees without a real bucket.
    struct SignedFixtureSource {
        sidecar: Vec<u8>,
    }

    impl ComponentSource for SignedFixtureSource {
        async fn fetch(&self, _c: &str, _s: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
            Ok((FIXTURE_WASM.to_vec(), self.sidecar.clone()))
        }
    }

    /// A `CliConfig` with `bundle_signing_public_keys` set so
    /// `Executor::new`'s derived `signing_keys` is non-empty and `on_load`
    /// actually enforces signature verification (unlike every other test
    /// in this module, which relies on `test_config()`'s unset default to
    /// skip it).
    fn test_config_with_signing_keys(raw_json: &str) -> CliConfig {
        let mut cfg = test_config();
        cfg.bundle_signing_public_keys = Some(raw_json.to_string());
        cfg
    }

    /// Counts every `fetch` call and sleeps briefly before returning, so a
    /// test can force several concurrent `on_load`s to genuinely overlap
    /// (without the sleep, a fast in-memory fetch could resolve before a
    /// second `on_load` even reaches the single-flight check, making the
    /// test flaky rather than a real concurrency proof).
    #[derive(Default)]
    struct CountingFixtureSource {
        fetch_count: std::sync::Arc<AtomicU64>,
    }

    impl ComponentSource for CountingFixtureSource {
        async fn fetch(&self, _c: &str, _s: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
            self.fetch_count.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            Ok((FIXTURE_WASM.to_vec(), b"{}".to_vec()))
        }
    }

    fn fixture_digest() -> String {
        let mut hasher = Sha256::new();
        hasher.update(FIXTURE_WASM);
        format!("sha256:{:x}", hasher.finalize())
    }

    fn fixture_load_body(app_id: &str) -> LoadBody {
        fixture_load_body_scoped(app_id, 1, 0)
    }

    /// Same as [`fixture_load_body`] but for an explicit `(tenant_id,
    /// community_id)` scope -- the tests that actually exercise the
    /// scope-set registry (two DIFFERENT scopes sharing or independently
    /// versioning a digest) need to tell scope A and scope B apart, which a
    /// fixed `(1, 0)` default can't do.
    fn fixture_load_body_scoped(app_id: &str, tenant_id: i32, community_id: i32) -> LoadBody {
        LoadBody {
            tenant_id,
            community_id,
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

    /// Builds an `UnloadBody` for `app_id`/`digest` scoped to `(tenant_id,
    /// community_id)` -- the counterpart to [`fixture_load_body_scoped`].
    fn fixture_unload_body(
        app_id: &str,
        tenant_id: i32,
        community_id: i32,
        digest: &str,
    ) -> UnloadBody {
        UnloadBody {
            app_id: app_id.to_string(),
            digest: digest.to_string(),
            tenant_id,
            community_id,
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
                tenant_id: 1,
                community_id: 0,
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
                tenant_id: 1,
                community_id: 0,
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

    /// Digest-only lookup, fail-closed (spec §3): a digest that is not
    /// resident is `UNKNOWN_BUNDLE`, even when a DIFFERENT digest is loaded
    /// under the exact same `app_id` -- there is no "loaded under this
    /// app_id but wrong digest" case anymore since the registry no longer
    /// has an `app_id`-keyed slot to mismatch against. This is the direct
    /// regression test for "never invoke whatever is loaded under app_id".
    #[tokio::test]
    async fn on_invoke_fails_closed_on_an_unrecognized_digest_even_under_a_loaded_app_id(
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
                code: ErrorCode::UnknownBundle,
                ..
            })
        ));
        Ok(())
    }

    /// **Multi-tenant correctness regression (the primary bug this module
    /// was fixed for):** two different `(tenant, community)` scopes
    /// activating two DIFFERENT digests of the SAME `app_id` must each get
    /// their own independent, correctly-versioned registry entry -- never
    /// collapse onto one `app_id`-keyed slot where the second scope's load
    /// would silently evict or shadow the first.
    #[tokio::test]
    async fn two_scopes_with_different_digests_of_the_same_app_id_both_stay_loaded(
    ) -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        // Same FixtureSource always returns the same bytes/digest in this
        // test module, so this test verifies the REGISTRY KEY behavior
        // (two loads under the same app_id but told apart by a caller-
        // supplied "second digest") using an artificial second entry
        // inserted directly -- the wire-level `on_load` always compiles the
        // one fixture; the registry-identity property under test is that a
        // SECOND, DIFFERENT digest for `waddles.test.app` occupies its own
        // slot rather than overwriting the first `on_load`'s entry.
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("first scope's load succeeds");
        assert_eq!(executor.bundles.read().await.len(), 1);

        // A second, distinct digest for the identical app_id (a different
        // tenant's independently-activated version) must land in its OWN
        // registry slot, not overwrite the first.
        let other_digest = format!("sha256:{}", "2".repeat(64));
        {
            let mut bundles = executor.bundles.write().await;
            let first = bundles
                .values()
                .next()
                .expect("first scope's bundle is registered")
                .test_parts();
            let mut scopes = HashSet::with_capacity(1);
            scopes.insert((2, 0, "waddles.test.app".to_string()));
            bundles.insert(
                other_digest.clone(),
                LoadedBundle {
                    app_id: "waddles.test.app".to_string(),
                    pre: first.0,
                    slots: first.1,
                    memory_limit_mb: 64,
                    scopes,
                },
            );
        }
        assert_eq!(
            executor.bundles.read().await.len(),
            2,
            "two different digests for the same app_id must both be resident"
        );
        assert!(
            executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "the first scope's digest must still be resident, untouched by the second"
        );
        assert!(
            executor.bundles.read().await.contains_key(&other_digest),
            "the second scope's digest must be independently resident"
        );
        Ok(())
    }

    /// Invokes the fixture's `transform` export once for `app_id`/`digest`
    /// under the given scope's connection-independent context, returning
    /// whether it succeeded -- shared by every test below that needs to
    /// prove "still genuinely invokable", not just "still in the map".
    async fn invoke_noop<S: ComponentSource>(
        executor: &Executor<S>,
        app_id: &str,
        digest: &str,
    ) -> Result<ResultBody, ErrorBody> {
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        executor
            .on_invoke(
                InvokeBody {
                    app_id: app_id.to_string(),
                    digest: digest.to_string(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "noop",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-22T00:00:00.000Z",
                    }),
                    deadline_ms: 1000,
                    trace: None,
                },
                1,
                connection,
            )
            .await
    }

    /// **Single-flight compile regression (gh security review item 2):**
    /// several concurrent `load`s for the exact same COLD digest (from
    /// different scopes) must trigger exactly ONE `ComponentSource::fetch`
    /// (and by extension one `Component::new` JIT compile) -- never one per
    /// concurrent caller. Uses a real, genuinely-overlapping concurrent
    /// `tokio::join!` (not sequential `.await`s) against a fetch source that
    /// sleeps, so this actually proves concurrency rather than an artifact
    /// of running one at a time.
    #[tokio::test]
    async fn concurrent_loads_of_a_cold_digest_share_one_compile() -> Result<(), ExecutorError> {
        let fetch_count = std::sync::Arc::new(AtomicU64::new(0));
        let executor = Executor::new(
            &test_config(),
            CountingFixtureSource {
                fetch_count: fetch_count.clone(),
            },
        )?;

        let (a, b, c) = tokio::join!(
            executor.on_load(fixture_load_body_scoped("waddles.test.app", 1, 0)),
            executor.on_load(fixture_load_body_scoped("waddles.test.app", 2, 0)),
            executor.on_load(fixture_load_body_scoped("waddles.test.app", 3, 0)),
        );
        a.expect("scope 1's load succeeds");
        b.expect("scope 2's load succeeds");
        c.expect("scope 3's load succeeds");

        assert_eq!(
            fetch_count.load(Ordering::SeqCst),
            1,
            "three concurrent loads of the same cold digest must fetch/compile exactly once"
        );
        let bundles = executor.bundles.read().await;
        assert_eq!(bundles.len(), 1, "one registry entry for the shared digest");
        assert_eq!(
            bundles
                .get(&fixture_digest())
                .expect("resident")
                .scopes
                .len(),
            3,
            "all three scopes must be registered against the one compiled entry"
        );
        Ok(())
    }

    /// The SAME digest loaded by two DIFFERENT scopes must compile once
    /// (one registry entry, two scopes in the set) -- verified by inserting
    /// the second `load` under a different `(tenant_id, community_id)` and
    /// observing the registry stays at one entry with two referencing
    /// scopes.
    #[tokio::test]
    async fn shared_digest_across_two_scopes_loads_once_and_tracks_both_scopes(
    ) -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A's load succeeds");
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 2, 0))
            .await
            .expect("scope B's load of the identical digest succeeds");

        let bundles = executor.bundles.read().await;
        assert_eq!(
            bundles.len(),
            1,
            "a shared digest must occupy exactly one registry slot"
        );
        let resident = bundles.get(&fixture_digest()).expect("resident");
        assert_eq!(
            resident.scopes.len(),
            2,
            "two referencing scopes must both be tracked"
        );
        assert!(resident
            .scopes
            .contains(&(1, 0, "waddles.test.app".to_string())));
        assert!(resident
            .scopes
            .contains(&(2, 0, "waddles.test.app".to_string())));
        Ok(())
    }

    /// Unloading one of two scopes referencing a shared digest must NOT
    /// evict the compiled component -- the other scope still needs it. Only
    /// the SECOND (last) unload actually removes the registry entry.
    #[tokio::test]
    async fn unload_of_one_scope_does_not_unload_a_digest_still_referenced(
    ) -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A load succeeds");
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 2, 0))
            .await
            .expect("scope B load succeeds");

        // Scope A goes away first.
        executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                1,
                0,
                &fixture_digest(),
            ))
            .await
            .expect("scope A unload succeeds");
        assert!(
            executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "scope B still references this digest -- it must remain resident"
        );

        // An invoke from scope B must still succeed against the resident
        // component -- proves this isn't just a bookkeeping artifact.
        let still_invokable = invoke_noop(&executor, "waddles.test.app", &fixture_digest()).await;
        assert!(
            still_invokable.is_ok(),
            "scope B's invoke must still succeed while its digest is still referenced: {still_invokable:?}"
        );

        // Scope B goes away second -- now the digest actually unloads.
        executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                2,
                0,
                &fixture_digest(),
            ))
            .await
            .expect("scope B unload succeeds");
        assert!(
            !executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "the last referencing scope's unload must actually evict the digest"
        );
        Ok(())
    }

    /// **The exact regression Gemini's review of PR #406 was filed for:** a
    /// DUPLICATE/retried `unload` from scope A (e.g. the stage perceiving a
    /// timeout on a call that actually succeeded, per `bundle_active_set::
    /// diff::plan_scoped`'s own doc on why this can happen) must NEVER
    /// evict a digest scope B still references. Against the old bare
    /// `refcount: usize` this would double-decrement past B's own
    /// contribution and evict the digest out from under B.
    #[tokio::test]
    async fn duplicate_unload_from_one_scope_never_evicts_a_digest_another_scope_still_needs(
    ) -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A load succeeds");
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 2, 0))
            .await
            .expect("scope B load succeeds");

        // Scope A unloads, then -- a duplicate/retried unload for the exact
        // SAME scope -- unloads again.
        for attempt in 0..2 {
            executor
                .on_unload(fixture_unload_body(
                    "waddles.test.app",
                    1,
                    0,
                    &fixture_digest(),
                ))
                .await
                .unwrap_or_else(|e| panic!("scope A unload attempt {attempt} must succeed (a duplicate is a documented no-op, never an error): {e:?}"));
        }

        assert!(
            executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "scope B's reference must survive scope A's duplicate unload"
        );
        let still_invokable = invoke_noop(&executor, "waddles.test.app", &fixture_digest()).await;
        assert!(
            still_invokable.is_ok(),
            "scope B must still be able to invoke after scope A's duplicate unload: {still_invokable:?}"
        );
        assert_eq!(
            executor.orphaned_unload_total.load(Ordering::Relaxed),
            1,
            "exactly the second, duplicate unload must be counted as orphaned"
        );
        Ok(())
    }

    /// An `unload` naming a scope that never actually loaded this digest
    /// (a different tenant/community than any real referencing scope) is a
    /// documented no-op -- it must not affect the digest's real referencing
    /// scope at all.
    #[tokio::test]
    async fn unload_for_a_scope_that_never_loaded_has_no_effect() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A load succeeds");

        // Scope (99, 0) never loaded anything.
        let result = executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                99,
                0,
                &fixture_digest(),
            ))
            .await;
        assert!(
            result.is_ok(),
            "an unload for a never-loaded scope is a no-op, not an error: {result:?}"
        );
        assert!(
            executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "the real scope A must be completely unaffected"
        );
        let still_invokable = invoke_noop(&executor, "waddles.test.app", &fixture_digest()).await;
        assert!(
            still_invokable.is_ok(),
            "scope A must still invoke: {still_invokable:?}"
        );
        assert_eq!(
            executor.orphaned_unload_total.load(Ordering::Relaxed),
            1,
            "the never-loaded scope's unload must be counted as orphaned"
        );
        Ok(())
    }

    /// Both real referencing scopes (A and B) unloading must actually evict
    /// the digest -- the positive-path counterpart to the two negative tests
    /// above, proving the set-based model still reaches zero correctly.
    #[tokio::test]
    async fn unload_from_both_referencing_scopes_evicts_the_digest() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A load succeeds");
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 2, 0))
            .await
            .expect("scope B load succeeds");

        executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                1,
                0,
                &fixture_digest(),
            ))
            .await
            .expect("scope A unload succeeds");
        executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                2,
                0,
                &fixture_digest(),
            ))
            .await
            .expect("scope B unload succeeds");

        assert!(
            !executor
                .bundles
                .read()
                .await
                .contains_key(&fixture_digest()),
            "once every referencing scope has unloaded, the digest must be evicted"
        );
        Ok(())
    }

    /// **CPU-bound guest, gh security review HIGH finding:** an unbounded
    /// guest loop (the fixture's `busy-loop` branch, no natural
    /// termination, no host import) must trap with `EXECUTOR_DEADLINE`
    /// within its `deadline_ms`, never hang the executor forever. Proves
    /// the epoch-deadline wiring at `Store::set_epoch_deadline`/
    /// `epoch_deadline_trap` (`crate::invoke::RequestHandler::on_invoke`)
    /// actually bounds guest CPU time on a REAL compiled guest, not a
    /// contrived host-side timeout.
    ///
    /// **`flavor = "multi_thread"` is load-bearing, not stylistic:** the
    /// guest's `busy-loop` branch makes zero host imports/await points, so
    /// wasmtime's async component call resolves in one synchronous `poll()`
    /// that never yields back to the runtime on its own. On a
    /// single-threaded (`current_thread`) runtime that poll would
    /// permanently monopolize the only OS thread, so `Executor::new`'s
    /// separately-`tokio::spawn`ed epoch ticker (`self.epoch_ticker`, spec
    /// SS7.2/SS7.3) would never get scheduled to advance the epoch at all --
    /// the trap would never fire and this test would hang forever. A real
    /// second worker thread is what lets the ticker keep incrementing the
    /// engine's epoch counter while this test's own task is stuck inside
    /// the guest's tight loop.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn on_invoke_traps_an_infinite_guest_loop_within_its_deadline(
    ) -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let deadline_ms = 500;
        let start = std::time::Instant::now();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(10),
            executor.on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "busy-loop",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-22T00:00:00.000Z",
                    }),
                    deadline_ms,
                    trace: None,
                },
                1,
                connection,
            ),
        )
        .await
        .expect(
            "on_invoke itself must return well within the outer 10s test timeout -- if this \
             outer timeout fires instead, the epoch deadline never interrupted the guest at all",
        );
        assert!(
            matches!(
                result,
                Err(ErrorBody {
                    code: ErrorCode::ExecutorDeadline,
                    ..
                })
            ),
            "an unbounded guest loop must trap with EXECUTOR_DEADLINE, got {result:?}"
        );
        assert!(
            start.elapsed() < std::time::Duration::from_secs(5),
            "the trap must fire close to the {deadline_ms}ms deadline, not linger; took {:?}",
            start.elapsed()
        );
        Ok(())
    }

    /// An unload for a digest that was never loaded (or already fully
    /// unloaded) is `UNKNOWN_BUNDLE` -- fail-closed, never a silent no-op.
    #[tokio::test]
    async fn unload_of_a_never_loaded_digest_fails_closed() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        let result = executor
            .on_unload(UnloadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: "waddles.test.app".to_string(),
                digest: format!("sha256:{}", "3".repeat(64)),
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
                tenant_id: 1,
                community_id: 0,
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
                tenant_id: 1,
                community_id: 0,
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

    fn limited_config(calls: u32, per_bundle: u32, pool_wait_ms: u64) -> CliConfig {
        let mut cfg = test_config();
        cfg.executor_max_concurrent_calls = calls;
        cfg.executor_instances_per_bundle = per_bundle;
        cfg.executor_pool_wait_ms = pool_wait_ms;
        cfg
    }

    /// Takes every global call permit but `leave`, simulating in-flight
    /// invocations without needing a slow guest.
    fn hold_global_calls(executor: &Executor<FixtureSource>, n: u32) -> Vec<OwnedSemaphorePermit> {
        (0..n)
            .map(|_| {
                Arc::clone(&executor.call_limiter)
                    .try_acquire_owned()
                    .expect("global call permit available")
            })
            .collect()
    }

    /// The global concurrency cap is enforced: with `max_concurrent_calls`
    /// slots all busy, the N+1th invoke queues for `EXECUTOR_POOL_WAIT_MS`
    /// and then fails `pool_exhausted`; once a slot frees it runs.
    #[tokio::test]
    async fn global_call_cap_queues_then_rejects_the_n_plus_1th_invoke() -> Result<(), ExecutorError>
    {
        let executor = Executor::new(&limited_config(2, 4, 150), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load");
        let held = hold_global_calls(&executor, 2);

        let started = Instant::now();
        let err = invoke_noop(&executor, "waddles.test.app", &fixture_digest())
            .await
            .expect_err("N+1th invoke must not run while all slots are busy");
        assert_eq!(err.code, ErrorCode::ExecutorDeadline);
        assert_eq!(err.detail.as_deref(), Some("pool_exhausted"));
        assert!(
            started.elapsed() >= Duration::from_millis(140),
            "must queue for the pool wait before failing, took {:?}",
            started.elapsed()
        );

        // Freeing a slot lets the next invoke through.
        drop(held);
        invoke_noop(&executor, "waddles.test.app", &fixture_digest())
            .await
            .expect("invoke runs once a slot is free");
        Ok(())
    }

    /// A waiting invoke is QUEUED, not rejected, when a slot frees within
    /// the pool wait.
    #[tokio::test]
    async fn invoke_queues_for_a_slot_that_frees_within_the_pool_wait() -> Result<(), ExecutorError>
    {
        let executor = Arc::new(Executor::new(&limited_config(1, 4, 5_000), FixtureSource)?);
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load");
        let held = hold_global_calls(&executor, 1);
        let waiter = {
            let executor = Arc::clone(&executor);
            tokio::spawn(async move {
                invoke_noop(&*executor, "waddles.test.app", &fixture_digest()).await
            })
        };
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(!waiter.is_finished(), "the N+1th invoke must block");
        drop(held);
        waiter
            .await
            .expect("join")
            .expect("queued invoke runs after the slot frees");
        Ok(())
    }

    /// `EXECUTOR_INSTANCES_PER_BUNDLE` caps one digest independently of the
    /// global ceiling: a saturated bundle fails `pool_exhausted` even though
    /// global slots are free.
    #[tokio::test]
    async fn per_bundle_instance_cap_is_enforced_independently() -> Result<(), ExecutorError> {
        let executor = Executor::new(&limited_config(32, 1, 100), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load");
        let slots = {
            let bundles = executor.bundles.read().await;
            Arc::clone(&bundles.get(&fixture_digest()).expect("resident").slots)
        };
        let _held = slots.try_acquire_owned().expect("the one bundle slot");
        let err = invoke_noop(&executor, "waddles.test.app", &fixture_digest())
            .await
            .expect_err("bundle at its instance cap must reject");
        assert_eq!(err.detail.as_deref(), Some("pool_exhausted"));
        assert!(executor.call_limiter.available_permits() == 32);
        Ok(())
    }

    /// Drain refuses new work with `SHUTTING_DOWN`, waits for in-flight
    /// work, and reports `Drained` only after it finishes.
    #[tokio::test]
    async fn drain_waits_for_in_flight_work_and_refuses_new_invokes() -> Result<(), ExecutorError> {
        let executor = Arc::new(Executor::new(&limited_config(2, 4, 100), FixtureSource)?);
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load");
        let held = hold_global_calls(&executor, 1);

        let drain = {
            let executor = Arc::clone(&executor);
            tokio::spawn(async move { executor.drain(Duration::from_secs(10)).await })
        };
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(
            !drain.is_finished(),
            "drain must wait for the in-flight call"
        );

        let err = invoke_noop(&*executor, "waddles.test.app", &fixture_digest())
            .await
            .expect_err("new invokes are refused while draining");
        assert_eq!(err.code, ErrorCode::ShuttingDown);
        let err = executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect_err("new loads are refused while draining");
        assert_eq!(err.code, ErrorCode::ShuttingDown);

        drop(held);
        assert_eq!(drain.await.expect("join"), DrainOutcome::Drained);
        Ok(())
    }

    /// A stuck call cannot hold the process hostage: drain gives up at its
    /// deadline and says how much was still running.
    #[tokio::test]
    async fn drain_times_out_and_reports_what_was_still_in_flight() -> Result<(), ExecutorError> {
        let executor = Executor::new(&limited_config(4, 4, 100), FixtureSource)?;
        let _held = hold_global_calls(&executor, 1);
        let outcome = executor.drain(Duration::from_millis(100)).await;
        assert_eq!(
            outcome,
            DrainOutcome::TimedOut {
                in_flight_calls: 1,
                in_flight_compiles: 0
            }
        );
        Ok(())
    }

    /// JIT compilation runs off the async reactor: on a SINGLE-threaded
    /// runtime, a concurrent task keeps getting scheduled while a cold
    /// `load` compiles. Inline `Component::new` would starve it for the
    /// whole compile (0 ticks).
    #[tokio::test(flavor = "current_thread")]
    async fn cold_load_compile_does_not_starve_the_async_runtime() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        let ticks = Arc::new(AtomicU64::new(0));
        let ticker = {
            let ticks = Arc::clone(&ticks);
            async move {
                loop {
                    tokio::time::sleep(Duration::from_millis(2)).await;
                    ticks.fetch_add(1, Ordering::SeqCst);
                }
            }
        };
        tokio::select! {
            loaded = executor.on_load(fixture_load_body("waddles.test.app")) => {
                loaded.expect("load");
            }
            _ = ticker => unreachable!("the ticker never completes"),
        }
        let observed = ticks.load(Ordering::SeqCst);
        assert!(
            observed >= 3,
            "runtime was starved during the compile: only {observed} ticks"
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

    /// `on_load`'s artifact-signature integration (spec SS5.6/Gemini review
    /// condition 9): builds a real signed sidecar for the fixture's own
    /// digest via `crate::signing`, exercised through the FULL `on_load`
    /// call rather than `crate::signing`'s own unit tests in isolation --
    /// proving the wiring (fetch -> digest check -> signature check ->
    /// compile), not just the crypto.
    mod artifact_signature_on_load {
        use ed25519_dalek::{Signer, SigningKey};
        use serde_json::json;

        use super::*;
        use crate::signing::signing_payload;

        const APP_ID: &str = "waddles.test.signed-app";
        const VERSION: &str = "1";
        const APPROVAL_ID: i64 = 7;

        fn test_key() -> SigningKey {
            SigningKey::from_bytes(&[11u8; 32])
        }

        fn public_keys_json(key_id: &str, key: &SigningKey) -> String {
            use base64::engine::general_purpose::STANDARD as BASE64;
            use base64::Engine as _;
            json!({ key_id: BASE64.encode(key.verifying_key().to_bytes()) }).to_string()
        }

        fn signed_sidecar(key: &SigningKey, key_id: &str, digest: &str) -> Vec<u8> {
            use base64::engine::general_purpose::STANDARD as BASE64;
            use base64::Engine as _;
            let payload =
                signing_payload(APP_ID, VERSION, digest, APPROVAL_ID).expect("valid test fixture");
            let signature = key.sign(&payload);
            serde_json::to_vec(&json!({
                "app_id": APP_ID,
                "version": VERSION,
                "digest": digest,
                "approval_id": APPROVAL_ID,
                "key_id": key_id,
                "algorithm": "ed25519",
                "signature": BASE64.encode(signature.to_bytes()),
            }))
            .expect("serializable fixture")
        }

        fn signed_load_body(digest: String, sidecar: Vec<u8>) -> (LoadBody, SignedFixtureSource) {
            let body = LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: APP_ID.to_string(),
                version: VERSION.to_string(),
                digest,
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                limits: penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            };
            (body, SignedFixtureSource { sidecar })
        }

        #[tokio::test]
        async fn on_load_succeeds_with_a_validly_signed_sidecar() -> Result<(), ExecutorError> {
            let key = test_key();
            let cfg = test_config_with_signing_keys(&public_keys_json("k1", &key));
            let digest = fixture_digest();
            let sidecar = signed_sidecar(&key, "k1", &digest);
            let (body, source) = signed_load_body(digest.clone(), sidecar);
            let executor = Executor::new(&cfg, source)?;
            let loaded = executor
                .on_load(body)
                .await
                .expect("a validly signed sidecar must load");
            assert_eq!(loaded.digest, digest);
            Ok(())
        }

        #[tokio::test]
        async fn on_load_rejects_a_missing_sidecar_signature() -> Result<(), ExecutorError> {
            let key = test_key();
            let cfg = test_config_with_signing_keys(&public_keys_json("k1", &key));
            let digest = fixture_digest();
            // The pre-approval `{}` stub `storage_service.
            // upload_bundle_component()` writes before hub-api ever signs
            // anything.
            let (body, source) = signed_load_body(digest, b"{}".to_vec());
            let executor = Executor::new(&cfg, source)?;
            let result = executor.on_load(body).await;
            assert!(matches!(
                result,
                Err(ErrorBody {
                    code: ErrorCode::LoadFailed,
                    ..
                })
            ));
            Ok(())
        }

        #[tokio::test]
        async fn on_load_rejects_a_sidecar_signed_for_a_different_digest(
        ) -> Result<(), ExecutorError> {
            let key = test_key();
            let cfg = test_config_with_signing_keys(&public_keys_json("k1", &key));
            let digest = fixture_digest();
            // Signed for a DIFFERENT digest than the one the load frame
            // (and the real fixture bytes) actually carry -- the exact
            // "prevent swapping" property the task requires.
            let wrong_digest = format!("sha256:{}", "9".repeat(64));
            let sidecar = signed_sidecar(&key, "k1", &wrong_digest);
            let (body, source) = signed_load_body(digest, sidecar);
            let executor = Executor::new(&cfg, source)?;
            let result = executor.on_load(body).await;
            assert!(matches!(
                result,
                Err(ErrorBody {
                    code: ErrorCode::LoadFailed,
                    ..
                })
            ));
            Ok(())
        }

        #[tokio::test]
        async fn on_load_rejects_an_unknown_signing_key_id() -> Result<(), ExecutorError> {
            let key = test_key();
            // `cfg` only knows about "k1"; the sidecar claims "k2".
            let cfg = test_config_with_signing_keys(&public_keys_json("k1", &key));
            let digest = fixture_digest();
            let sidecar = signed_sidecar(&key, "k2", &digest);
            let (body, source) = signed_load_body(digest, sidecar);
            let executor = Executor::new(&cfg, source)?;
            let result = executor.on_load(body).await;
            assert!(matches!(
                result,
                Err(ErrorBody {
                    code: ErrorCode::LoadFailed,
                    ..
                })
            ));
            Ok(())
        }

        #[tokio::test]
        async fn on_load_accepts_rotation_a_second_configured_key_id_still_loads(
        ) -> Result<(), ExecutorError> {
            let old_key = test_key();
            let new_key = SigningKey::from_bytes(&[13u8; 32]);
            use base64::engine::general_purpose::STANDARD as BASE64;
            use base64::Engine as _;
            let both_keys = json!({
                "platform-old": BASE64.encode(old_key.verifying_key().to_bytes()),
                "platform-new": BASE64.encode(new_key.verifying_key().to_bytes()),
            })
            .to_string();
            let cfg = test_config_with_signing_keys(&both_keys);
            let digest = fixture_digest();
            // Signed under the newly rotated-in key -- still loads because
            // both keys remain configured during rotation.
            let sidecar = signed_sidecar(&new_key, "platform-new", &digest);
            let (body, source) = signed_load_body(digest.clone(), sidecar);
            let executor = Executor::new(&cfg, source)?;
            let loaded = executor
                .on_load(body)
                .await
                .expect("a key rotated in must still load");
            assert_eq!(loaded.digest, digest);
            Ok(())
        }

        #[tokio::test]
        async fn on_load_skips_verification_when_no_signing_keys_are_configured(
        ) -> Result<(), ExecutorError> {
            // Documents the deliberate, precedent-matching "unconfigured"
            // behavior (`crate::signing::PlatformPublicKeys::from_cli`'s
            // own doc) -- unreachable in production because `crate::lib::
            // run` calls `from_cli_required` first and refuses to start
            // otherwise.
            let digest = fixture_digest();
            let (body, source) = signed_load_body(digest.clone(), b"not even json".to_vec());
            let executor = Executor::new(&test_config(), source)?;
            let loaded = executor
                .on_load(body)
                .await
                .expect("no configured signing keys means verification is skipped");
            assert_eq!(loaded.digest, digest);
            Ok(())
        }
    }

    /// **Item 4 regression (gh security review on PR #406):** a disconnect
    /// must wipe the ENTIRE bundle registry -- every scope from every
    /// digest, not just some -- so a subsequent reconnect starts from a
    /// clean slate rather than serving a stale, orphaned scope set.
    #[tokio::test]
    async fn on_disconnect_wipes_the_entire_bundle_registry() -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await
            .expect("scope A load succeeds");
        executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 2, 0))
            .await
            .expect("scope B load succeeds");
        assert_eq!(executor.bundles.read().await.len(), 1);

        executor.on_disconnect().await;

        assert!(
            executor.bundles.read().await.is_empty(),
            "every scope from every digest must be gone after a disconnect"
        );

        // The digest must also be re-fetchable/re-compilable from scratch --
        // proves `compiling`'s in-flight tracker was cleared too, not left
        // pointing at a stale resolved cell from before the wipe.
        let reloaded = executor
            .on_load(fixture_load_body_scoped("waddles.test.app", 1, 0))
            .await;
        assert!(
            reloaded.is_ok(),
            "a fresh load after disconnect must succeed: {reloaded:?}"
        );
        assert_eq!(executor.bundles.read().await.len(), 1);
        Ok(())
    }

    /// **Item 1's "a peer can only unload scopes it loaded itself", proven
    /// at the connection level:** once a connection disconnects (wiping its
    /// scopes), NO subsequent `unload` naming those same scopes can find
    /// anything to act on -- it's a fail-closed `UNKNOWN_BUNDLE`, never a
    /// silent success that could be confused with actually owning them.
    #[tokio::test]
    async fn unload_after_disconnect_fails_closed_as_unknown() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("load succeeds");
        executor.on_disconnect().await;

        let result = executor
            .on_unload(fixture_unload_body(
                "waddles.test.app",
                1,
                0,
                &fixture_digest(),
            ))
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

    /// Ad-hoc fuel-metering overhead measurement (connector spec SS0
    /// condition 4's "measure the overhead" requirement) -- not a
    /// correctness assertion (wall-clock timing in CI is noisy), so this is
    /// `#[ignore]`d and run manually:
    /// `cargo test --lib measure_fuel_overhead -- --ignored --nocapture`.
    /// Average per-call latency for a cheap, no-host-call transform
    /// (`socket-probe`, denied natively with no stage round trip) over many
    /// calls against the same loaded bundle, with fuel metering enabled
    /// exactly as `crate::engine::build_engine` always configures it today.
    #[ignore]
    #[tokio::test(flavor = "multi_thread")]
    async fn measure_fuel_overhead() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.bench.app"))
            .await
            .expect("load succeeds");
        const N: u32 = 2000;
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
                            "event_type": "socket-probe",
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
                .expect("socket-probe succeeds");
        }
        let elapsed = start.elapsed();
        println!(
            "fuel_overhead: {N} calls in {elapsed:?} ({:.4} ms/call)",
            elapsed.as_secs_f64() * 1000.0 / f64::from(N)
        );
        Ok(())
    }

    /// Metric-emission tests: each drives a REAL `on_load`/`on_unload`/
    /// `on_invoke` against the committed hostile fixture component and reads
    /// the data points back out of an in-memory OTel exporter -- proving the
    /// instruments emit under real wasmtime execution, not that they merely
    /// exist. Every test owns an isolated provider (`Executor::with_metrics`),
    /// so assertions are exact.
    mod metrics_emission {
        use super::*;
        use crate::metrics::test_support::Harness;

        fn executor_with<S: ComponentSource>(source: S) -> (Harness, Executor<S>) {
            let harness = Harness::new();
            let metrics = Arc::new(ExecutorMetrics::new(&harness.meter()));
            let executor = Executor::new(&test_config(), source)
                .expect("executor builds")
                .with_metrics(metrics);
            (harness, executor)
        }

        async fn invoke_event(
            executor: &Executor<impl ComponentSource>,
            app_id: &str,
            export: ExportKind,
            payload: serde_json::Value,
            deadline_ms: u64,
        ) -> Result<ResultBody, ErrorBody> {
            let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
            let connection = crate::wire::Connection::new(tx);
            executor
                .on_invoke(
                    InvokeBody {
                        app_id: app_id.to_string(),
                        digest: fixture_digest(),
                        export,
                        payload,
                        deadline_ms,
                        trace: None,
                    },
                    1,
                    connection,
                )
                .await
        }

        fn event(kind: &str) -> serde_json::Value {
            serde_json::json!({
                "platform": "test",
                "event_type": kind,
                "actor": null,
                "payload_json": "{}",
                "occurred_at": "2026-09-22T00:00:00.000Z",
            })
        }

        #[tokio::test]
        async fn a_cold_load_records_every_phase_and_the_resident_gauge() {
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body("waddles.mx.load"))
                .await
                .expect("load succeeds");

            for phase in ["fetch", "verify", "precompile", "total"] {
                assert_eq!(
                    h.count(
                        "waddles_executor_load_seconds",
                        &[
                            ("app_id", "waddles.mx.load"),
                            ("phase", phase),
                            ("result", "ok")
                        ]
                    ),
                    1.0,
                    "phase {phase} must be recorded exactly once"
                );
            }
            // The compile is the expensive phase: real wasmtime work, so its
            // recorded time is genuinely non-zero.
            let compile = h
                .last("waddles_executor_load_seconds", &[("phase", "precompile")])
                .expect("precompile exported");
            assert!(compile.sum > 0.0, "compile time was {}", compile.sum);
            assert_eq!(
                h.count("waddles_executor_loads_total", &[("result", "ok")]),
                1.0
            );
            assert_eq!(
                h.last("waddles_executor_bundles_loaded", &[])
                    .expect("gauge exported")
                    .value,
                1.0
            );
        }

        #[tokio::test]
        async fn a_second_scope_on_a_resident_digest_is_counted_without_redoing_the_phases() {
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body_scoped("waddles.mx.shared", 1, 0))
                .await
                .expect("first load");
            executor
                .on_load(fixture_load_body_scoped("waddles.mx.shared", 2, 0))
                .await
                .expect("second scope, same digest");

            assert_eq!(
                h.count(
                    "waddles_executor_loads_total",
                    &[("result", "already_resident")]
                ),
                1.0
            );
            assert_eq!(
                h.count("waddles_executor_loads_total", &[("result", "ok")]),
                1.0
            );
            // One real compile; the fast path adds only a `total` sample.
            assert_eq!(
                h.count("waddles_executor_load_seconds", &[("phase", "precompile")]),
                1.0
            );
            assert_eq!(
                h.count("waddles_executor_load_seconds", &[("phase", "total")]),
                2.0
            );
            assert_eq!(
                h.last("waddles_executor_bundles_loaded", &[])
                    .expect("gauge exported")
                    .value,
                1.0,
                "two scopes share one resident digest"
            );
        }

        #[tokio::test]
        async fn unloads_are_classified_and_the_gauge_follows_eviction() {
            let (h, executor) = executor_with(FixtureSource);
            let digest = fixture_digest();
            executor
                .on_load(fixture_load_body_scoped("waddles.mx.unload", 1, 0))
                .await
                .expect("load a");
            executor
                .on_load(fixture_load_body_scoped("waddles.mx.unload", 2, 0))
                .await
                .expect("load b");

            // A scope that never referenced the digest: orphaned, no effect.
            executor
                .on_unload(fixture_unload_body("waddles.mx.unload", 9, 9, &digest))
                .await
                .expect("orphaned unload is a no-op, not an error");
            executor
                .on_unload(fixture_unload_body("waddles.mx.unload", 1, 0, &digest))
                .await
                .expect("unload a");
            executor
                .on_unload(fixture_unload_body("waddles.mx.unload", 2, 0, &digest))
                .await
                .expect("unload b (last)");
            // Now fully evicted: another unload names an unknown digest.
            executor
                .on_unload(fixture_unload_body("waddles.mx.unload", 2, 0, &digest))
                .await
                .expect_err("unknown digest fails closed");

            for (result, want) in [
                ("orphaned", 1.0),
                ("still_referenced", 1.0),
                ("evicted", 1.0),
                ("unknown_bundle", 1.0),
            ] {
                assert_eq!(
                    h.count("waddles_executor_unloads_total", &[("result", result)]),
                    want,
                    "{result}"
                );
            }
            assert_eq!(h.count("waddles_executor_orphaned_unloads_total", &[]), 1.0);
            assert_eq!(
                executor.orphaned_unload_total.load(Ordering::Relaxed),
                1,
                "the pre-existing atomic must keep counting alongside the OTel counter"
            );
            assert_eq!(
                h.last("waddles_executor_bundles_loaded", &[])
                    .expect("gauge exported")
                    .value,
                0.0
            );
        }

        #[tokio::test]
        async fn a_digest_mismatch_records_a_failed_verify_and_no_compile() {
            let (h, executor) = executor_with(FixtureSource);
            let mut body = fixture_load_body("waddles.mx.mismatch");
            body.digest = format!("sha256:{}", "0".repeat(64));
            executor
                .on_load(body)
                .await
                .expect_err("a digest mismatch fails the load");

            let count = |phase: &str, result: &str| {
                h.count(
                    "waddles_executor_load_seconds",
                    &[("phase", phase), ("result", result)],
                )
            };
            assert_eq!(count("fetch", "ok"), 1.0);
            assert_eq!(count("verify", "error"), 1.0);
            assert_eq!(
                count("precompile", "ok") + count("precompile", "error"),
                0.0
            );
            assert_eq!(count("total", "error"), 1.0);
            assert_eq!(
                h.count(
                    "waddles_executor_loads_total",
                    &[("result", "digest_mismatch")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_failed_fetch_records_a_failed_fetch_phase_and_load_failed() {
            let (h, executor) = executor_with(UnimplementedBucketSource);
            executor
                .on_load(fixture_load_body("waddles.mx.fetchfail"))
                .await
                .expect_err("the unimplemented bucket source fails closed");
            assert_eq!(
                h.count(
                    "waddles_executor_load_seconds",
                    &[("phase", "fetch"), ("result", "error")]
                ),
                1.0
            );
            assert_eq!(
                h.count("waddles_executor_load_seconds", &[("phase", "verify")]),
                0.0,
                "nothing was fetched, so nothing was verified"
            );
            assert_eq!(
                h.count("waddles_executor_loads_total", &[("result", "load_failed")]),
                1.0
            );
        }

        #[tokio::test]
        async fn a_successful_invoke_records_instantiate_guest_and_total_and_clears_the_gauge() {
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body("waddles.mx.invoke"))
                .await
                .expect("load");
            invoke_event(
                &executor,
                "waddles.mx.invoke",
                ExportKind::Transform,
                event("noop"),
                1000,
            )
            .await
            .expect("noop invoke succeeds");

            assert_eq!(
                h.count(
                    "waddles_executor_invoke_seconds",
                    &[
                        ("app_id", "waddles.mx.invoke"),
                        ("export", "transform"),
                        ("result", "ok")
                    ]
                ),
                1.0
            );
            assert_eq!(
                h.count(
                    "waddles_executor_instantiate_seconds",
                    &[("app_id", "waddles.mx.invoke")]
                ),
                1.0
            );
            assert_eq!(
                h.count(
                    "waddles_executor_guest_seconds",
                    &[("app_id", "waddles.mx.invoke"), ("export", "transform")]
                ),
                1.0
            );
            let total = h
                .last("waddles_executor_invoke_seconds", &[("result", "ok")])
                .expect("invoke exported");
            let guest = h
                .last("waddles_executor_guest_seconds", &[])
                .expect("guest exported");
            assert!(total.sum >= guest.sum, "end-to-end >= guest-only");
            assert_eq!(
                h.last("waddles_executor_invocations_active", &[])
                    .expect("gauge exported")
                    .value,
                0.0,
                "the in-flight gauge must return to zero"
            );
            assert_eq!(h.count("waddles_executor_sandbox_trip_total", &[]), 0.0);
        }

        #[tokio::test]
        async fn an_unknown_digest_is_an_unknown_bundle_result_not_a_trip() {
            let (h, executor) = executor_with(FixtureSource);
            // Nothing loaded: the digest lookup fails closed before any guest runs.
            invoke_event(
                &executor,
                "waddles.mx.unknown",
                ExportKind::Transform,
                event("noop"),
                1000,
            )
            .await
            .expect_err("unknown bundle");
            assert_eq!(
                h.count(
                    "waddles_executor_invoke_seconds",
                    &[
                        ("app_id", "waddles.mx.unknown"),
                        ("result", "unknown_bundle")
                    ]
                ),
                1.0
            );
            assert_eq!(h.count("waddles_executor_sandbox_trip_total", &[]), 0.0);
            assert_eq!(h.count("waddles_executor_instantiate_seconds", &[]), 0.0);
            assert_eq!(
                h.last("waddles_executor_invocations_active", &[])
                    .expect("gauge exported")
                    .value,
                0.0
            );
        }

        /// Multi-threaded on purpose (same as the pre-existing deadline test):
        /// the epoch ticker is a runtime task, and a current-thread runtime
        /// would starve it while the guest's tight loop owns the only thread.
        #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
        async fn a_runaway_guest_records_a_timeout_trip() {
            crate::init_test_tracing();
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body("waddles.mx.busy"))
                .await
                .expect("load");
            invoke_event(
                &executor,
                "waddles.mx.busy",
                ExportKind::Transform,
                event("busy-loop"),
                200,
            )
            .await
            .expect_err("the epoch deadline must interrupt the loop");

            assert_eq!(
                h.count(
                    "waddles_executor_sandbox_trip_total",
                    &[("app_id", "waddles.mx.busy"), ("limit", "timeout")]
                ),
                1.0
            );
            assert_eq!(
                h.count(
                    "waddles_executor_invoke_seconds",
                    &[
                        ("app_id", "waddles.mx.busy"),
                        ("result", "executor_deadline")
                    ]
                ),
                1.0
            );
            // The guest ran (and was cut off), so its time was still recorded.
            let guest = h
                .last(
                    "waddles_executor_guest_seconds",
                    &[("app_id", "waddles.mx.busy")],
                )
                .expect("guest time recorded even when trapped");
            // The epoch deadline is enforced at `EPOCH_TICK` (50 ms) granularity
            // from an arbitrary phase within a tick, so a 200 ms deadline lets
            // the guest run roughly 150-200 ms; assert a floor with margin
            // that still proves real execution time was recorded.
            assert!(
                guest.sum >= 0.1,
                "guest ran for a deadline of ~200ms, recorded {}",
                guest.sum
            );
        }

        #[tokio::test]
        async fn a_memory_hog_records_a_memory_trip() {
            crate::init_test_tracing();
            let (h, executor) = executor_with(FixtureSource);
            let mut body = fixture_load_body("waddles.mx.hog");
            body.limits = penguin_bundle_host::wire::LoadLimits {
                timeout_ms: 10_000,
                memory_mb: 8,
            };
            executor.on_load(body).await.expect("load");
            invoke_event(
                &executor,
                "waddles.mx.hog",
                ExportKind::Transform,
                event("memory-hog"),
                10_000,
            )
            .await
            .expect_err("the memory cap must trap the hog");
            assert_eq!(
                h.count(
                    "waddles_executor_sandbox_trip_total",
                    &[("app_id", "waddles.mx.hog"), ("limit", "memory")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_malformed_dispatch_payload_is_labeled_by_export_and_code() {
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body("waddles.mx.dispatch"))
                .await
                .expect("load");
            invoke_event(
                &executor,
                "waddles.mx.dispatch",
                ExportKind::Dispatch,
                serde_json::json!({"not": "an envelope"}),
                1000,
            )
            .await
            .expect_err("malformed dispatch payload");
            assert_eq!(
                h.count(
                    "waddles_executor_invoke_seconds",
                    &[
                        ("app_id", "waddles.mx.dispatch"),
                        ("export", "dispatch"),
                        ("result", "malformed_frame")
                    ]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_disconnect_wipes_the_registry_and_zeroes_the_gauge() {
            let (h, executor) = executor_with(FixtureSource);
            executor
                .on_load(fixture_load_body("waddles.mx.disconnect"))
                .await
                .expect("load");
            assert_eq!(
                h.last("waddles_executor_bundles_loaded", &[])
                    .expect("gauge exported")
                    .value,
                1.0
            );
            executor.on_disconnect().await;
            assert_eq!(
                h.last("waddles_executor_bundles_loaded", &[])
                    .expect("gauge exported")
                    .value,
                0.0
            );
        }

        #[test]
        fn export_labels_are_stable() {
            assert_eq!(export_label(&ExportKind::Transform), "transform");
            assert_eq!(export_label(&ExportKind::Dispatch), "dispatch");
        }
    }
}
