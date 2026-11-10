//! `svc-streaming`: the Waddles A/V data-plane service.
//!
//! This crate is split into a library (this file) and a thin binary
//! (`src/main.rs`) so integration tests under `tests/` can exercise the
//! router, config loader, and auth extractors directly instead of spawning
//! a subprocess.

pub mod api;
pub mod billing;
pub mod config;
pub mod db;
pub mod egress;
pub mod error;
pub mod http;
pub mod ingest;
pub mod orchestrator;
pub mod pipeline;
pub mod redact;
pub mod rooms;
pub mod rtc;
pub mod spec_builder;
pub mod store;
pub mod stream_key;
pub mod telemetry;
pub mod tls;

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context as _;
use tokio::signal;

use crate::ingest::IngestListener as _;

/// Default `tracing`/OTel service name, also the fallback `--healthcheck`
/// target and the resource `service.name` when `OTEL_SERVICE_NAME` is
/// unset.
pub const SERVICE_NAME: &str = "svc-streaming";

/// Runs the service: loads config, bootstraps telemetry, wires the ingest
/// (RTMP/SRT/WHIP) -> pipeline -> egress (HLS/relay/record) orchestrator
/// (`orchestrator::Orchestrator`), builds the control-plane + metrics
/// routers, and serves everything until SIGINT/SIGTERM is received.
pub async fn run() -> anyhow::Result<()> {
    let config = config::Config::load()?;
    run_with_shutdown(config, shutdown_signal(), shutdown_signal()).await
}

/// Default poll interval for [`orchestrator::RefreshingSrtAuth`]'s
/// background allowlist refresh -- short enough that a newly-enabled
/// `streaming_configs` row is publishable over SRT within a few seconds of
/// being created.
const SRT_AUTH_REFRESH_INTERVAL: Duration = Duration::from_secs(5);

