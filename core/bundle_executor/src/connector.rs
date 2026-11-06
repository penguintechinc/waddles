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
use crate::engine::connector_tools_world::{ConnectorTools, ConnectorToolsPre};
use crate::engine::connector_world::{Connector, ConnectorPre};
use crate::engine::LinkerCache;
use crate::error::ExecutorError;
use crate::host::{ExecState, HostBridge};
use crate::invoke::{new_bounded_store, verify_digest};
use crate::manifest::{VerifiedManifest, WitWorld};
use crate::metrics::{executor_metrics, ExecutorMetrics};

/// The `sender.build-request` argument and result records (P6a T6d): the
/// outbound `action` the host hands a connector, and the `http-request-tpl`
/// (slots and opaque secret handles only) it returns.
pub use crate::engine::connector_world::exports::waddle::connector::sender::{
    Action as SenderAction, Header as TemplateHeader, HttpRequestTpl,
};
/// The generated WIT types a connector call takes and returns, re-exported so
/// callers never reach into `engine::connector_world`'s generated tree.
pub use crate::engine::connector_world::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};
/// The `tool-provider.call` error a `connector-tools` guest returns (P6a T4).
pub use crate::engine::stage_tools_world::waddle::bundle::tool::ToolError;

/// A pre-linked connector component: the frozen `connector` world, or the
/// additive `connector-tools` world (P6a T4: `connector` + `tool` import +
/// `tool-provider` export). Chosen once at load from the manifest's world.
#[derive(Clone)]
enum ConnectorPreKind {
    Plain(ConnectorPre<ExecState>),
    Tools(ConnectorToolsPre<ExecState>),
}

/// One instantiated connector component of either world.
enum ConnectorInstance {
    Plain(Connector),
    Tools(ConnectorTools),
}

/// Awaits one `receiver` export call on whichever world's generated accessor
/// the instance carries. Each world generates its own `Guest` struct (the
/// `with` remap shares the data types, not the export accessor), so the call
/// is spelled out per arm; argument and return types are the shared ones.
macro_rules! receiver_call {
    ($inst:expr, $method:ident, $store:expr $(, $arg:expr)*) => {
        match $inst {
            ConnectorInstance::Plain(c) => {
                c.waddle_connector_receiver().$method($store $(, $arg)*).await
            }
            ConnectorInstance::Tools(c) => {
                c.waddle_connector_receiver().$method($store $(, $arg)*).await
            }
        }
    };
}

/// `sender.build-request` on whichever world's accessor the instance carries.
/// The `connector-tools` bindgen remaps imported interfaces onto `connector`'s
/// but still generates its own copy of the EXPORTED `sender` records, so the
/// tools arm converts the (structurally identical) records at the boundary.
async fn call_sender(
    conn: &ConnectorInstance,
    store: &mut Store<ExecState>,
    action: &SenderAction,
) -> wasmtime::Result<Result<HttpRequestTpl, FrameError>> {
    match conn {
        ConnectorInstance::Plain(c) => {
            c.waddle_connector_sender()
                .call_build_request(store, action)
                .await
        }
        ConnectorInstance::Tools(c) => {
            use crate::engine::connector_tools_world::exports::waddle::connector::sender as tools;
            let tools_action = tools::Action {
                kind: action.kind.clone(),
                target_source_id: action.target_source_id.clone(),
                payload_json: action.payload_json.clone(),
            };
            let reply = c
                .waddle_connector_sender()
                .call_build_request(store, &tools_action)
                .await?;
            Ok(reply.map(|t| HttpRequestTpl {
                method: t.method,
                url: t.url,
                headers: t
                    .headers
                    .into_iter()
                    .map(|h| TemplateHeader {
                        name: h.name,
                        value: h.value,
                    })
                    .collect(),
                body: t.body,
                secret_refs: t.secret_refs,
            }))
        }
    }
}

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
    /// Largest accepted `tool-provider.call` args, canonical-JSON bytes.
    pub max_tool_args_bytes: usize,
    /// Largest accepted `tool-provider.call` result, canonical-JSON bytes.
    pub max_tool_result_bytes: usize,
}

/// Default cap on tool args / results: just under the host-API
/// `MAX_FRAME_BYTES` (1 MiB), since a larger value could not ride a wire frame
/// anyway. Checked BEFORE the result is parsed into a `serde_json::Value`, so a
/// guest cannot amplify a huge byte blob into a larger host-heap tree.
pub const MAX_TOOL_PAYLOAD_BYTES: usize = 900_000;

