//! `bundle-executor`: the Waddles credential-less WASM bundle sandbox
//! host (M2). See `docs/superpowers/specs/2026-09-14-rust-data-plane-
//! design.md` SS4.5/SS7 for the full design; module-level docs below cover
//! what each piece does.
//!
//! **Scope of this pass** (see individual module docs for detail):
//! - **Real**: wasmtime component-model instantiation against the
//!   committed `wit/waddle-bundle/stage.wit` world (`crate::engine`); all
//!   eight WIT host imports serviced over the wire protocol
//!   (`crate::host`); the native `wasi:sockets` denial (assumption A19,
//!   `crate::host::build_wasi_ctx`); the length-prefixed frame protocol
//!   and its request/reply dispatch (`crate::wire`, using the landed
//!   `penguin-bundle-host::wire` module directly); the per-call epoch
//!   deadline (`crate::engine::ticks_for_deadline` + `crate::invoke`);
//!   SHA-256 digest verification (`crate::invoke::verify_digest`); the
//!   bucket `GET` that supplies a bundle's component bytes
//!   (`crate::bucket::BucketComponentSource`, a hand-rolled SigV4-signed
//!   HTTP/1.1 client wired into `Executor` in place of the fail-closed
//!   `crate::invoke::UnimplementedBucketSource` this production path used
//!   before this pass -- that stub now backs only the tests exercising its
//!   own fail-closed behavior; see `crate::bucket`'s doc for why not
//!   `object_store`); the sidecar's Ed25519 signature verification against
//!   `BUNDLE_SIGNING_PUBLIC_KEYS` (`crate::signing`, spec SS5.6/Gemini
//!   review condition 9) -- `BucketComponentSource` now fetches the
//!   sidecar alongside the component, and `Executor::on_load` refuses to
//!   instantiate a component whose sidecar signature doesn't verify.
//! - **Scaffolded with a `TODO`**: precompiled `.cwasm`
//!   caching under `EXECUTOR_PRECOMPILE_DIR` (every `load` JIT-compiles
//!   fresh); the mTLS peer-identity (SPIFFE ID / pinned CN) check on top
//!   of the base rustls handshake in `crate::tls`.

pub mod bucket;
pub mod config;
pub mod engine;
pub mod error;
pub mod heartbeat;
pub mod host;
pub mod invoke;
pub mod manifest;
pub mod probe;
pub mod signing;
pub mod tls;
pub mod wire;

use std::sync::Arc;
use std::time::Instant;

use tracing::{error, info, warn};

use crate::bucket::BucketComponentSource;
use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::heartbeat::{Heartbeat, HeartbeatConfig, HeartbeatMetrics};
use crate::invoke::{ComponentSource, Executor};

/// `tracing`/OTel service name (spec SS12.7's `OTEL_SERVICE_NAME`
/// fallback).
pub const SERVICE_NAME: &str = "bundle-executor";

/// A tiny, dependency-free xorshift64* PRNG for jittering `crate::run`'s
/// host-api reconnect backoff -- regression: readiness gated on executor
/// connection deadlocked rollouts (alpha 2026-10-02): without jitter,
/// every executor replica that lost its connection when a svc pod was
/// replaced reconnects on the exact same exponential schedule, producing a
/// thundering herd against the replacement pod the instant it comes up.
/// Cryptographic randomness is not required -- jitter only needs to avoid
/// a thundering herd, not resist an adversary -- so this mirrors
/// `core/svc_action/src/retry.rs::Jitter` (same rationale: avoid adding a
/// `rand` dependency to a crate that otherwise pins tightly) rather than
/// pulling in a new crate.
struct Jitter(std::sync::atomic::AtomicU64);

