//! `egress-proxy` -- the network-level egress gateway for the Waddles
//! data plane (feature/bundle-egress-gateway, PR #463). The sole path
//! svc-ingest/svc-process/svc-action pods have to the internet once the
//! Helm chart's `egressPolicy` CiliumNetworkPolicies land
//! (default-deny egress for those pods otherwise).
//!
//! Every request must carry:
//! - `Authorization: Bearer <machine JWT>` -- hub-api-issued, verified by
//!   `core/service_auth` (PR #438), scoped `egress:connect`, `sub` one of
//!   the allowed data-plane services.
//! - `X-Waddles-Egress-Assertion: <compact EdDSA JWT>` -- the signed
//!   per-tenant/community/app/port allowlist grant (`net.http.fqdn`/
//!   `.public-ip`/`.private-ip`), signed by the *calling service's own*
//!   per-service Ed25519 key (the same one it signs its machine JWT with)
//!   and verified against the identical hub-api JWKS trust bundle used
//!   for the machine JWT -- see `assertion` module doc for why this is a
//!   second token rather than a machine-JWT claim, and for the redesign
//!   away from a single static hub-api-held signing key.
//!
//! This proxy re-validates the destination against the assertion itself
//! (never trusts the in-process guard's own check), re-resolves DNS
//! itself (never trusts a caller-supplied address), and connects only to
//! that freshly-resolved address -- see `proxy::validate`.
//!
//! # Graceful shutdown (SIGTERM / SIGINT)
//!
//! [`run`] installs SIGTERM and SIGINT handlers. On either signal the
//! proxy **stops accepting new connections**, lets in-flight HTTP requests
//! finish (hyper graceful shutdown -- idle keep-alive connections close at
//! once) and waits for live CONNECT tunnels to end, for at most
//! `SHUTDOWN_DRAIN_SECONDS` (default 25s, under the Kubernetes default 30s
//! `terminationGracePeriodSeconds`). It then returns `Ok(())` -- process
//! exit code 0 -- so a rollout never has to SIGKILL the pod. Work still in
//! flight when the deadline passes is cut off and logged at WARN with the
//! count (`egress_proxy.drain_deadline_exceeded`); the exit is still 0.

pub mod assertion;
pub mod audit;
pub mod auth;
pub mod config;
pub mod dns;
pub mod ip_policy;
pub mod limits;
pub mod metrics;
pub mod proxy;
pub mod telemetry;

use std::sync::Arc;

// `std::env` is process-global; every `#[cfg(test)]` module in this crate
// that reads/writes `Config::from_env`'s variables (this module's own
// tests plus `config::tests`) serializes on this single lock so parallel
// `cargo test` threads never race on the same variables. `tokio::sync::
// Mutex` (not `std::sync::Mutex`) -- this module's tests hold the guard
// across an `.await` (`build_state`/`run_healthcheck` are async), which a
// std mutex guard may not do (`clippy::await_holding_lock`); `config.rs`'s
// own (synchronous) tests take it via `blocking_lock()` instead.
#[cfg(test)]
pub(crate) static ENV_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

use config::Config;
use dns::TokioResolver;
use limits::TenantLimiter;
use proxy::ProxyState;

/// Builds the shared [`ProxyState`] from environment configuration --
/// constructs the hub-api JWKS-backed machine-JWT trust bundle, which now
/// also verifies the allowlist assertion (both tokens are signed by the
/// same per-service key set, see `assertion` module doc), and the
/// per-instance replay cache. `registry` is the shared Prometheus registry
/// `telemetry::init` hands back, so this service's own metrics and the
/// OTel meter provider's `target_info` series render from the same
/// `/metrics` surface.
pub async fn build_state(registry: &prometheus::Registry) -> anyhow::Result<Arc<ProxyState>> {
    let cfg = Config::from_env().map_err(anyhow::Error::from)?;
    let trust_bundle: Arc<dyn service_auth::TrustBundle> = Arc::new(
        service_auth::JwksTrustBundle::new(cfg.machine_jwt_jwks_url.clone()),
    );
    let replay_cache: Arc<dyn assertion::ReplayCache> =
        Arc::new(assertion::InMemoryReplayCache::new());
    let cluster_cidrs = cfg.deny_cluster_cidrs.clone();
    let limiter = TenantLimiter::new(
        cfg.per_tenant_max_connections,
        cfg.per_tenant_bandwidth_bytes_per_sec,
    );
    let metrics = metrics::Metrics::new(registry);

    Ok(Arc::new(ProxyState {
        cfg: Arc::new(cfg),
        trust_bundle,
        replay_cache,
        cluster_cidrs,
        resolver: Arc::new(TokioResolver),
        limiter,
        metrics,
    }))
}