/// Extra wall-clock a call may run past its epoch deadline before the host
/// abandons it. The epoch trap normally ends a guest at `deadline_ms`; this
/// grace only matters when the guest is parked on a HOST await (a stage that
/// never answers a host call), where no epoch trap can fire.
const HOST_AWAIT_GRACE: Duration = Duration::from_millis(250);

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
            max_tool_args_bytes: MAX_TOOL_PAYLOAD_BYTES,
            max_tool_result_bytes: MAX_TOOL_PAYLOAD_BYTES,
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
    /// The guest's `tool-provider.call` returned `result::err(tool-error)`
    /// (P6a T4). A typed tool refusal, not a sandbox fault.
    #[error("connector tool call returned a tool-error: {0:?}")]
    Tool(ToolError),
    /// `tool-provider.call` on a component that was loaded under the plain
    /// `connector` world and therefore exports no tools. Fail loud, never a
    /// silent empty result.
    #[error("connector exports no tool-provider (loaded under the `connector` world)")]
    NoToolProvider,
    /// A tool's args (caller) or result (guest) exceeded the configured byte
    /// cap. Refused loudly; the oversized value is never parsed or forwarded.
    #[error("tool {what} is {len} bytes, over the {max}-byte cap")]
    PayloadTooLarge {
        /// `"args"` or `"result"`.
        what: &'static str,
        /// The offending size.
        len: usize,
        /// The cap it exceeded.
        max: usize,
    },
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
            Self::Tool(_) => "tool_error",
            Self::NoToolProvider => "no_tool_provider",
            Self::PayloadTooLarge { .. } => "payload_too_large",
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

/// The slots one call holds: always one of the combined `pool_size` pool, plus
/// (for a tool call) one of the tool share. Released together on drop.
struct CallPermits {
    _total: OwnedSemaphorePermit,
    _tool_share: Option<OwnedSemaphorePermit>,
}

/// Runs one connector export on a blocking-pool thread under a pool slot, a
/// fresh bounded `Store` and an armed fuel budget, then classifies and records
/// the outcome. A macro (not a closure-taking fn) because the guest call
/// borrows the freshly built `Store` and component instance, which a plain
/// `FnOnce` signature cannot express without boxing the future.
macro_rules! run_export {
    ($host:expr, $export:literal, $guest_err:expr, |$conn:ident, $store:ident| $call:expr) => {{
        run_export!(
            $host,
            $export,
            $guest_err,
            pool = receiver,
            deadline = $host.inner.limits.deadline_ms,
            |$conn, $store| $call
        )
    }};
    ($host:expr, $export:literal, $guest_err:expr, pool = $pool:ident, deadline = $deadline:expr,
     |$conn:ident, $store:ident| $call:expr) => {{
        let inner = Arc::clone(&$host.inner);
        let deadline_ms: u64 = $deadline;
        let permit = inner
            .acquire_slot($export, stringify!($pool) == "tool")
            .await?;
        let handle = tokio::runtime::Handle::current();
        tokio::task::spawn_blocking(move || {
            let _slot = permit;
            handle.block_on(async move {
                let started = Instant::now();
                let (mut store, $conn) =
                    inner.fresh_instance($export, started, deadline_ms).await?;
                let $store = &mut store;
                // The epoch trap cannot interrupt a guest parked on a HOST await
                // (an import the stage never answers). Bound the whole call by
                // the deadline plus a grace so the permit is always returned.
                let budget = Duration::from_millis(deadline_ms) + HOST_AWAIT_GRACE;
                let outcome = match tokio::time::timeout(budget, $call).await {
                    Ok(outcome) => outcome,
                    Err(_elapsed) => {
                        let err = ConnectorCallError::DeadlineExceeded(
                            "host await exceeded the call deadline".to_string(),
                        );
                        inner.record($export, &err, started);
                        return Err(err);
                    }
                };
                inner.finish($export, started, &store, outcome, $guest_err)
            })
        })
        .await
        .map_err(|e| ConnectorCallError::Internal(format!("guest task failed: {e}")))?
    }};
}

/// Size of the tool-provider SHARE of the combined pool: half of it, at least
/// one. Tool calls draw from this share AND the combined pool, so total
/// in-flight instances never exceed `pool_size` and saturating tools can never
/// consume more than the share -- `on-frame`/heartbeat always keep the rest.
#[must_use]
pub fn tool_pool_size(receiver_pool_size: usize) -> usize {
    (receiver_pool_size / 2).max(1)
}

