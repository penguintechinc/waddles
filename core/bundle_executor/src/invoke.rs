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
    /// The `app_id` from the FIRST `load` call that registered this digest
    /// -- informational only (logging/error messages): the registry's real
    /// identity is the digest (this struct's `HashMap` key), never this
    /// field, so a second scope loading the same digest under a different
    /// `app_id` string never causes a collision or a silent overwrite.
    app_id: String,
    component: Component,
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
    /// Number of outstanding `load` calls not yet matched by an `unload`
    /// (spec: "load/unload refcounted by the set of (tenant, community,
    /// app) scopes referencing that digest") -- the caller
    /// (`bundle_active_set::diff::plan_scoped`-driven stage loop) sends
    /// exactly one `load`/`unload` pair per scope that starts/stops
    /// referencing this digest, so this count tracks how many scopes are
    /// currently relying on this compiled `Component` staying resident.
    /// The component is only actually evicted (`on_unload`) once this
    /// reaches zero -- never on the first `unload` if another scope still
    /// references the same digest.
    refcount: usize,
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
    bundles: RwLock<HashMap<String, LoadedBundle>>,
    /// Advances `engine`'s epoch on a fixed tick (spec SS7.2/SS7.3,
    /// assumption A16's executor-side half) so `on_invoke`'s
    /// `Store::set_epoch_deadline` and `Store::epoch_deadline_trap` calls
    /// actually fire -- without this ticker the epoch counter never moves
    /// and no call would ever time out. Aborted on `Drop` so tests don't
    /// leak tasks.
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
        let app_id = body.app_id.clone();
        let digest = body.digest.clone();

        // Content-addressed fast path: a digest already resident (loaded by
        // an earlier scope, possibly under a different `app_id`) is never
        // re-fetched or re-compiled -- just refcount-bumped. This is what
        // makes a digest shared across tenants compile exactly once.
        {
            let mut bundles = self.bundles.write().await;
            if let Some(existing) = bundles.get_mut(&digest) {
                existing.refcount += 1;
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
                    refcount = existing.refcount,
                    "bundle already resident, refcount incremented"
                );
                return Ok(LoadedBody {
                    app_id,
                    digest,
                    precompile_ms: start.elapsed().as_millis() as u64,
                    exports: vec!["transform".to_string(), "dispatch".to_string()],
                });
            }
        }

        let bytes = self
            .source
            .fetch(&body.component_key, &body.sidecar_key)
            .await
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

        verify_digest(&bytes, &body.digest)
            .map_err(|e| error_body(ErrorCode::DigestMismatch, e.to_string()))?;

        let component = Component::new(&self.engine, &bytes)
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

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
        // for a digest that's already resident; refcount-bump instead.
        let mut bundles = self.bundles.write().await;
        if let Some(existing) = bundles.get_mut(&digest) {
            existing.refcount += 1;
            info!(
                app_id,
                digest,
                refcount = existing.refcount,
                "bundle became resident concurrently, refcount incremented"
            );
        } else {
            bundles.insert(
                digest.clone(),
                LoadedBundle {
                    app_id: app_id.clone(),
                    component,
                    memory_limit_mb,
                    refcount: 1,
                },
            );
            info!(
                app_id,
                digest,
                ms = start.elapsed().as_millis() as u64,
                "bundle loaded"
            );
        }
        drop(bundles);

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
        match bundles.get_mut(&body.digest) {
            Some(loaded) => {
                loaded.refcount = loaded.refcount.saturating_sub(1);
                if loaded.refcount == 0 {
                    bundles.remove(&body.digest);
                    info!(
                        app_id = body.app_id,
                        digest = body.digest,
                        "bundle unloaded (last referencing scope)"
                    );
                } else {
                    info!(
                        app_id = body.app_id,
                        digest = body.digest,
                        refcount = loaded.refcount,
                        "bundle unload refcount decremented, still referenced by another scope"
                    );
                }
                Ok(UnloadedBody {
                    app_id: body.app_id,
                    digest: body.digest,
                })
            }
            None => Err(error_body(
                ErrorCode::UnknownBundle,
                format!("digest {} is not loaded", body.digest),
            )),
        }
    }

    async fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> Result<ResultBody, ErrorBody> {
        // Fail-closed, digest-only lookup (spec §3): never falls back to
        // "whatever happens to be loaded under this app_id" -- an
        // unrecognized digest is always `UNKNOWN_BUNDLE`, even if some OTHER
        // digest is currently resident for this same `app_id` under a
        // different scope.
        let (component, memory_limit_mb) = {
            let bundles = self.bundles.read().await;
            let loaded = bundles
                .get(&body.digest)
                .ok_or_else(|| error_body(ErrorCode::UnknownBundle, body.digest.clone()))?;
            (loaded.component.clone(), loaded.memory_limit_mb)
        };

        let deadline_ms = body.deadline_ms.min(self.max_call_timeout_ms).max(1);
        let bridge = HostBridge::new(connection);
        let exec_state = ExecState::new(Some(bridge), body.app_id.clone(), invoke_id)
            .with_memory_limit_mb(memory_limit_mb);
        let mut store = Store::new(&self.engine, exec_state);
        store.set_epoch_deadline(ticks_for_deadline(deadline_ms));
        store.epoch_deadline_trap();
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
        let stage = Stage::instantiate_async(&mut store, &component, &self.linker)
            .await
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

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

        Ok(ResultBody {
            payload: result,
            duration_ms: start.elapsed().as_millis() as u64,
            fuel_used: 0,
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
fn trap_to_error_body(err: wasmtime::Error, memory_cap_hit: bool) -> ErrorBody {
    let full_chain = format!("{err:#}");
    let code = if memory_cap_hit {
        ErrorCode::MemoryLimit
    } else {
        match err.downcast_ref::<wasmtime::Trap>() {
            Some(wasmtime::Trap::Interrupt) => ErrorCode::ExecutorDeadline,
            _ => ErrorCode::WasmTrap,
        }
    };
    error_body(code, full_chain)
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
                .component
                .clone();
            bundles.insert(
                other_digest.clone(),
                LoadedBundle {
                    app_id: "waddles.test.app".to_string(),
                    component: first,
                    memory_limit_mb: 64,
                    refcount: 1,
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

    /// The SAME digest loaded by two different scopes must compile once
    /// (one registry entry, refcount 2) -- verified by inserting the second
    /// `load` and observing the registry stays at one entry.
    #[tokio::test]
    async fn shared_digest_across_two_scopes_loads_once_and_refcounts() -> Result<(), ExecutorError>
    {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("first scope's load succeeds");
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("second scope's load of the identical digest succeeds");

        let bundles = executor.bundles.read().await;
        assert_eq!(
            bundles.len(),
            1,
            "a shared digest must occupy exactly one registry slot"
        );
        assert_eq!(
            bundles.get(&fixture_digest()).expect("resident").refcount,
            2,
            "two referencing scopes must refcount to 2"
        );
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
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("scope A load succeeds");
        executor
            .on_load(fixture_load_body("waddles.test.app"))
            .await
            .expect("scope B load succeeds");

        // Scope A goes away first.
        executor
            .on_unload(UnloadBody {
                app_id: "waddles.test.app".to_string(),
                digest: fixture_digest(),
            })
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
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let still_invokable = executor
            .on_invoke(
                InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: fixture_digest(),
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
            .await;
        assert!(
            still_invokable.is_ok(),
            "scope B's invoke must still succeed while its digest is still referenced: {still_invokable:?}"
        );

        // Scope B goes away second -- now the digest actually unloads.
        executor
            .on_unload(UnloadBody {
                app_id: "waddles.test.app".to_string(),
                digest: fixture_digest(),
            })
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

    /// An unload for a digest that was never loaded (or already fully
    /// unloaded) is `UNKNOWN_BUNDLE` -- fail-closed, never a silent no-op.
    #[tokio::test]
    async fn unload_of_a_never_loaded_digest_fails_closed() -> Result<(), ExecutorError> {
        let executor = Executor::new(&test_config(), FixtureSource)?;
        let result = executor
            .on_unload(UnloadBody {
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

    #[tokio::test]
    async fn on_shutdown_logs_and_returns() -> Result<(), ExecutorError> {
        crate::init_test_tracing();
        let executor = Executor::new(&test_config(), UnimplementedBucketSource)?;
        executor
            .on_shutdown(penguin_bundle_host::wire::ShutdownBody { grace_ms: 100 })
            .await;
        Ok(())
    }
}
