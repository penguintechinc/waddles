//! HTTP layer: axum router wiring for the control-plane surface (health,
//! readiness, the JWT-gated `/api/v1/*` community surface, the
//! `ServiceKey`-gated `/api/v1/internal/*` surface, the two-document OpenAPI
//! split, and -- since the S12 integration chunk -- the public HLS serving
//! surface and the WHIP/WHEP WebRTC signaling routers. RTMP/SRT ingest
//! listeners bind their own dedicated ports and are wired in
//! [`crate::run_with_shutdown`], not here; WHIP/WHEP ride this shared HTTP
//! port (no dedicated port for WebRTC signaling, only the ICE/RTP media
//! itself is on `WEBRTC_UDP_RANGE`).

pub mod auth;
pub mod health;
pub mod openapi;

use std::net::IpAddr;
use std::sync::Arc;
use std::time::Instant;

use axum::extract::{MatchedPath, Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use tokio::sync::mpsc;
use tower_http::trace::TraceLayer;

use crate::api::SharedEngine;
use crate::billing::token_ledger::TokenLedgerClient;
use crate::config::Config;
use crate::egress::hls::HlsRouterState;
use crate::egress::whep::{WhepState, DEFAULT_MAX_VIEWERS};
use crate::ingest::whip::WhipState;
use crate::ingest::IngestSession;
use crate::rtc::ingest_auth::{InternalIngestAuthClient, WhipTokenAuthorizer};
use crate::rtc::{PeerConnectionFactory, RtcConfig, RtcMetrics};
use crate::telemetry::RequestMetrics;

/// Shared state handed to every axum handler via `Router::with_state`.
/// Cheap to clone: everything behind an `Arc`.
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub metrics: Arc<prometheus::Registry>,
    pub request_metrics: RequestMetrics,
    pub started_at: Instant,
    /// The pipeline lifecycle engine backing `/api/v1/.../start|stop|status`
    /// and `/api/v1/internal/streaming/pipelines`. Defaults to
    /// [`crate::pipeline::StubSupervisor`] (every call `Unimplemented`) --
    /// [`crate::run_with_shutdown`] overrides this field with a real
    /// [`crate::pipeline::FfmpegSupervisor`] before serving.
    pub engine: SharedEngine,
    /// State for the public (unauthenticated) HLS serving router
    /// (`/live/...`) -- defaults to an empty [`crate::egress::hls::RunningPipelines`]
    /// registry (file serving still works from disk; only the
    /// `/live/{community_id}` listing endpoint reports nothing).
    /// [`crate::run_with_shutdown`] overrides this with the orchestrator's
    /// live registry.
    pub hls_router_state: HlsRouterState,
    /// Shared WHIP (WebRTC ingest) signaling state -- always present so
    /// `/whip/*` is always mounted; its ingest hand-off channel is a
    /// throwaway (dropped receiver) until [`crate::run_with_shutdown`]
    /// rebuilds it with the orchestrator's real channel.
    pub whip_state: Arc<WhipState>,
    /// Shared WHEP (WebRTC egress) signaling state -- always present so
    /// `/whep/*` is always mounted.
    pub whep_state: Arc<WhepState>,
    /// HTTP client for hub-api's transcode-token ledger
    /// (`crate::billing::token_ledger`) -- used by
    /// `crate::api::lifecycle::start`'s TRANSCODE admission check. Always
    /// present (cheap to construct, holds no connection state).
    pub token_ledger: Arc<TokenLedgerClient>,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`] and the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// with a throwaway WHIP ingest hand-off channel (nothing reads from
    /// it) -- suitable for tests and any caller that doesn't need real
    /// ingest wiring. See [`Self::new_with_ingest_tx`] for the production
    /// constructor [`crate::run_with_shutdown`] uses.
    pub fn new(config: Config, metrics: prometheus::Registry) -> Self {
        let (ingest_tx, _ingest_rx) = mpsc::channel(1);
        Self::new_with_ingest_tx(config, metrics, ingest_tx)
    }

    /// Like [`Self::new`], but takes the real ingest hand-off channel a
    /// WHIP publish should be routed through -- used by
    /// [`crate::run_with_shutdown`] so `/whip/{token}` sessions reach the
    /// orchestrator's [`crate::ingest::IngestSession`] receiver from the
    /// moment the service starts serving, without a second
    /// state-construction pass. Registers this service's base request
    /// metrics plus [`RtcMetrics`] against `metrics` -- see
    /// [`crate::telemetry::register_request_metrics`].
    pub fn new_with_ingest_tx(
        config: Config,
        metrics: prometheus::Registry,
        ingest_tx: mpsc::Sender<IngestSession>,
    ) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        let rtc_metrics = RtcMetrics::register(&metrics)
            .expect("RtcMetrics registered exactly once per AppState construction");
        let rtc_config = RtcConfig::from_config(&config).expect(
            "WEBRTC_UDP_RANGE is already validated by CliConfig::validate at Config::load time",
        );
        let pc_factory = Arc::new(
            PeerConnectionFactory::new(rtc_config)
                .expect("the `webrtc` crate is compiled with its default runtime-tokio feature"),
        );
        let whip_authorizer: Arc<dyn WhipTokenAuthorizer> = Arc::new(
            InternalIngestAuthClient::new(&config)
                .expect("building the loopback reqwest client cannot fail"),
        );
        let bind_addr: IpAddr = config.cli.bind_addr;
        let stream_data_dir = config.cli.stream_data_dir.clone();
        let whip_state = Arc::new(WhipState::new(
            pc_factory.clone(),
            whip_authorizer,
            ingest_tx,
            stream_data_dir.clone(),
            bind_addr,
            rtc_metrics.clone(),
        ));
        let whep_state = Arc::new(WhepState::new(pc_factory, rtc_metrics, DEFAULT_MAX_VIEWERS));
        let engine: SharedEngine = Arc::new(crate::pipeline::StubSupervisor);
        let hls_router_state = HlsRouterState::new(
            stream_data_dir,
            Arc::new(crate::egress::hls::EmptyRunningPipelines)
                as Arc<dyn crate::egress::hls::RunningPipelines>,
        );
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            engine,
            hls_router_state,
            whip_state,
            whep_state,
            token_ledger: Arc::new(TokenLedgerClient::new()),
        }
    }
}

/// Records every request into [`AppState::request_metrics`]: a labeled
/// counter and a latency histogram. Applied to the whole control-plane
/// router so `/metrics` always has data once the service has served at
/// least one request (the `up` gauge covers the window before that).
async fn record_http_metrics(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let method = req.method().to_string();
    // The matched route template (`/whip/{token}`), never the raw path:
    // WHIP/WHEP tokens ride in the path, so a raw-path label would publish
    // credentials on the `/metrics` scrape surface *and* mint one series per
    // token/scanner probe. Unrouted requests collapse into `unmatched`.
    let path = req
        .extensions()
        .get::<MatchedPath>()
        .map_or("unmatched", MatchedPath::as_str)
        .to_string();
    let start = Instant::now();
    let response = next.run(req).await;
    let status = response.status().as_u16().to_string();
    state
        .request_metrics
        .http_requests_total
        .with_label_values(&[&method, &path, &status])
        .inc();
    state
        .request_metrics
        .http_request_duration_seconds
        .with_label_values(&[&method, &path])
        .observe(start.elapsed().as_secs_f64());
    response
}

/// Builds the full control-plane + data-plane-signaling router:
/// - public health/readiness + the public minimal OpenAPI doc (no auth)
/// - the JWT-gated surface: community/tenant `/api/v1/*` routes, full
///   OpenAPI doc, Swagger UI
/// - the `ServiceKey`-gated `/api/v1/internal/*` surface, mounted as its
///   **own** nest so it never rides the JWT `route_layer` above -- fixes
///   the gap `crate::api::internal`'s module doc used to flag (a single
///   combined nest meant `/internal/*` needed a valid user JWT *and* the
///   service key, defeating the point of a pure service-to-service
///   credential)
/// - the public HLS serving surface (`/live/...`, `crate::egress::hls`)
/// - the WHIP (`/whip/...`) and WHEP (`/whep/...`) WebRTC signaling routers
pub fn router(state: AppState) -> Router {
    let public = Router::new()
        .route("/health", get(health::liveness))
        .route("/readyz", get(health::readiness))
        .route("/api/v1/openapi/public.json", get(openapi::public_spec));

    let authenticated = Router::new()
        // `openapi::swagger_ui()` registers both `/api/v1/docs` (UI) and
        // `/api/v1/openapi.json` (the JSON it points at) -- see that
        // function's doc comment for why there is no separate handwritten
        // route for the JSON path.
        .merge(openapi::swagger_ui())
        .nest("/api/v1", crate::api::router(state.engine.clone()))
        .route_layer(axum::middleware::from_fn_with_state(
            state.clone(),
            auth::require_auth,
        ));

    let internal = Router::new().nest("/api/v1", crate::api::internal_router(state.engine.clone()));

    public
        .merge(authenticated)
        .merge(internal)
        .with_state(state.clone())
        .merge(crate::egress::hls::hls_router(
            state.hls_router_state.clone(),
        ))
        .merge(crate::ingest::whip::router(state.whip_state.clone()))
        .merge(crate::egress::whep::router(state.whep_state.clone()))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            record_http_metrics,
        ))
        // `request_span` parents each request span to an incoming W3C
        // `traceparent` and records the route template, not the raw URI
        // (which can carry WHIP/WHEP tokens) -- see `telemetry::trace_context`.
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(crate::telemetry::trace_context::request_span::<axum::body::Body>),
        )
}

/// Builds the secondary Prometheus metrics router, bound to its own port
/// (`METRICS_PORT`, default 9090) per `rules/critical-rules.md`
/// Observability -- kept separate from the control-plane router so a
/// scraper never needs auth and never shares a listener with user traffic.
pub fn metrics_router(state: AppState) -> Router {
    Router::new()
        .route("/metrics", get(health::metrics))
        .with_state(state)
}
