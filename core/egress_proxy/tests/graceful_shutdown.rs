//! Graceful-shutdown tests (release-audit ops HIGH: egress_proxy ignored
//! SIGTERM, so every rollout waited out the grace period and ended in a
//! SIGKILL).
//!
//! Two layers:
//! - `serve_proxy_until` (in-process): stop-accepting, drain-until-idle and
//!   drain-deadline behaviour, driven by an in-flight `TenantLimiter` slot
//!   standing in for a live CONNECT tunnel (the tunnel holds its slot for
//!   its whole lifetime).
//! - The real `egress-proxy` binary: a literal SIGTERM must produce exit
//!   code 0 well inside the drain window -- not a signal death.

use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use egress_proxy::assertion::InMemoryReplayCache;
use egress_proxy::config::Config;
use egress_proxy::limits::TenantLimiter;
use egress_proxy::metrics::Metrics;
use egress_proxy::proxy::ProxyState;
use jsonwebtoken::DecodingKey;
use service_auth::TrustBundle;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;

struct EmptyBundle;

#[async_trait]
impl TrustBundle for EmptyBundle {
    async fn public_key(&self, _kid: &str) -> Option<DecodingKey> {
        None
    }
}

fn state() -> Arc<ProxyState> {
    let cfg = Config {
        listen_port: 0,
        metrics_port: 0,
        allowed_ports: vec![443],
        deny_cidrs: vec![],
        deny_cluster_cidrs: vec![],
        machine_jwt_jwks_url: String::new(),
        machine_jwt_audience: "egress-proxy".into(),
        machine_jwt_trusted_issuers: vec!["hub-api".into()],
        machine_jwt_required_scope: "egress:connect".into(),
        allowed_caller_services: vec!["svc-process".into()],
        per_tenant_max_connections: 50,
        per_tenant_bandwidth_bytes_per_sec: 100_000_000,
        connect_timeout: Duration::from_secs(5),
        assertion_max_ttl: Duration::from_secs(60),
        header_read_timeout: Duration::from_secs(10),
        tunnel_idle_timeout: Duration::from_secs(300),
        shutdown_drain: Duration::from_secs(2),
        tunnel_max_duration: Duration::from_secs(3600),
        allow_private_ip: false,
    };
    Arc::new(ProxyState {
        cfg: Arc::new(cfg),
        trust_bundle: Arc::new(EmptyBundle),
        replay_cache: Arc::new(InMemoryReplayCache::new()),
        cluster_cidrs: vec![],
        resolver: Arc::new(egress_proxy::dns::TokioResolver),
        limiter: TenantLimiter::new(50, 100_000_000),
        metrics: Metrics::new(&prometheus::Registry::new()),
    })
}

/// Starts `serve_proxy_until` on an ephemeral port; returns its address, a
/// trigger that fires the shutdown, and the server's join handle.
async fn start(
    state: Arc<ProxyState>,
    drain: Duration,
) -> (
    std::net::SocketAddr,
    oneshot::Sender<()>,
    tokio::task::JoinHandle<anyhow::Result<()>>,
) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).await.expect("bind");
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = oneshot::channel::<()>();
    let handle = tokio::spawn(egress_proxy::serve_proxy_until(
        listener,
        state,
        async move {
            let _ = rx.await;
        },
        drain,
    ));
    (addr, tx, handle)
}