/// Runs the proxy listener (CONNECT/forward-HTTP, `PROXY_LISTEN_PORT`)
/// and the metrics/health listener (`METRICS_PORT`) concurrently. Returns
/// only on a fatal bind error.
pub async fn run() -> anyhow::Result<()> {
    let (telemetry_guard, registry) = telemetry::init("egress-proxy");
    // Held for the process lifetime: dropping it flushes buffered OTel
    // spans/logs/metrics. Never held across `?` early-returns below in a
    // way that would drop it prematurely -- it outlives both servers.
    let _telemetry_guard = telemetry_guard;

    let state = build_state(&registry).await?;
    let proxy_addr = (std::net::Ipv4Addr::UNSPECIFIED, state.cfg.listen_port);
    let metrics_addr = (std::net::Ipv4Addr::UNSPECIFIED, state.cfg.metrics_port);

    tracing::info!(
        proxy_port = state.cfg.listen_port,
        metrics_port = state.cfg.metrics_port,
        "egress_proxy.starting"
    );

    let metrics_router = metrics::router(state.metrics.clone());
    let metrics_listener = TcpListener::bind(metrics_addr).await?;
    let metrics_server = axum::serve(metrics_listener, metrics_router);

    let proxy_listener = TcpListener::bind(proxy_addr).await?;
    let drain = state.cfg.shutdown_drain;
    let signal = termination_signal()?;
    let proxy_server = serve_proxy_until(
        proxy_listener,
        state,
        async move {
            let name = signal.await;
            tracing::info!(
                signal = name,
                "egress_proxy.shutdown_signal_received: no longer accepting, draining"
            );
        },
        drain,
    );

    tokio::select! {
        res = metrics_server => res.map_err(anyhow::Error::from),
        res = proxy_server => res,
    }
}

/// Resolves when the process receives SIGTERM or SIGINT, yielding the
/// signal's name. Signal handlers are registered eagerly (before the
/// returned future is first polled) so a signal arriving during startup is
/// never lost, and a registration failure is a hard error rather than a
/// silently-ignored SIGTERM.
pub fn termination_signal() -> std::io::Result<impl std::future::Future<Output = &'static str>> {
    use tokio::signal::unix::{signal, SignalKind};
    let mut term = signal(SignalKind::terminate())?;
    let mut int = signal(SignalKind::interrupt())?;
    Ok(async move {
        tokio::select! {
            _ = term.recv() => "SIGTERM",
            _ = int.recv() => "SIGINT",
        }
    })
}

/// Drives the CONNECT/forward-HTTP accept loop over an already-bound
/// listener, forever. `pub` so `tests/e2e.rs` can stand up a real server
/// against an ephemeral port without going through `run`'s env-based
/// `build_state`. Never shuts down gracefully -- production uses
/// [`serve_proxy_until`].
pub async fn serve_proxy(listener: TcpListener, state: Arc<ProxyState>) -> anyhow::Result<()> {
    serve_proxy_until(
        listener,
        state,
        std::future::pending::<()>(),
        std::time::Duration::ZERO,
    )
    .await
}

