//! Startup wiring for dynamic (flag ON) per-community Twitch sessions.
//!
//! [`try_start_twitch_sessions`] builds the hub-api [`ConnectionResolver`],
//! the Valkey lease and the [`SessionManager`], and runs the manager as a
//! background task until process shutdown. Every misconfiguration is a fatal
//! `Err` -- dynamic mode never falls back to the static identity.

use std::future::Future;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use connection_resolver::{
    ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics as ConnResolverMetrics,
};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::config::{CliConfig, Config};
use crate::twitch_sessions::lease::{SessionLease, ValkeyLease};
use crate::twitch_sessions::manager::{RealRunner, SessionDeps, SessionManager};
use crate::twitch_sessions::metrics::SessionMetrics;
use crate::{outbound, publish, scope_resolver, telemetry, ReceiverReadiness};
use connection_resolver::ConnectionResolve;

/// Debug-logs hub-api resolve/list outcomes. Counting is deliberately left to
/// the session manager and connector (`SessionMetrics`), which already record
/// the same events under fixed label sets -- counting here too would double
/// every series.
struct ResolverTelemetry;

impl ConnResolverMetrics for ResolverTelemetry {
    fn resolve(&self, _kind: ConnectionKind, outcome: &'static str, latency: Duration) {
        tracing::debug!(
            outcome,
            latency_ms = latency.as_millis(),
            "connection resolve"
        );
    }

    fn resolve_failed(&self, outcome: &'static str, latency: Duration) {
        tracing::debug!(
            outcome,
            latency_ms = latency.as_millis(),
            "connection resolve failed"
        );
    }

    fn list(&self, outcome: &'static str) {
        tracing::debug!(outcome, "connection binding list");
    }
}

/// Checks everything dynamic mode needs before any task starts: a service-JWT
/// endpoint, an acceptable hub transport, and an installed scope resolver
/// (`scope_resolver_installed`, passed in so it is testable). Messages name
/// settings only, never values.
pub fn validate_dynamic_prereqs(
    cli: &CliConfig,
    scope_resolver_installed: bool,
) -> anyhow::Result<()> {
    if cli.service_jwt_token_endpoint.is_empty() {
        anyhow::bail!(
            "{} is ON but SERVICE_JWT_TOKEN_ENDPOINT is unset: no machine JWT for twitch sessions",
            scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG
        );
    }
    scope_resolver::check_transport(&cli.hub_api_url, &cli.environment)
        .map_err(|e| anyhow::anyhow!(e))?;
    if !scope_resolver_installed {
        anyhow::bail!(
            "{} is ON but the dataplane scope resolver is not installed: \
             per-community twitch scope cannot be resolved",
            scope_resolver::INGEST_DYNAMIC_CONNECTIONS_FLAG
        );
    }
    Ok(())
}