#[tokio::test]
async fn shutdown_stops_accepting_and_exits_ok_promptly_when_idle() {
    let (addr, trigger, handle) = start(state(), Duration::from_secs(5)).await;

    // Serving before the signal.
    TcpStream::connect(addr)
        .await
        .expect("accepts pre-shutdown");

    let started = Instant::now();
    trigger.send(()).unwrap();
    let result = tokio::time::timeout(Duration::from_secs(6), handle)
        .await
        .expect("idle proxy must exit well before the drain deadline")
        .expect("join");
    result.expect("graceful shutdown returns Ok (exit 0)");
    assert!(started.elapsed() < Duration::from_secs(6));

    // Listener closed: no new work is accepted. Retried briefly so a
    // loaded CI box can't turn socket-teardown latency into a flake.
    let mut refused = false;
    for _ in 0..40 {
        if TcpStream::connect(addr).await.is_err() {
            refused = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(refused, "new connections must be refused after shutdown");
}

#[tokio::test]
async fn idle_keepalive_connection_does_not_hold_up_the_drain() {
    let (addr, trigger, handle) = start(state(), Duration::from_secs(5)).await;
    // An open connection that never sent a request (header_read_timeout is
    // 10s, longer than this whole test) must be closed by the drain, not
    // waited out.
    let _idle = TcpStream::connect(addr).await.expect("connect");
    tokio::time::sleep(Duration::from_millis(100)).await;

    trigger.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(6), handle)
        .await
        .expect("idle connection must not block the drain")
        .expect("join")
        .expect("Ok");
}

#[tokio::test]
async fn shutdown_waits_for_in_flight_work_then_exits() {
    let state = state();
    // Stand-in for a live CONNECT tunnel: it holds a limiter slot for its
    // whole lifetime.
    let in_flight = state.limiter.acquire("tenant-a").expect("slot");
    let (_addr, trigger, handle) = start(Arc::clone(&state), Duration::from_secs(10)).await;

    trigger.send(()).unwrap();
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert!(
        !handle.is_finished(),
        "must keep draining while work is in flight"
    );

    drop(in_flight); // the tunnel ends
    tokio::time::timeout(Duration::from_secs(6), handle)
        .await
        .expect("must exit promptly once in-flight work completes")
        .expect("join")
        .expect("Ok");
}

#[tokio::test]
async fn drain_deadline_bounds_shutdown_and_still_exits_ok() {
    let state = state();
    let _stuck = state.limiter.acquire("tenant-a").expect("slot");
    let (_addr, trigger, handle) = start(Arc::clone(&state), Duration::from_millis(300)).await;

    let started = Instant::now();
    trigger.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(6), handle)
        .await
        .expect("deadline must cut the drain off")
        .expect("join")
        .expect("deadline exceeded is still a clean Ok (exit 0), never an error");
    assert!(
        started.elapsed() >= Duration::from_millis(250),
        "must have actually waited for the deadline"
    );
    assert_eq!(state.limiter.active_total(), 1, "stuck work was cut off");
}

/// Picks a currently-free local TCP port (bind to 0, read, release).
fn free_port() -> u16 {
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .expect("bind")
        .local_addr()
        .unwrap()
        .port()
}

/// The real binary, a real SIGTERM: the process must exit with status 0
/// (not die by signal, not hang until SIGKILL) well inside the drain
/// window.
#[test]
fn real_binary_exits_zero_on_sigterm() {
    let proxy_port = free_port();
    let metrics_port = free_port();
    let mut child = Command::new(env!("CARGO_BIN_EXE_egress-proxy"))
        .env("PROXY_LISTEN_PORT", proxy_port.to_string())
        .env("METRICS_PORT", metrics_port.to_string())
        .env("MACHINE_JWT_JWKS_URL", "http://127.0.0.1:9/jwks.json")
        .env("MACHINE_JWT_AUDIENCE", "egress-proxy")
        .env("SHUTDOWN_DRAIN_SECONDS", "5")
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn egress-proxy");

    // Wait until the proxy listener is up (signal handlers are registered
    // before the listener serves, so ready == handlers installed).
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if std::net::TcpStream::connect(("127.0.0.1", proxy_port)).is_ok() {
            break;
        }
        if let Some(status) = child.try_wait().expect("try_wait") {
            panic!("egress-proxy exited before becoming ready: {status}");
        }
        assert!(
            Instant::now() < deadline,
            "egress-proxy never started listening"
        );
        std::thread::sleep(Duration::from_millis(50));
    }

    let started = Instant::now();
    let status = Command::new("kill")
        .arg("-TERM")
        .arg(child.id().to_string())
        .status()
        .expect("run kill");
    assert!(status.success(), "kill -TERM failed: {status}");

    let exit = loop {
        if let Some(status) = child.try_wait().expect("try_wait") {
            break status;
        }
        if started.elapsed() > Duration::from_secs(10) {
            let _ = child.kill();
            panic!("egress-proxy ignored SIGTERM (still running after 10s)");
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    assert!(
        exit.success(),
        "SIGTERM must produce a clean exit 0, got {exit}"
    );
    assert!(
        started.elapsed() < Duration::from_secs(6),
        "idle drain must finish quickly, took {:?}",
        started.elapsed()
    );
}