/// [`serve_proxy`] with graceful shutdown: runs the accept loop until
/// `shutdown` resolves, then (1) drops the listener so no new connection is
/// accepted, (2) asks every live HTTP connection to finish its in-flight
/// request and close (`UpgradeableConnection::graceful_shutdown`), and (3) waits for
/// CONNECT tunnels to end (they hold a `TenantLimiter` slot for their whole
/// lifetime, so `limiter.active_total() == 0` means nothing is in flight),
/// all bounded by `drain_deadline`. Returns `Ok(())` either way -- after a
/// clean drain, or after the deadline with a WARN naming how much work was
/// cut off -- so the caller can exit 0 and the pod is never SIGKILLed.
pub async fn serve_proxy_until<F>(
    listener: TcpListener,
    state: Arc<ProxyState>,
    shutdown: F,
    drain_deadline: std::time::Duration,
) -> anyhow::Result<()>
where
    F: std::future::Future<Output = ()>,
{
    // Graceful-shutdown plumbing (hyper-util's `GracefulShutdown` helper
    // does not cover http1's `UpgradeableConnection`, which CONNECT needs):
    // `drain_tx` tells every live connection task to finish its in-flight
    // request; each task holds a clone of `done_tx`, so `done_rx.recv()`
    // returning `None` means every connection task has ended.
    let (drain_tx, drain_rx) = tokio::sync::watch::channel(false);
    let (done_tx, mut done_rx) = tokio::sync::mpsc::channel::<()>(1);
    tokio::pin!(shutdown);
    loop {
        let (stream, _peer) = tokio::select! {
            biased;
            _ = &mut shutdown => break,
            accepted = listener.accept() => accepted?,
        };
        let io = TokioIo::new(stream);
        let conn_state = state.clone();
        let header_read_timeout = state.cfg.header_read_timeout;
        let service = service_fn(move |req| proxy::handle(conn_state.clone(), req));
        // `header_read_timeout` requires an explicit `Timer` to take
        // effect at all (hyper 1.x: silently does nothing without one,
        // rather than falling back to its documented 30s default) --
        // bounds a slow-loris-style caller trickling request headers in
        // indefinitely.
        let conn = hyper::server::conn::http1::Builder::new()
            .timer(hyper_util::rt::TokioTimer::new())
            .header_read_timeout(header_read_timeout)
            .serve_connection(io, service)
            .with_upgrades();
        let mut drain_rx = drain_rx.clone();
        let done_tx = done_tx.clone();
        tokio::spawn(async move {
            let _done = done_tx;
            tokio::pin!(conn);
            let mut draining = false;
            let result = loop {
                tokio::select! {
                    res = conn.as_mut() => break res,
                    changed = drain_rx.changed(), if !draining => {
                        draining = true;
                        if changed.is_ok() {
                            // Finish the in-flight request (if any), then
                            // close; an idle keep-alive connection closes
                            // immediately.
                            conn.as_mut().graceful_shutdown();
                        }
                    }
                }
            };
            if let Err(err) = result {
                tracing::debug!(error = %err, "egress_proxy.connection_closed");
            }
        });
    }

    // Stop accepting: the listener is closed before the drain begins.
    drop(listener);
    drop(done_tx);
    let _ = drain_tx.send(true);
    let in_flight_at_start = state.limiter.active_total();
    tracing::info!(
        in_flight = in_flight_at_start,
        drain_deadline_s = drain_deadline.as_secs_f64(),
        "egress_proxy.draining"
    );
    let drained = tokio::time::timeout(drain_deadline, async {
        // `None` once every connection task (each holds a `done_tx`) ended.
        let _ = done_rx.recv().await;
        while state.limiter.active_total() > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    })
    .await;
    match drained {
        Ok(()) => tracing::info!("egress_proxy.drain_complete"),
        Err(_) => tracing::warn!(
            remaining = state.limiter.active_total(),
            drain_deadline_s = drain_deadline.as_secs_f64(),
            "egress_proxy.drain_deadline_exceeded: exiting with work still in flight"
        ),
    }
    Ok(())
}