/// The effective per-call deadline: a caller-supplied (wire) deadline may only
/// tighten the connector's own limit, never extend it.
#[must_use]
pub fn clamp_deadline_ms(requested_ms: Option<u64>, limit_ms: u64) -> u64 {
    requested_ms.map_or(limit_ms, |r| r.min(limit_ms)).max(1)
}

/// An always-pinned connector component, pre-linked once, callable many times
/// concurrently. Cheap to clone (an `Arc`); every clone shares one pool.
#[derive(Clone)]
pub struct ConnectorHost {
    inner: Arc<Inner>,
}

#[derive(Clone)]
struct Inner {
    engine: Engine,
    pre: ConnectorPreKind,
    app_id: String,
    digest: String,
    limits: ConnectorLimits,
    /// The combined cap on concurrent instances (`pool_size`), shared by every
    /// export kind.
    slots: Arc<Semaphore>,
    /// The reserved tool share ([`tool_pool_size`]) of `slots`: a tool call
    /// holds one of these AND one of `slots`, so tool traffic can never occupy
    /// more than its share and starve `receiver` calls (`on-frame`, heartbeat).
    tool_slots: Arc<Semaphore>,
    bridge: Option<Arc<HostBridge>>,
    /// The `invoke` frame id charged for this host's host-calls
    /// (`HostCallBody.call_id`); `0` for a pin-time host not bound to a call.
    call_id: u64,
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
        if !manifest.world.is_connector() {
            return Err(ExecutorError::ConnectorLoad(format!(
                "{} declares world {:?}, not `connector`/`connector-tools`",
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
        let pre = match manifest.world {
            WitWorld::ConnectorTools => ConnectorToolsPre::new(instance_pre)
                .map(ConnectorPreKind::Tools)
                .map_err(|e| {
                    ExecutorError::ConnectorLoad(format!(
                        "component lacks the connector-tools exports (receiver/sender/tool-provider): {e}"
                    ))
                })?,
            _ => ConnectorPre::new(instance_pre)
                .map(ConnectorPreKind::Plain)
                .map_err(|e| {
                    ExecutorError::ConnectorLoad(format!(
                        "component lacks the connector exports: {e}"
                    ))
                })?,
        };

        debug!(app_id = %manifest.app_id, pool_size = limits.pool_size, "connector pinned");
        Ok(Self {
            inner: Arc::new(Inner {
                engine: engine.clone(),
                pre,
                app_id: manifest.app_id.clone(),
                digest: manifest.digest.clone(),
                slots: Arc::new(Semaphore::new(limits.pool_size)),
                tool_slots: Arc::new(Semaphore::new(tool_pool_size(limits.pool_size))),
                limits,
                bridge,
                call_id: 0,
                metrics: executor_metrics(),
            }),
        })
    }

    /// Test seam: route this host's call metrics into `metrics`.
    #[must_use]
    pub fn with_metrics(self, metrics: Arc<ExecutorMetrics>) -> Self {
        Self {
            inner: Arc::new(Inner {
                metrics,
                ..(*self.inner).clone()
            }),
        }
    }

    /// A handle to the SAME pinned component and pools, whose host-calls go
    /// through `bridge` and are charged to `call_id` (the `invoke` frame id).
    /// Used per connector-link invoke so every guest host-call returns over
    /// the connection the export arrived on, carrying that call's scope.
    #[must_use]
    pub fn bound(&self, bridge: Option<Arc<HostBridge>>, call_id: u64) -> Self {
        Self {
            inner: Arc::new(Inner {
                bridge,
                call_id,
                ..(*self.inner).clone()
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

    /// Free tool-share slots right now (test/diagnostic introspection).
    #[must_use]
    pub fn available_tool_slots(&self) -> usize {
        self.inner.tool_slots.available_permits()
    }

    /// Free slots of the combined pool right now (test/diagnostic introspection).
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
        run_export!(
            self,
            "on-connect",
            ConnectorCallError::Guest,
            |conn, store| { async { receiver_call!(&conn, call_on_connect, store, &ctx) } }
        )
    }

    /// `receiver.on-frame`: normalize one raw frame into zero or more events.
    pub async fn on_frame(
        &self,
        frame: RawFrame,
    ) -> Result<CallReply<Vec<NormalizedEvent>>, ConnectorCallError> {
        run_export!(
            self,
            "on-frame",
            ConnectorCallError::Guest,
            |conn, store| { async { receiver_call!(&conn, call_on_frame, store, &frame) } }
        )
    }

    /// `receiver.on-heartbeat-due`: an optional bundle-supplied heartbeat body.
    pub async fn on_heartbeat_due(
        &self,
        ctx: ConnectionCtx,
    ) -> Result<CallReply<Option<Vec<u8>>>, ConnectorCallError> {
        run_export!(
            self,
            "on-heartbeat-due",
            ConnectorCallError::Guest,
            |conn, store| { async { receiver_call!(&conn, call_on_heartbeat_due, store, &ctx) } }
        )
    }

    /// `tool-provider.call` (P6a T4): run one of the connector service bundle's own
    /// exported tools (`get_chatters`, `send_chat`, ...). `args` and the
    /// result are canonical JSON bytes. Fails loud with
    /// [`ConnectorCallError::NoToolProvider`] on a plain-`connector` component
    /// (no silent empty result); a guest `tool-error` is
    /// [`ConnectorCallError::Tool`]. Runs under the same pool slot, fuel
    /// budget and deadline as every other connector export.
    pub async fn call_tool(
        &self,
        name: String,
        args: Vec<u8>,
    ) -> Result<CallReply<Vec<u8>>, ConnectorCallError> {
        self.call_tool_with_deadline(name, args, None).await
    }

    /// [`Self::call_tool`] with a caller-supplied deadline (the wire
    /// `InvokeBody.deadline_ms`), clamped to `min(requested, limits.deadline_ms)`
    /// -- a caller can tighten but never extend the connector's own bound. Runs
    /// on the dedicated tool pool ([`tool_pool_size`]), never the receiver pool.
    pub async fn call_tool_with_deadline(
        &self,
        name: String,
        args: Vec<u8>,
        requested_deadline_ms: Option<u64>,
    ) -> Result<CallReply<Vec<u8>>, ConnectorCallError> {
        let deadline = clamp_deadline_ms(requested_deadline_ms, self.inner.limits.deadline_ms);
        // Size gate BEFORE admission: an oversized request never takes a slot.
        if args.len() > self.inner.limits.max_tool_args_bytes {
            let err = ConnectorCallError::PayloadTooLarge {
                what: "args",
                len: args.len(),
                max: self.inner.limits.max_tool_args_bytes,
            };
            self.inner.record("tool-call", &err, Instant::now());
            return Err(err);
        }
        if matches!(self.inner.pre, ConnectorPreKind::Plain(_)) {
            let err = ConnectorCallError::NoToolProvider;
            self.inner.metrics.record_connector_call(
                &self.inner.app_id,
                "tool-call",
                err.kind(),
                Duration::ZERO,
            );
            warn!(app_id = %self.inner.app_id, "tool call on a connector with no tool-provider");
            return Err(err);
        }
        let reply = run_export!(
            self,
            "tool-call",
            ConnectorCallError::Tool,
            pool = tool,
            deadline = deadline,
            |conn, store| {
                async {
                    match &conn {
                        ConnectorInstance::Tools(c) => {
                            c.waddle_bundle_tool_provider()
                                .call_call(store, &name, &args)
                                .await
                        }
                        // Unreachable: guarded above. Surfaces as a trap-shaped
                        // host error rather than a panic if it ever is not.
                        ConnectorInstance::Plain(_) => Err(wasmtime::Error::msg(
                            "tool-call routed to a plain connector instance",
                        )),
                    }
                }
            }
        )?;
        // The guest result is already lifted into a host `Vec`; refuse an
        // oversized one HERE, before anything parses it into a larger tree.
        if reply.value.len() > self.inner.limits.max_tool_result_bytes {
            let err = ConnectorCallError::PayloadTooLarge {
                what: "result",
                len: reply.value.len(),
                max: self.inner.limits.max_tool_result_bytes,
            };
            self.inner.record("tool-call", &err, Instant::now());
            return Err(err);
        }
        Ok(reply)
    }

    /// `sender.build-request` (P6a T6d): the connector shapes the platform
    /// request for an outbound `action`. The returned template carries SLOTS
    /// and opaque secret handles only; the host's outbound executor verifies
    /// and fills it (the guest never sees a credential).
    ///
    /// Bounded like the tool-call path (#984): the action's total size is
    /// checked against `max_tool_args_bytes` BEFORE admission (an oversized
    /// request never takes a slot), the template's total size against
    /// `max_tool_result_bytes` BEFORE it is encoded anywhere, and a
    /// caller-supplied deadline can only tighten `limits.deadline_ms`. Runs on
    /// the reserved tool/sender share ([`tool_pool_size`]) of the combined pool
    /// (never the receiver capacity, so a flood of build-requests cannot starve
    /// `on-frame`/heartbeat) under the connector's own fuel budget. A guest
    /// `frame-error` is [`ConnectorCallError::Guest`].
    pub async fn build_request(
        &self,
        action: SenderAction,
        requested_deadline_ms: Option<u64>,
    ) -> Result<CallReply<HttpRequestTpl>, ConnectorCallError> {
        let deadline = clamp_deadline_ms(requested_deadline_ms, self.inner.limits.deadline_ms);
        let action_len = action
            .kind
            .len()
            .saturating_add(action.target_source_id.len())
            .saturating_add(action.payload_json.len());
        if action_len > self.inner.limits.max_tool_args_bytes {
            let err = ConnectorCallError::PayloadTooLarge {
                what: "args",
                len: action_len,
                max: self.inner.limits.max_tool_args_bytes,
            };
            self.inner.record("build-request", &err, Instant::now());
            return Err(err);
        }
        let reply = run_export!(
            self,
            "build-request",
            ConnectorCallError::Guest,
            pool = tool,
            deadline = deadline,
            |conn, store| call_sender(&conn, store, &action)
        )?;
        // The guest result is already lifted into host memory; refuse an
        // oversized template HERE, before anything encodes it further.
        let result_len = template_len(&reply.value);
        if result_len > self.inner.limits.max_tool_result_bytes {
            let err = ConnectorCallError::PayloadTooLarge {
                what: "result",
                len: result_len,
                max: self.inner.limits.max_tool_result_bytes,
            };
            self.inner.record("build-request", &err, Instant::now());
            return Err(err);
        }
        Ok(reply)
    }

    /// `receiver.on-disconnect`: tell the connector the connection ended. Has
    /// no WIT result, so it can only fail by trap/limit.
    pub async fn on_disconnect(
        &self,
        ctx: ConnectionCtx,
        reason: String,
    ) -> Result<CallReply<()>, ConnectorCallError> {
        run_export!(
            self,
            "on-disconnect",
            ConnectorCallError::Guest,
            |conn, store| {
                async { receiver_call!(&conn, call_on_disconnect, store, &ctx, &reason).map(Ok) }
            }
        )
    }
}

impl Inner {
    /// Waits up to `pool_wait` for a slot; a full pool is typed back-pressure,
    /// recorded, never an unbounded queue. A tool call takes its reserved share
    /// FIRST and then a combined-pool slot (a receiver call never waits on the
    /// share, so the order cannot deadlock); one timeout bounds both waits.
    async fn acquire_slot(
        &self,
        export: &'static str,
        tool: bool,
    ) -> Result<CallPermits, ConnectorCallError> {
        let acquire = async {
            let tool_share = if tool {
                Some(Arc::clone(&self.tool_slots).acquire_owned().await?)
            } else {
                None
            };
            let total = Arc::clone(&self.slots).acquire_owned().await?;
            Ok::<_, tokio::sync::AcquireError>(CallPermits {
                _total: total,
                _tool_share: tool_share,
            })
        };
        match tokio::time::timeout(self.limits.pool_wait, acquire).await {
            Ok(Ok(permits)) => Ok(permits),
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
        deadline_ms: u64,
    ) -> Result<(Store<ExecState>, ConnectorInstance), ConnectorCallError> {
        let exec_state = ExecState::new(self.bridge.clone(), self.app_id.clone(), self.call_id)
            .with_memory_limit_mb(self.limits.memory_limit_mb);
        let mut store = new_bounded_store(&self.engine, exec_state, deadline_ms);
        if let Err(e) = store.set_fuel(self.limits.fuel) {
            // Fuel accounting off on an engine `build_engine` always enables it
            // for is a host invariant breach -- never blamed on the guest.
            error!(app_id = %self.app_id, export, error = %e,
                "invariant breach: could not arm the connector fuel budget");
            let err = ConnectorCallError::Internal(format!("failed to arm fuel budget: {e}"));
            self.record(export, &err, started);
            return Err(err);
        }
        let instantiated = match &self.pre {
            ConnectorPreKind::Plain(pre) => pre
                .instantiate_async(&mut store)
                .await
                .map(ConnectorInstance::Plain),
            ConnectorPreKind::Tools(pre) => pre
                .instantiate_async(&mut store)
                .await
                .map(ConnectorInstance::Tools),
        };
        match instantiated {
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
    fn finish<T, E>(
        &self,
        export: &'static str,
        started: Instant,
        store: &Store<ExecState>,
        outcome: wasmtime::Result<Result<T, E>>,
        guest_err: fn(E) -> ConnectorCallError,
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
            Ok(Err(guest_error)) => Err(guest_err(guest_error)),
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

/// Total byte size of a guest-built template (method + url + every header
/// name/value + body + every secret-ref slot/handle), saturating.
fn template_len(t: &HttpRequestTpl) -> usize {
    let headers: usize = t
        .headers
        .iter()
        .map(|h| h.name.len().saturating_add(h.value.len()))
        .fold(0, usize::saturating_add);
    let refs: usize = t
        .secret_refs
        .iter()
        .map(|(a, b)| a.len().saturating_add(b.len()))
        .fold(0, usize::saturating_add);
    t.method
        .len()
        .saturating_add(t.url.len())
        .saturating_add(headers)
        .saturating_add(t.body.as_ref().map_or(0, Vec::len))
        .saturating_add(refs)
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
            ConnectorCallError::PayloadTooLarge {
                what: "args",
                len: 2,
                max: 1,
            }
            .kind(),
            ConnectorCallError::NoToolProvider.kind(),
            ConnectorCallError::Tool(ToolError::TargetUnavailable).kind(),
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

    /// regression (PR #984 review claim 2): the epoch deadline cannot interrupt a
    /// guest parked on a HOST await (a stage that never answers a `tool.invoke`
    /// host call). The call must still end near its deadline with a typed
    /// `DeadlineExceeded`, and the tool permit must come back -- never be held
    /// by a hung blocking task.
    #[tokio::test]
    async fn a_tool_call_hung_on_a_host_await_is_bounded_by_its_deadline() {
        use crate::engine::{build_engine, LinkerCache};
        use crate::wire::Connection;
        use clap::Parser;
        use sha2::{Digest, Sha256};

        const TOOLS: &[u8] = include_bytes!("../tests/fixtures/connector_tools_fixture.wasm");
        let app_id = "waddles.core.test.connector-hang";
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse");
        let engine = build_engine(&cfg).expect("engine");
        let _ticker = crate::engine::spawn_epoch_ticker(&engine);
        let mut h = Sha256::new();
        h.update(TOOLS);
        let manifest = VerifiedManifest::new(
            app_id,
            format!("sha256:{:x}", h.finalize()),
            WitWorld::ConnectorTools,
            std::collections::HashSet::new(),
        );
        // A stage that reads the host call and NEVER answers it.
        let (tx, mut rx) = tokio::sync::mpsc::channel(8);
        let connection = Connection::new(tx);
        let silent_stage = tokio::spawn(async move {
            let _held_open = rx.recv().await;
            std::future::pending::<()>().await;
        });
        let host = ConnectorHost::load(
            &engine,
            &LinkerCache::new(engine.clone()),
            &manifest,
            TOOLS.to_vec(),
            ConnectorLimits::from_config(&cfg),
            Some(HostBridge::new(connection)),
        )
        .await
        .expect("tools connector loads");
        let before = host.available_tool_slots();

        let started = Instant::now();
        let outcome = tokio::time::timeout(
            Duration::from_secs(10),
            host.call_tool_with_deadline(
                "relay".into(),
                br#"{"target":"waddles.core.other","tool":"x","args":{}}"#.to_vec(),
                Some(300),
            ),
        )
        .await
        .expect("a hung host await must not outlive the deadline by seconds");
        let err = outcome.expect_err("the hung call must fail, not fabricate a result");
        assert!(
            matches!(err, ConnectorCallError::DeadlineExceeded(_)),
            "{err:?}"
        );
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "took {:?}",
            started.elapsed()
        );
        // The permit is released once the bounded task unwinds.
        let give_up = Instant::now() + Duration::from_secs(3);
        while host.available_tool_slots() != before {
            assert!(Instant::now() < give_up, "tool permit never came back");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        silent_stage.abort();
    }
}