impl Jitter {
    /// Seeds from a mix of wall-clock nanos and this process's PID. Never
    /// zero (xorshift's one fixed point) -- falls back to a fixed odd
    /// constant if the clock read is exactly zero.
    fn from_entropy() -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let seed = nanos ^ (u64::from(std::process::id()) << 32) ^ 0x9E37_79B9_7F4A_7C15;
        Self(std::sync::atomic::AtomicU64::new(if seed == 0 {
            0xDEAD_BEEF_CAFE_F00D
        } else {
            seed
        }))
    }

    /// Deterministic constructor for tests.
    #[cfg(test)]
    fn seeded(seed: u64) -> Self {
        Self(std::sync::atomic::AtomicU64::new(if seed == 0 {
            1
        } else {
            seed
        }))
    }

    fn next_u64(&self) -> u64 {
        let mut x = self.0.load(std::sync::atomic::Ordering::Relaxed);
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0.store(x, std::sync::atomic::Ordering::Relaxed);
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// Pseudo-random value in `0..=max` inclusive (`max = 0` always `0`).
    fn uniform(&self, max: u64) -> u64 {
        if max == 0 {
            0
        } else {
            self.next_u64() % (max + 1)
        }
    }
}

/// Half-jitter applied to the reconnect backoff: `base/2 +
/// uniform(0..=base/2)`. Keeps the exponential schedule's growth shape
/// (so the backoff still ramps up on repeated failures) while randomizing
/// enough across concurrently-reconnecting executor replicas to break a
/// thundering herd.
fn jittered_backoff(jitter: &Jitter, base: std::time::Duration) -> std::time::Duration {
    let half_ms = u64::try_from(base.as_millis()).unwrap_or(u64::MAX) / 2;
    std::time::Duration::from_millis(half_ms + jitter.uniform(half_ms))
}

/// Runs the executor: loads config, bootstraps telemetry, then dials the
/// stage and services connections until the process is asked to stop.
/// Reconnects with jittered exponential backoff (see [`jittered_backoff`])
/// on any connection failure (spec SS4.5) rather than exiting -- a stage
/// restart or network blip is not fatal.
pub async fn run() -> Result<(), ExecutorError> {
    let cfg = <CliConfig as clap::Parser>::parse();
    cfg.validate()?;
    // Fails closed at startup if no platform signing key is configured --
    // spec SS5.6 has no supported "verification off" mode in production;
    // `Executor::new`'s own (lenient) derivation from the same field is
    // only ever reached with an empty key set in a test that never called
    // this function. See `crate::signing::PlatformPublicKeys::from_cli_required`.
    crate::signing::PlatformPublicKeys::from_cli_required(&cfg)?;
    cfg.validate_host_api_tls()?;

    init_telemetry();

    // Marks "the long-running process started trying to connect now" --
    // `crate::probe::check_session`'s startup-grace window is measured
    // from this, not from the probe file itself (which doesn't exist
    // until the first successful connection).
    if let Err(e) = probe::record_start(&cfg.executor_probe_file) {
        warn!(error = %e, probe_file = ?cfg.executor_probe_file, "failed to record liveness probe start marker");
    }

    let source = BucketComponentSource::from_cli(&cfg)?;
    let executor = Arc::new(Executor::new(&cfg, source)?);
    let heartbeat_cfg = HeartbeatConfig::from_cli(&cfg);
    let heartbeat_metrics = Arc::new(HeartbeatMetrics::default());
    info!(
        heartbeat_interval_secs = heartbeat_cfg.interval.as_secs(),
        self_ping_enabled = heartbeat_cfg.self_ping_enabled,
        heartbeat_enabled = heartbeat_cfg.enabled,
        "host-api session liveness configured"
    );

    let mut backoff = std::time::Duration::from_secs(1);
    let backoff_cap = std::time::Duration::from_secs(30);
    let mut attempt: u32 = 0;
    let mut disconnected_at: Option<Instant> = None;
    let jitter = Jitter::from_entropy();
    loop {
        attempt += 1;
        match connect_and_serve(
            &cfg,
            &executor,
            attempt,
            heartbeat_cfg,
            Arc::clone(&heartbeat_metrics),
            disconnected_at,
        )
        .await
        {
            Ok(()) => {
                info!(attempt, "host-api connection closed cleanly, reconnecting");
                backoff = std::time::Duration::from_secs(1);
            }
            Err(e) => {
                let sleep_for = jittered_backoff(&jitter, backoff);
                error!(
                    error = %e,
                    attempt,
                    backoff_s = backoff.as_secs(),
                    sleep_s = sleep_for.as_secs_f64(),
                    "host-api connection failed, retrying"
                );
                tokio::time::sleep(sleep_for).await;
                backoff = (backoff * 2).min(backoff_cap);
            }
        }
        disconnected_at = Some(Instant::now());
        heartbeat_metrics.record_reconnect();
    }
}