/// Same as [`run`], but takes an already-loaded [`config::Config`] and
/// caller-supplied shutdown futures for each listener instead of installing
/// OS signal handlers. This is what makes the bind/serve/telemetry wiring
/// testable: a test can pass `config` built via
/// [`config::CliConfig::parse_from`] (port `0` for an OS-assigned ephemeral
/// port) and an already-resolved future so the server binds, logs, and
/// shuts down immediately instead of blocking forever on a real signal.
/// **No graceful shutdown for the RTMP/SRT ingest listeners or the
/// orchestrator's dispatch loop** -- only the HTTP/metrics servers drain via
/// `http_shutdown`/`metrics_shutdown`; the ingest side is dropped when the
/// process/runtime exits (see `README.md` Runtime Wiring, a documented
/// simplification, not a silent gap).
pub async fn run_with_shutdown<F1, F2>(
    config: config::Config,
    http_shutdown: F1,
    metrics_shutdown: F2,
) -> anyhow::Result<()>
where
    F1: std::future::Future<Output = ()> + Send + 'static,
    F2: std::future::Future<Output = ()> + Send + 'static,
{
    let (_telemetry_guard, prom_registry) = telemetry::init(SERVICE_NAME);

    tracing::info!(
        http_port = config.cli.http_port,
        metrics_port = config.cli.metrics_port,
        rtmp_port = config.cli.rtmp_port,
        srt_port = config.cli.srt_port,
        "starting {SERVICE_NAME}"
    );

    // One shared channel: every ingest source (RTMP/SRT listeners, the WHIP
    // HTTP router) pushes accepted sessions here; the orchestrator is the
    // sole reader. `AppState::new_with_ingest_tx` hands a clone of `tx` to
    // `WhipState` so a `POST /whip/{token}` hand-off reaches the same
    // dispatch loop as RTMP/SRT.
    let (ingest_tx, ingest_rx) = tokio::sync::mpsc::channel::<ingest::IngestSession>(256);

    let state =
        http::AppState::new_with_ingest_tx(config.clone(), prom_registry, ingest_tx.clone());

    // --- Real pipeline engine + egress sinks, overriding AppState's stub
    // defaults so `/api/v1/.../start|stop|status` drive the actual ffmpeg
    // supervisor instead of `StubSupervisor`.
    let rtp_base_port = config::parse_udp_range(&config.cli.webrtc_udp_range)
        .map(|(start, _end)| start)
        .unwrap_or(40000); // unreachable in practice: already validated at Config::load time

    // GPU-preferred, CPU-fallback: probe once at startup which encoders this
    // host + ffmpeg build can actually use (a device node alone proves
    // nothing -- see `pipeline::encoder`). Never fails: any problem degrades
    // to the CPU encoders with a log line saying why.
    let device_hints = pipeline::DeviceHints::detect(config.cli.stream_vaapi_device.as_deref());
    let encoders = pipeline::detect_encoders(
        &config.cli.ffmpeg_path,
        config.cli.stream_encoder,
        &device_hints,
    )
    .await;

    // One policy for both layers: the sink tracks relay targets, the
    // supervisor enforces the same cap in the ffmpeg argv itself.
    let relay_policy = egress::relay::RelayPolicy::default();

    let supervisor = Arc::new(
        pipeline::FfmpegSupervisor::new(
            config.cli.ffmpeg_path.clone(),
            config.cli.stream_data_dir.clone(),
            rtp_base_port,
            Arc::new(store::DefaultSecretResolver),
            pipeline::SupervisorConfig::default(),
        )
        .with_encoders(encoders)
        .with_relay_policy(relay_policy),
    );
    let engine: api::SharedEngine = supervisor.clone();

    let hls = Arc::new(egress::hls::HlsSink::new(
        config.cli.stream_data_dir.clone(),
        &state.metrics,
    ));
    let registry = Arc::new(orchestrator::PipelineRegistry::new());
    let hls_router_state = egress::hls::HlsRouterState::new(
        config.cli.stream_data_dir.clone(),
        registry.clone() as Arc<dyn egress::hls::RunningPipelines>,
    );

    let relay_metrics = egress::relay::register_relay_metrics(&state.metrics)
        .expect("relay metrics registered exactly once per process");
    let relay =
        Arc::new(egress::relay::RelaySink::with_policy(relay_policy).with_metrics(relay_metrics));

    let record_metrics = egress::record::register_metrics(&state.metrics);
    let record_result = egress::record::RecordSink::from_env(
        &store::DefaultSecretResolver,
        &config.cli.stream_data_dir,
        record_metrics,
    );
    let (record, record_readiness) = record_status(record_result);

    let mut state = state;
    state.record = record_readiness;
    state.engine = engine;
    state.hls_router_state = hls_router_state;

    let orchestrator = Arc::new(
        orchestrator::Orchestrator::new(
            config.clone(),
            supervisor,
            hls,
            relay,
            record,
            registry,
            state.whip_state.clone(),
        )
        .with_whep_state(state.whep_state.clone()),
    );
    tokio::spawn(Arc::clone(&orchestrator).run(ingest_rx));

    // --- Ingest listeners. `DbIngestAuth` resolves its DB connection
    // lazily per-attempt (see its own doc comment) -- binding these
    // listeners never itself requires the DB to be reachable yet.
    let db_auth = Arc::new(orchestrator::DbIngestAuth::new(config.clone()));
    let rtmp_listener = ingest::rtmp::RtmpListener::bind(
        config.cli.bind_addr,
        config.cli.rtmp_port,
        db_auth,
        &state.metrics,
    )
    .await
    .context("binding the RTMP ingest listener")?;
    tracing::info!(rtmp_addr = ?rtmp_listener.local_addr().ok(), "rtmp ingest listening");
    tokio::spawn(rtmp_listener.run(ingest_tx.clone()));

    let srt_auth = Arc::new(orchestrator::RefreshingSrtAuth::new());
    srt_auth.spawn_refresh_loop(config.clone(), SRT_AUTH_REFRESH_INTERVAL);
    let srt_listener = ingest::srt::SrtListener::from_config(&config).with_auth(srt_auth);
    tracing::info!(srt_port = config.cli.srt_port, "srt ingest listening");
    tokio::spawn(srt_listener.run(ingest_tx));

    // --- WebRTC rooms host-API (mTLS; off + logged when HOST_API_* unset).
    state.rooms.spawn_sweeper(Duration::from_secs(5));
    state.whep_state.spawn_sweeper(Duration::from_secs(5));
    rooms::host_api::spawn_from_env(config.cli.bind_addr, state.rooms.clone())
        .await
        .context("starting the webrtc rooms host-API listener")?;

    // --- HTTP + metrics.
    let http_addr = SocketAddr::new(config.cli.bind_addr, config.cli.http_port);
    let metrics_addr = SocketAddr::new(config.cli.bind_addr, config.cli.metrics_port);

    let http_listener = tokio::net::TcpListener::bind(http_addr).await?;
    let metrics_listener = tokio::net::TcpListener::bind(metrics_addr).await?;

    tracing::info!(%http_addr, %metrics_addr, "listening");

    // One stop signal fans out to the plaintext listener and the TLS listener.
    let (stop_tx, stop_rx) = tokio::sync::watch::channel(false);
    tokio::spawn(async move {
        http_shutdown.await;
        let _ = stop_tx.send(true);
    });
    let mut plain_stop = stop_rx.clone();
    let http_shutdown = async move {
        let _ = plain_stop.wait_for(|stopped| *stopped).await;
    };

    // Optional in-cluster TLS control-plane listener (hub-api -> svc-streaming).
    // Misconfigured material is a startup error, never a silent plaintext-only boot.
    let tls_server = match (
        config.cli.tls_port,
        &config.cli.tls_cert_file,
        &config.cli.tls_key_file,
    ) {
        (Some(port), Some(cert), Some(key)) => {
            let server_config = tls::build_server_config(cert, key)
                .context("building the TLS control-plane server config")?;
            let tls_addr = SocketAddr::new(config.cli.bind_addr, port);
            let tls_listener = tokio::net::TcpListener::bind(tls_addr).await?;
            tracing::info!(%tls_addr, "tls control plane listening");
            let mut tls_stop = stop_rx.clone();
            let router = http::router(state.clone());
            Some(tls::serve(
                tls_listener,
                server_config,
                router,
                async move {
                    let _ = tls_stop.wait_for(|stopped| *stopped).await;
                },
            ))
        }
        _ => None,
    };

    let http_server = axum::serve(http_listener, http::router(state.clone()))
        .with_graceful_shutdown(http_shutdown);
    let metrics_server = axum::serve(metrics_listener, http::metrics_router(state))
        .with_graceful_shutdown(metrics_shutdown);

    tokio::try_join!(
        async { http_server.await.map_err(anyhow::Error::from) },
        async { metrics_server.await.map_err(anyhow::Error::from) },
        async {
            match tls_server {
                Some(server) => server.await,
                None => Ok(()),
            }
        },
    )?;

    Ok(())
}

