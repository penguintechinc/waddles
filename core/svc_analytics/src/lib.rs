//! `svc-analytics`: the analytics layer-A writer. See `README.md` and
//! `docs/analytics-foundation.md` for the contract, tiering and privacy rules.
//!
//! Extension points for later work items (not built here): the layer B trend builder
//! reads `analytics_fp.member_daily` as the `trend_builder` role in its own service; the
//! bad-actor/bot signal writer owns `analytics_fp.member_signals_daily` and plugs into
//! `aggregate::build_batch` (Premium-gated, admin-only); read endpoints live in hub-api.

pub mod aggregate;
pub mod config;
pub mod consumer;
pub mod contract;
pub mod crypto;
pub mod db;
pub mod flag;
pub mod gate;
pub mod health;
pub mod jobs;
pub mod metrics;

use std::net::SocketAddr;

use anyhow::Context;
use tokio::sync::watch;

/// Default service name for telemetry.
pub const SERVICE_NAME: &str = "svc-analytics";

/// Runs the service until SIGINT/SIGTERM.
///
/// # Errors
/// Configuration, connection or listener failures.
pub async fn run() -> anyhow::Result<()> {
    crypto::ensure_installed();
    let cfg = config::Config::from_env().map_err(anyhow::Error::msg)?;
    let (mut guard, _level, _registry) =
        penguin_logging::init(penguin_logging::ServiceConfig::from_env(SERVICE_NAME));
    tracing::info!(stream = %cfg.stream, group = %cfg.group, "svc-analytics starting");

    let addr: SocketAddr = cfg.grpc_addr.parse().context("GRPC_HEALTH_PORT")?;
    let (tx, rx) = watch::channel(false);
    let ready = health::Ready::new();

    let mut health_rx = rx.clone();
    let health = tokio::spawn(health::serve(addr, ready.clone(), async move {
        let _ = health_rx.wait_for(|v| *v).await;
    }));

    let db = db::PgStore {
        db: db::PgDb::connect(cfg.database_url.expose())
            .await
            .map_err(|e| anyhow::anyhow!("{e}"))?,
        session_max_secs: cfg.session_max_secs,
    };
    let client = redis::Client::open(cfg.valkey_url.expose()).context("VALKEY_URL")?;
    let conn = redis::aio::ConnectionManager::new(client)
        .await
        .context("valkey connect")?;
    let consumer = consumer::Consumer::new(
        cfg,
        db,
        conn,
        flag::FlagGate::from_env(),
        metrics::Metrics::new(),
        ready,
    );

    let signal_tx = tx.clone();
    tokio::spawn(async move {
        wait_for_signal().await;
        let _ = signal_tx.send(true);
    });
    consumer.run(rx).await;
    let _ = tx.send(true);
    let _ = health.await;
    guard.shutdown();
    Ok(())
}

async fn wait_for_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = term.recv() => {}
            }
        }
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

/// `--healthcheck`: one gRPC health `Check` against the configured listener.
///
/// # Errors
/// Not serving, or the listener is unreachable.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port = std::env::var("GRPC_HEALTH_PORT").unwrap_or_else(|_| "50209".into());
    health::check(&format!("127.0.0.1:{port}")).await
}