/// Dials the stage and runs one connection to completion (spec SS4.5).
/// `attempt` is this process's monotonically increasing connection-attempt
/// counter (1 on the very first dial, logged alongside the peer address so
/// an operator can tell "first connect" from "the Nth reconnect" at a
/// glance). `disconnected_at`, when set, is when the PREVIOUS connection
/// ended -- used only to log how long the host-api link was down once this
/// one's handshake completes (`crate::heartbeat::Heartbeat::on_connected`).
async fn connect_and_serve<S: ComponentSource>(
    cfg: &CliConfig,
    executor: &Arc<Executor<S>>,
    attempt: u32,
    heartbeat_cfg: HeartbeatConfig,
    heartbeat_metrics: Arc<HeartbeatMetrics>,
    disconnected_at: Option<Instant>,
) -> Result<(), ExecutorError> {
    let io = tls::dial_stage(cfg).await?;
    let peer = io
        .get_ref()
        .0
        .peer_addr()
        .map(|a| a.to_string())
        .unwrap_or_else(|_| cfg.stage_host_api_addr.clone());
    info!(peer = %peer, attempt, "host-api connecting");

    let hello = executor.hello(
        cfg.sandbox_gvisor,
        if cfg.sandbox_gvisor { "gvisor" } else { "runc" },
    );
    let probe_file = cfg.executor_probe_file.clone();
    let on_connected_peer = peer.clone();
    let heartbeat = Heartbeat {
        cfg: heartbeat_cfg,
        metrics: heartbeat_metrics,
        probe_file: probe_file.clone(),
        probe_grace: std::time::Duration::from_secs(cfg.executor_grace_secs),
        on_connected: Some(Box::new(move || {
            if let Some(since) = disconnected_at {
                info!(
                    peer = %on_connected_peer,
                    attempt,
                    outage_ms = since.elapsed().as_millis() as u64,
                    "host-api reconnected"
                );
            }
            if let Err(e) = probe::touch(&probe_file) {
                warn!(error = %e, probe_file = ?probe_file, "failed to refresh liveness probe file on connect");
            }
        })),
    };
    wire::run_connection(io, hello, Arc::clone(executor), &peer, heartbeat).await
}

/// Installs this binary's `tracing` subscriber: JSON to stdout, level
/// from `LOG_LEVEL`/`RUST_LOG` (default `info`), matching the standard
/// env var this repo's other services read (spec SS12.7) even though
/// this one carries no OTLP exporter -- see the `Cargo.toml` doc comment
/// on why `penguin-logging` is not used here. Uses `try_init` rather than
/// `init`: a subscriber already being set (a double call, or another
/// component in the process having installed one first) is logged and
/// skipped rather than panicking -- telemetry setup must never crash the
/// process it's instrumenting.
fn init_telemetry() {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_env("LOG_LEVEL")
        .or_else(|_| EnvFilter::try_from_default_env())
        .unwrap_or_else(|_| EnvFilter::new("info"));
    if let Err(e) = tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .with_target(true)
        .try_init()
    {
        // `warn!` still reaches whatever subscriber won the race (this
        // is only reachable when one already exists); a true "nothing is
        // listening at all" case degrades to a silent no-op, same as any
        // other tracing call before a subscriber is installed.
        warn!(error = %e, "tracing subscriber already installed, skipping");
    }
}