/// Waits for SIGINT (Ctrl-C) or SIGTERM (Kubernetes pod termination) and
/// returns, letting `axum::serve`'s graceful shutdown drain in-flight
/// requests rather than dropping connections mid-response.
async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install SIGINT handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

/// `svc-streaming --healthcheck`: GETs `/health` on the locally-bound HTTP
/// port and exits 0/1 accordingly. Reads `MODULE_PORT` the same way
/// [`run`] does, without needing the full secret-bearing [`config::Config`]
/// -- the container `HEALTHCHECK` invokes this directly instead of relying
/// on `curl` being present in the runtime image.
pub async fn run_healthcheck() -> anyhow::Result<()> {
    let port: u16 = std::env::var("MODULE_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8208);
    let url = format!("http://127.0.0.1:{port}/health");

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()?;

    match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => Ok(()),
        Ok(resp) => {
            eprintln!("healthcheck failed: {url} returned {}", resp.status());
            std::process::exit(1);
        }
        Err(err) => {
            eprintln!("healthcheck failed: {err}");
            std::process::exit(1);
        }
    }
}

/// Splits the `RecordSink::from_env` outcome into the sink the orchestrator
/// uses and the readiness `/readyz` reports. A failure is logged and kept as
/// the `Disabled` reason (it names the missing variable, never a value).
fn record_status(
    result: anyhow::Result<egress::record::RecordSink>,
) -> (
    Option<Arc<egress::record::RecordSink>>,
    http::RecordReadiness,
) {
    match result {
        Ok(sink) => (Some(Arc::new(sink)), http::RecordReadiness::Enabled),
        Err(err) => {
            tracing::warn!(error = %err, "recording disabled: S3_ENDPOINT/RECORDINGS_BUCKET/S3_ACCESS_KEY/S3_SECRET_KEY not fully configured");
            (None, http::RecordReadiness::Disabled(format!("{err:#}")))
        }
    }
}

#[cfg(test)]
mod record_status_tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use clap::Parser as _;
    use http_body_util::BodyExt;
    use tower::ServiceExt as _;

    async fn readyz(readiness: http::RecordReadiness) -> serde_json::Value {
        let cli = config::CliConfig::parse_from(["svc-streaming"]);
        let cfg = config::Config {
            cli,
            db_password: config::Secret::new("x"),
            cache_password: None,
            service_api_key: config::Secret::new("x"),
            jwt_hmac_secret: None,
        };
        let mut state = http::AppState::new(cfg, prometheus::Registry::new());
        state.record = readiness;
        let res = http::router(state)
            .oneshot(
                Request::builder()
                    .uri("/readyz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), 200);
        serde_json::from_slice(&res.into_body().collect().await.unwrap().to_bytes()).unwrap()
    }

    #[tokio::test]
    async fn disabled_sink_reports_degraded_with_the_reason() {
        let (sink, readiness) = record_status(Err(anyhow::anyhow!("S3_ENDPOINT is not set")));
        assert!(sink.is_none());
        let body = readyz(readiness).await;
        assert_eq!(body["status"], "degraded");
        let record = &body["dependencies"][3];
        assert_eq!(record["name"], "record");
        assert_eq!(record["configured"], false);
        assert!(record["detail"]
            .as_str()
            .unwrap()
            .contains("S3_ENDPOINT is not set"));
    }

    #[tokio::test]
    async fn enabled_sink_reports_ok() {
        let dir = std::env::temp_dir();
        let metrics = egress::record::register_metrics(&prometheus::Registry::new());
        let sink = egress::record::RecordSink::new(
            dir,
            Arc::new(object_store::memory::InMemory::new()),
            metrics,
        );
        let (got, readiness) = record_status(Ok(sink));
        assert!(got.is_some());
        let body = readyz(readiness).await;
        assert_eq!(body["status"], "ok");
        assert_eq!(body["dependencies"][3]["configured"], true);
    }
}
