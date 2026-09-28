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
    bundles: RwLock<HashMap<String, LoadedBundle>>,
    /// Advances `engine`'s epoch on a fixed tick (spec SS7.2/SS7.3,
    /// assumption A16's executor-side half) so `on_invoke`'s
    /// `Store::set_epoch_deadline` and `Store::epoch_deadline_trap` calls
    /// actually fire -- without this ticker the epoch counter never moves
    /// and no call would ever time out. Aborted on `Drop` so tests don't
    /// leak tasks.
    epoch_ticker: tokio::task::JoinHandle<()>,
    /// Platform Ed25519 public key(s) `on_load` checks every fetched
    /// sidecar's signature against (spec SS5.6). Derived leniently from
    /// `cfg.bundle_signing_public_keys` -- unset/blank yields an empty set
    /// (verification skipped, see that field's own doc for why this is
    /// safe); a value that IS set but fails to parse propagates as a
    /// `Config` error from this constructor, same as any other malformed
    /// config field.
    signing_keys: PlatformPublicKeys,
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
        let signing_keys = PlatformPublicKeys::from_cli(cfg)?;
        Ok(Self {
            engine,
            linker,
            source,
            max_call_timeout_ms: cfg.executor_max_call_timeout_ms,
            default_memory_limit_mb: cfg.executor_memory_limit_mb,
            max_memory_limit_mb: cfg.executor_max_memory_limit_mb,
            bundles: RwLock::new(HashMap::new()),
            epoch_ticker,
            signing_keys,
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
        let (bytes, sidecar_bytes) = self
            .source
            .fetch(&body.component_key, &body.sidecar_key)
            .await
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;

        verify_digest(&bytes, &body.digest)
            .map_err(|e| error_body(ErrorCode::DigestMismatch, e.to_string()))?;

        // Artifact integrity (spec SS5.6/Gemini review condition 9): refuse
        // to instantiate a component whose signed sidecar doesn't verify
        // against a configured platform public key, BEFORE the
        // (expensive, and otherwise-trusting) `Component::new` compile
        // step below. Skipped only when no platform key is configured at
        // all (`self.signing_keys.is_empty()`) -- unreachable in
        // production, see `crate::signing::PlatformPublicKeys`'s own doc.
        if !self.signing_keys.is_empty() {
            crate::signing::verify_artifact_signature(
                &sidecar_bytes,
                &self.signing_keys,
                &body.app_id,
                &body.version,
                &body.digest,
            )
            .map_err(|e| error_body(ErrorCode::LoadFailed, e.to_string()))?;
        }

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
            let payload = signing_payload(APP_ID, VERSION, digest, APPROVAL_ID);
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
}