/// Runs `manager` as a background task that ends (after stopping every
/// session) when `shutdown` completes.
pub fn spawn_manager_task<R, L>(
    manager: SessionManager<R, L>,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> JoinHandle<()>
where
    R: ConnectionResolve + 'static,
    L: SessionLease + 'static,
{
    let (tx, rx) = oneshot::channel();
    tokio::spawn(async move {
        shutdown.await;
        let _ = tx.send(());
    });
    tokio::spawn(manager.run(rx))
}

/// `true` when the manager task ended without a requested shutdown (a panic,
/// or any early return) -- the process must then exit rather than sit healthy
/// with zero Twitch sessions.
pub fn manager_exit_is_fatal(
    result: &Result<(), tokio::task::JoinError>,
    shutdown_requested: bool,
) -> bool {
    result.is_err() || !shutdown_requested
}

/// Starts per-community Twitch sessions (flag ON) and returns immediately;
/// the spine/Valkey connects and the manager run in a background task.
/// Returns `Err` -- the process must not start -- when the service-JWT
/// endpoint is unset, the hub transport is refused, the scope resolver is not
/// installed, the binding keyring/active kid is unusable, or the spine config
/// is missing. A spine/Valkey that never connects within the grace period
/// exits the process (same contract as the static receivers).
pub fn try_start_twitch_sessions(
    config: &Config,
    metrics: Arc<telemetry::IngestMetrics>,
    readiness: Arc<ReceiverReadiness>,
) -> anyhow::Result<()> {
    let cli = &config.cli;
    validate_dynamic_prereqs(cli, publish::scope_resolver_installed())?;
    let keyring = crate::resolve_binding_keyring(config, "twitch sessions").ok_or_else(|| {
        anyhow::anyhow!(
            "twitch sessions need a valid ENVELOPE_BINDING_KEYS + ENVELOPE_BINDING_ACTIVE_KID"
        )
    })?;
    let spine_cfg = penguin_spine::SpineConfig::from_env()
        .map_err(|e| anyhow::anyhow!("spine config unavailable for twitch sessions: {e}"))?;
    let redis_client = outbound::build_redis_client(&spine_cfg).map_err(|e| {
        // Type only: a redis error Display can echo the URL + password.
        anyhow::anyhow!(
            "valkey client build failed for twitch sessions ({:?})",
            e.kind()
        )
    })?;
    let resolver = Arc::new(
        ConnectionResolver::new(
            ResolverConfig::new(
                cli.hub_api_url.clone(),
                cli.environment.clone(),
                cli.service_jwt_token_endpoint.clone(),
                cli.service_jwt_sa_token_path.clone(),
            ),
            Arc::new(ResolverTelemetry),
        )
        .map_err(|e| anyhow::anyhow!("connection resolver unavailable: {e}"))?,
    );

    let active_kid = cli.binding_active_kid.clone();
    let scope = Arc::new(config.ingest_scope());
    let (host, port, tls) = (
        cli.twitch_irc_host.clone(),
        cli.twitch_irc_port,
        cli.twitch_irc_use_tls,
    );
    let interval = Duration::from_secs(cli.twitch_reconcile_interval_secs);
    let resolve_timeout = Duration::from_secs(cli.twitch_resolve_timeout_secs);
    let backoff_max = Duration::from_secs(cli.spine_connect_backoff_max_secs);
    let grace = Duration::from_secs(cli.spine_connect_grace_secs);

    readiness.twitch_irc.mark_enabled();
    tokio::spawn(async move {
        let spine_metrics: Arc<dyn penguin_spine::SpineMetrics> = metrics.clone();
        let Some(appender) = crate::connect_spine_with_retry(
            spine_cfg,
            spine_metrics,
            metrics.as_ref(),
            "twitch_sessions",
            &readiness.twitch_irc,
            backoff_max,
            Instant::now() + grace,
        )
        .await
        else {
            tracing::error!(
                grace_secs = grace.as_secs(),
                "spine connect still failing after grace period; exiting so Kubernetes restarts \
                 this pod visibly (twitch sessions never started)"
            );
            std::process::exit(1);
        };
        let conn = match redis_client.get_multiplexed_async_connection().await {
            Ok(conn) => conn,
            Err(err) => {
                readiness.twitch_irc.set_ready(false);
                tracing::error!(error_kind = ?err.kind(), "valkey connect failed; exiting so Kubernetes restarts this pod (twitch sessions never started)");
                std::process::exit(1);
            }
        };
        let session_metrics: Arc<dyn SessionMetrics> = metrics.clone();
        let runner = RealRunner::new(
            resolver.clone(),
            Arc::new(appender),
            metrics,
            Arc::new(keyring),
            active_kid,
            scope,
            host,
            port,
            tls,
        )
        .with_session_metrics(session_metrics.clone())
        .with_resolve_timeout(resolve_timeout);
        let manager = SessionManager::new(
            resolver,
            Arc::new(ValkeyLease::new(conn)),
            SessionDeps {
                runner: Arc::new(runner),
                metrics: session_metrics,
            },
        )
        .with_timing(interval, resolve_timeout);
        tracing::info!("twitch per-community sessions started");
        let shutdown_requested = Arc::new(AtomicBool::new(false));
        let flag = shutdown_requested.clone();
        let result = spawn_manager_task(manager, async move {
            crate::shutdown_signal().await;
            flag.store(true, Ordering::SeqCst);
        })
        .await;
        if manager_exit_is_fatal(&result, shutdown_requested.load(Ordering::SeqCst)) {
            readiness.twitch_irc.set_ready(false);
            tracing::error!(
                panicked = result.is_err(),
                "twitch session manager ended unexpectedly; exiting so Kubernetes restarts this pod (no sessions would run)"
            );
            std::process::exit(1);
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The debug-only resolver telemetry must accept every outcome and never panic; it carries
    /// only a fixed outcome label and a latency (no ids, no tokens).
    #[test]
    fn resolver_telemetry_accepts_every_signal_without_panicking() {
        let t = ResolverTelemetry;
        t.resolve(
            ConnectionKind::PlatformOauth,
            "ok",
            Duration::from_millis(3),
        );
        t.resolve_failed("auth", Duration::from_millis(5));
        t.list("ok");
    }
}