/// Installs a permissive `tracing` subscriber exactly once per test
/// process (`try_init` silently no-ops on a second call). Without any
/// subscriber, `tracing`'s macros treat every level as disabled and skip
/// evaluating field-value expressions entirely -- which otherwise leaves
/// every `info!`/`warn!`/`debug!` call site's argument expressions
/// permanently uncovered by `cargo llvm-cov` even when the call itself
/// runs on every test invocation. Test-only; never linked into the
/// release binary.
#[cfg(test)]
pub(crate) fn init_test_tracing() {
    let _ = tracing_subscriber::fmt()
        .with_test_writer()
        .with_max_level(tracing::Level::TRACE)
        .try_init();
}

/// Runs this binary's own health self-check for the container
/// `HEALTHCHECK` (`rules/general.md`: "native Rust healthcheck subcommand
/// -- never curl"). This process has no HTTP surface to probe (spec
/// SS12.4/SS12.5: no ingress at all), so the check that actually reflects
/// this binary's health is "can it still build the wasmtime engine and
/// linker it depends on for every `load`/`invoke`" -- exercising the same
/// `crate::engine::build_engine`/`build_linker` path `Executor::new` uses.
pub async fn run_healthcheck() -> Result<(), ExecutorError> {
    let cfg = CliConfig::for_healthcheck();
    let engine = engine::build_engine(&cfg)?;
    engine::build_linker(&engine)?;
    Ok(())
}

