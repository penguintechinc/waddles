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
//!   SHA-256 digest verification (`crate::invoke::verify_digest`).
//! - **Scaffolded with a `TODO`**: the bucket `GET` that supplies a
//!   bundle's component bytes (`crate::invoke::UnimplementedBucketSource`,
//!   fails closed rather than fabricating bytes); precompiled `.cwasm`
//!   caching under `EXECUTOR_PRECOMPILE_DIR` (every `load` JIT-compiles
//!   fresh); the mTLS peer-identity (SPIFFE ID / pinned CN) check on top
//!   of the base rustls handshake in `crate::tls`; the per-bundle
//!   `wasmtime::StoreLimits` memory-limit override in `crate::invoke`.

pub mod config;
pub mod engine;
pub mod error;
pub mod host;
pub mod invoke;
pub mod tls;
pub mod wire;

use std::sync::Arc;

use tracing::{info, warn};

use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::invoke::{Executor, UnimplementedBucketSource};

/// `tracing`/OTel service name (spec SS12.7's `OTEL_SERVICE_NAME`
/// fallback).
pub const SERVICE_NAME: &str = "bundle-executor";

/// Runs the executor: loads config, bootstraps telemetry, then dials the
/// stage and services connections until the process is asked to stop.
/// Reconnects with exponential backoff on any connection failure (spec
/// SS4.5) rather than exiting -- a stage restart or network blip is not
/// fatal.
pub async fn run() -> Result<(), ExecutorError> {
    let cfg = <CliConfig as clap::Parser>::parse();
    cfg.validate()?;

    init_telemetry();

    let executor = Arc::new(Executor::new(&cfg, UnimplementedBucketSource)?);

    let mut backoff = std::time::Duration::from_secs(1);
    let backoff_cap = std::time::Duration::from_secs(30);
    loop {
        match connect_and_serve(&cfg, &executor).await {
            Ok(()) => {
                info!("host-api connection closed cleanly, reconnecting");
                backoff = std::time::Duration::from_secs(1);
            }
            Err(e) => {
                warn!(error = %e, backoff_s = backoff.as_secs(), "host-api connection failed, retrying");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(backoff_cap);
            }
        }
    }
}

async fn connect_and_serve(
    cfg: &CliConfig,
    executor: &Arc<Executor<UnimplementedBucketSource>>,
) -> Result<(), ExecutorError> {
    let io = tls::dial_stage(cfg).await?;
    let hello = executor.hello(
        cfg.sandbox_gvisor,
        if cfg.sandbox_gvisor { "gvisor" } else { "runc" },
    );
    wire::run_connection(io, hello, Arc::clone(executor)).await
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

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

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

        connect_and_serve(&cfg, &executor).await?;
        stage_task.await?;
        let _ = std::fs::remove_file(&ca_path);
        Ok(())
    }
}