/// `--healthcheck` subcommand: a local TCP probe against the metrics
/// port's `/health`, run in-process (no `curl`, per rules/client.md).
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port: u16 = std::env::var("METRICS_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(9090);
    let url = format!("http://127.0.0.1:{port}/health");
    let stream = tokio::net::TcpStream::connect(("127.0.0.1", port)).await?;
    drop(stream);
    tracing::debug!(url, "egress_proxy.healthcheck_ok");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every env var `Config::from_env` reads -- cleared before/after each
    /// test in this module so one test's leftover value can never leak
    /// into another (this module's tests all hold `ENV_LOCK`, but the same
    /// variables are also touched by `config::tests`, which acquires the
    /// same lock).
    const CONFIG_ENV_VARS: &[&str] = &[
        "PROXY_LISTEN_PORT",
        "METRICS_PORT",
        "ALLOWED_PORTS",
        "DENY_CIDRS",
        "DENY_CLUSTER_CIDRS",
        "MACHINE_JWT_JWKS_URL",
        "MACHINE_JWT_AUDIENCE",
        "MACHINE_JWT_TRUSTED_ISSUERS",
        "MACHINE_JWT_REQUIRED_SCOPE",
        "ALLOWED_CALLER_SERVICES",
        "PER_TENANT_MAX_CONNECTIONS",
        "PER_TENANT_BANDWIDTH_BYTES_PER_SEC",
        "CONNECT_TIMEOUT_SECONDS",
        "ASSERTION_MAX_TTL_SECONDS",
        "HEADER_READ_TIMEOUT_SECONDS",
        "TUNNEL_IDLE_TIMEOUT_SECONDS",
        "TUNNEL_MAX_DURATION_SECONDS",
        "EGRESS_PROXY_ALLOW_PRIVATE_IP",
        "SHUTDOWN_DRAIN_SECONDS",
    ];

    fn clear_config_env() {
        for var in CONFIG_ENV_VARS {
            // SAFETY: serialized by ENV_LOCK, held by every caller of this
            // helper.
            unsafe { std::env::remove_var(var) };
        }
    }

    fn set_required_config_env() {
        // SAFETY: serialized by ENV_LOCK, held by every caller of this
        // helper.
        unsafe {
            std::env::set_var("MACHINE_JWT_JWKS_URL", "https://hub-api.example/jwks.json");
            std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy");
        }
    }

    #[tokio::test]
    async fn build_state_populates_config_and_metrics_from_env_defaults() {
        let _guard = ENV_LOCK.lock().await;
        clear_config_env();
        set_required_config_env();

        let registry = prometheus::Registry::new();
        let state = build_state(&registry)
            .await
            .expect("required env vars are set");

        assert_eq!(state.cfg.listen_port, 8443);
        assert_eq!(state.cfg.metrics_port, 9090);
        assert_eq!(state.cfg.allowed_ports, vec![80, 443, 6697]);
        assert_eq!(state.cfg.per_tenant_max_connections, 50);
        assert_eq!(
            state.cfg.machine_jwt_jwks_url,
            "https://hub-api.example/jwks.json"
        );
        assert!(state.cluster_cidrs.is_empty());

        clear_config_env();
    }

    #[tokio::test]
    async fn build_state_fails_closed_when_jwks_url_is_missing() {
        let _guard = ENV_LOCK.lock().await;
        clear_config_env();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("MACHINE_JWT_AUDIENCE", "egress-proxy") };

        let registry = prometheus::Registry::new();
        let err = match build_state(&registry).await {
            Ok(_) => panic!("expected MACHINE_JWT_JWKS_URL to be required"),
            Err(e) => e,
        };
        assert!(
            err.to_string().contains("MACHINE_JWT_JWKS_URL"),
            "expected a clear missing-var message, got: {err}"
        );

        clear_config_env();
    }

    #[tokio::test]
    async fn run_healthcheck_succeeds_against_a_bound_metrics_port() {
        let _guard = ENV_LOCK.lock().await;
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("bind ephemeral port");
        let port = listener.local_addr().unwrap().port();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("METRICS_PORT", port.to_string()) };

        let result = run_healthcheck().await;

        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("METRICS_PORT") };
        drop(listener);
        assert!(result.is_ok(), "expected Ok, got {result:?}");
    }
}