/// `--healthcheck=session [--max-age <secs>]`: checks `crate::probe`'s
/// on-disk liveness file rather than building a wasmtime engine. This is
/// the check that actually proves the long-running process's host-API
/// session is alive (`run_healthcheck` above only proves the engine/linker
/// still build -- it says nothing about whether `crate::run`'s dial loop
/// is stuck on a half-open connection, the exact incident this subcommand
/// exists to catch). Reads `EXECUTOR_PROBE_FILE`/`EXECUTOR_GRACE_SECONDS`
/// directly from the environment rather than the full `CliConfig::parse`
/// every other entry point uses: this is invoked as a brand new, short-
/// lived process by Kubernetes' exec probe, sharing nothing with the
/// long-running process but the filesystem and environment, and should not
/// need `STAGE_HOST_API_ADDR`/mTLS material to be set just to check a
/// file.
pub async fn run_session_healthcheck(
    max_age: Option<std::time::Duration>,
) -> Result<(), ExecutorError> {
    let probe_file = std::env::var("EXECUTOR_PROBE_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::PathBuf::from(probe::DEFAULT_PROBE_FILE_PATH));
    let grace_secs = std::env::var("EXECUTOR_GRACE_SECONDS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(probe::DEFAULT_GRACE_SECS);
    let grace = std::time::Duration::from_secs(grace_secs);
    let max_age = max_age.unwrap_or(grace);
    probe::check_session(&probe_file, max_age, grace).map_err(ExecutorError::Config)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::invoke::UnimplementedBucketSource;

    #[tokio::test]
    async fn run_healthcheck_succeeds() -> Result<(), ExecutorError> {
        run_healthcheck().await
    }

    #[test]
    fn init_telemetry_is_safe_to_call_more_than_once() {
        // Exercises both the successful-install branch (whichever call
        // wins the race across the test binary) and the
        // already-installed branch -- `init_telemetry` must never panic
        // either way.
        init_telemetry();
        init_telemetry();
    }

    // regression: readiness gated on executor connection deadlocked
    // rollouts (alpha 2026-10-02) -- `crate::run`'s reconnect backoff must
    // be jittered so every executor replica reconnecting to a replaced svc
    // pod doesn't hit it on the exact same schedule.
    #[test]
    fn jittered_backoff_stays_within_the_half_jitter_window() {
        let jitter = Jitter::seeded(42);
        let base = std::time::Duration::from_secs(8);
        for _ in 0..100 {
            let got = jittered_backoff(&jitter, base);
            assert!(got >= base / 2, "{got:?} below half-jitter floor");
            assert!(got <= base, "{got:?} above base ceiling");
        }
    }

    #[test]
    fn jittered_backoff_varies_across_successive_calls() {
        // A fixed, non-jittered delay would return the exact same value on
        // every call for a constant `base` -- jitter must actually vary it.
        let jitter = Jitter::seeded(7);
        let base = std::time::Duration::from_secs(4);
        let samples: std::collections::HashSet<_> =
            (0..20).map(|_| jittered_backoff(&jitter, base)).collect();
        assert!(
            samples.len() > 1,
            "expected varying jittered delays, got a single repeated value"
        );
    }

    #[test]
    fn jittered_backoff_handles_zero_base_without_panicking() {
        let jitter = Jitter::seeded(1);
        assert_eq!(
            jittered_backoff(&jitter, std::time::Duration::ZERO),
            std::time::Duration::ZERO
        );
    }

    #[test]
    fn jitter_seeded_zero_does_not_get_stuck_at_the_fixed_point() {
        // xorshift64*'s one fixed point is state == 0; `Jitter::seeded(0)`
        // must not silently produce an all-zero stream.
        let jitter = Jitter::seeded(0);
        let first = jitter.next_u64();
        let second = jitter.next_u64();
        assert_ne!(first, 0);
        assert_ne!(first, second);
    }

    fn test_config_for(addr: std::net::SocketAddr) -> CliConfig {
        use clap::Parser;
        let mut cfg = CliConfig::try_parse_from(["bundle-executor", "--stage-host-api-addr", "x"])
            .expect("static test args always parse");
        cfg.stage_host_api_addr = addr.to_string();
        cfg
    }

    /// Drives `connect_and_serve` end to end against a real local TLS
    /// listener acting as a minimal fake stage: completes the TLS
    /// handshake, answers `hello` with `hello-ok`, then sends `shutdown`
    /// -- proving `connect_and_serve`'s dial -> hello -> `run_connection`
    /// chain, not just its individual pieces in isolation.
    #[tokio::test]
    async fn connect_and_serve_completes_against_a_real_local_stage(
    ) -> Result<(), Box<dyn std::error::Error>> {
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloLimits, HelloOkBody, Message, ShutdownBody,
        };

        let ca_key = rcgen::KeyPair::generate()?;
        let ca_params = rcgen::CertificateParams::new(vec!["bundle-executor-test-ca".to_string()])?;
        let ca_cert = ca_params.self_signed(&ca_key)?;
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate()?;
        let server_params = rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()])?;
        let server_cert = server_params.signed_by(&server_key, &issuer)?;

        let server_config = rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(
                vec![rustls_pki_types::CertificateDer::from(
                    server_cert.der().to_vec(),
                )],
                rustls_pki_types::PrivateKeyDer::try_from(server_key.serialize_der())
                    .map_err(|e| e.to_string())?,
            )?;
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;

        let ca_path = std::env::temp_dir().join(format!(
            "bundle-executor-test-{}-connect-and-serve-ca.pem",
            std::process::id()
        ));
        std::fs::write(&ca_path, ca_cert.pem())?;

        let stage_task = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.expect("accept");
            let mut tls = acceptor.accept(tcp).await.expect("server tls handshake");

            let hello = read_frame(&mut tls).await.expect("hello");
            write_frame(
                &mut tls,
                &Frame::new(
                    hello.id,
                    Message::HelloOk(HelloOkBody {
                        stage: "svc-process".to_string(),
                        protocol_version: 1,
                        limits: HelloLimits {
                            call_timeout_ms: 2000,
                            memory_mb: 64,
                            max_concurrent_calls: 32,
                        },
                    }),
                ),
            )
            .await
            .expect("write hello-ok");

            write_frame(
                &mut tls,
                &Frame::new(1, Message::Shutdown(ShutdownBody { grace_ms: 10 })),
            )
            .await
            .expect("write shutdown");
        });

        let mut cfg = test_config_for(addr);
        cfg.host_api_ca_file = Some(ca_path.clone());
        let executor = Arc::new(Executor::new(&cfg, UnimplementedBucketSource)?);

        connect_and_serve(
            &cfg,
            &executor,
            1,
            crate::heartbeat::HeartbeatConfig::disabled(),
            Arc::new(crate::heartbeat::HeartbeatMetrics::default()),
            None,
        )
        .await?;
        stage_task.await?;
        let _ = std::fs::remove_file(&ca_path);
        Ok(())
    }
}
