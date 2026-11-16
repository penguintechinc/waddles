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
use axum::http::{header, HeaderValue, Method};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::get;
use axum::Router;
use tokio::sync::mpsc;
use tower_http::cors::{AllowOrigin, CorsLayer};
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
    /// WebRTC rooms (local SFU) -- `/rtc/rooms/*` data plane + host-API control.
    pub rooms: Arc<crate::rooms::RoomService>,
    /// HTTP client for hub-api's transcode-token ledger
    /// (`crate::billing::token_ledger`) -- used by
    /// `crate::api::lifecycle::start`'s TRANSCODE admission check. Always
    /// present (cheap to construct, holds no connection state).
    pub token_ledger: Arc<TokenLedgerClient>,
    /// Whether the S3 recording sink came up. Surfaced by `/readyz` so a pod
    /// that silently cannot record is visible to operators and probes, not
    /// only to whoever greps the startup log.
    /// [`crate::run_with_shutdown`] sets this from `RecordSink::from_env`.
    pub record: RecordReadiness,
}

/// Recording-sink readiness, reported by `/readyz`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordReadiness {
    /// The S3 sink was constructed; recordings are uploaded.
    Enabled,
    /// Recording is off; the string says why (names the missing env var --
    /// never a credential value).
    Disabled(String),
}

impl Default for RecordReadiness {
    /// Disabled until the real sink is built -- a state that was never
    /// initialised must not claim recording works.
    fn default() -> Self {
        Self::Disabled("record sink not initialised".to_string())
    }
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
        let room_keys = crate::rooms::token::TokenKeys::from_env()
            .expect("the room token signing key (WEBRTC_ROOM_TOKEN_SIGNING_KEY[_FILE]), when configured, must be a valid ES256 P-256 PKCS#8 PEM key; the retired WEBRTC_ROOM_TOKEN_SECRET is refused");
        let mut whep = WhepState::new(pc_factory.clone(), rtc_metrics.clone(), DEFAULT_MAX_VIEWERS);
        if let Some(k) = room_keys.clone() {
            whep = whep.with_viewer_auth(k);
        }
        let whep_state = Arc::new(whep);
        let rooms = Arc::new(
            crate::rooms::RoomService::new(
                pc_factory,
                rtc_metrics,
                room_keys,
                config.cli.public_base_url.clone(),
            )
            .with_whep(whep_state.clone())
            .with_moderation(moderation_flag_from_env())
            .with_idle_policy(
                crate::rooms::IdlePolicy::from_env()
                    .expect("ROOMS_EMPTY_TIMEOUT_DEFAULT_SECS / ROOMS_EMPTY_TIMEOUT_MAX_SECS, when set, must be valid"),
            )
            .with_session_idle_secs(
                crate::rooms::session_idle_secs_from_lookup(|k| std::env::var(k).ok())
                    .expect("ROOMS_SESSION_IDLE_SECS, when set, must be a non-negative integer"),
            ),
        );
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
            rooms,
            token_ledger: Arc::new(TokenLedgerClient::new()),
            record: RecordReadiness::default(),
        }
    }
}

/// Route label used when a request matched no route (404s, scanners). A
/// single fixed value, so unmatched traffic can never mint new series.
pub const UNMATCHED_ROUTE: &str = "unmatched";

/// Returns the low-cardinality route *template* a request matched (for
/// example `/whip/{token}`), never the live URI path.
///
/// WHIP puts its bearer token in the path (`/whip/<token>`, and
/// `/whip/<token>/<session>` for the session resource), so the raw path is a
/// credential and an unbounded label domain. Everything that needs to name
/// "which endpoint" in a metric label, span, or log line goes through here
/// (`rules/critical-rules.md` Token & Secret Hygiene; Observability).
/// Requests that matched no route collapse to [`UNMATCHED_ROUTE`].
pub fn route_template<B>(req: &axum::http::Request<B>) -> String {
    req.extensions()
        .get::<MatchedPath>()
        .map_or_else(|| UNMATCHED_ROUTE.to_string(), |m| m.as_str().to_string())
}

/// Records every request into [`AppState::request_metrics`]: a labeled
/// counter and a latency histogram, keyed by method and matched route
/// *template* (see [`route_template`]) -- never the raw path, which carries
/// the WHIP bearer token. Applied to the whole control-plane router so
/// `/metrics` always has data once the service has served at least one
/// request (the `up` gauge covers the window before that).
async fn record_http_metrics(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let method = req.method().to_string();
    let route = route_template(&req);
    let start = Instant::now();
    let response = next.run(req).await;
    let status = response.status().as_u16().to_string();
    state
        .request_metrics
        .http_requests_total
        .with_label_values(&[&method, &route, &status])
        .inc();
    state
        .request_metrics
        .http_request_duration_seconds
        .with_label_values(&[&method, &route])
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

    let media_cors = media_cors_layer(&state.config.cli.cors_allowed_origins);
    public
        .merge(authenticated)
        .merge(internal)
        .with_state(state.clone())
        .merge(crate::egress::hls::hls_router(
            state.hls_router_state.clone(),
        ))
        .merge(crate::ingest::whip::router(state.whip_state.clone()).layer(media_cors.clone()))
        .merge(crate::egress::whep::router(state.whep_state.clone()).layer(media_cors.clone()))
        .merge(crate::rooms::media::router(state.rooms.clone()).layer(media_cors))
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

/// Resolves the `rooms.moderation` feature flag from its env baseline
/// (`FEATURE_ROOMS_MODERATION`); unset or anything but true/1 means OFF.
pub fn moderation_flag_from_env() -> bool {
    std::env::var("FEATURE_ROOMS_MODERATION")
        .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "on"))
        .unwrap_or(false)
}

/// CORS for the media signaling routes (WHIP/WHEP/session DELETE): an exact-origin allowlist
/// from config. No wildcard ever (a `*` entry is dropped here and refused at config
/// validation); an empty list emits no CORS headers at all. `Content-Type` is allowed so the
/// `application/sdp` offer passes preflight, `Authorization` for the bearer token, and
/// `Location` is exposed so a browser can read the session URL.
pub fn media_cors_layer(origins: &[String]) -> CorsLayer {
    let base = CorsLayer::new()
        .allow_methods([Method::POST, Method::PATCH, Method::DELETE, Method::OPTIONS])
        .allow_headers([header::AUTHORIZATION, header::CONTENT_TYPE])
        .expose_headers([header::LOCATION]);

    let parsed: Vec<HeaderValue> = origins
        .iter()
        .filter_map(|o| {
            let trimmed = o.trim();
            if trimmed.is_empty() || trimmed.contains('*') {
                None
            } else {
                HeaderValue::from_str(trimmed).ok()
            }
        })
        .collect();
    if parsed.is_empty() {
        base
    } else {
        base.allow_origin(AllowOrigin::list(parsed))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use clap::Parser;
    use tower::ServiceExt;

    #[tokio::test]
    async fn media_cors_preflight_exposes_location_and_allows_configured_origin() {
        let mut cli = crate::config::CliConfig::parse_from(["svc-streaming"]);
        cli.cors_allowed_origins = vec!["https://app.waddles.gg".to_string()];
        let cfg = Config {
            cli,
            db_password: crate::config::Secret::new("db-pass"),
            cache_password: None,
            service_api_key: crate::config::Secret::new("svc-key"),
            jwt_hmac_secret: Some(crate::config::Secret::new("jwt-secret")),
        };
        let state = AppState::new(cfg, prometheus::Registry::new());
        let app = router(state);

        let res = app
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/rtc/rooms/00000000-0000-0000-0000-000000000001/whip")
                    .header(header::ORIGIN, "https://app.waddles.gg")
                    .header(header::ACCESS_CONTROL_REQUEST_METHOD, "POST")
                    .header(
                        header::ACCESS_CONTROL_REQUEST_HEADERS,
                        "authorization,content-type",
                    )
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(res.status(), StatusCode::OK);
        assert_eq!(
            res.headers()
                .get(header::ACCESS_CONTROL_ALLOW_ORIGIN)
                .and_then(|v| v.to_str().ok()),
            Some("https://app.waddles.gg")
        );
        let allow_methods = res
            .headers()
            .get(header::ACCESS_CONTROL_ALLOW_METHODS)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string();
        assert!(allow_methods.contains("POST") && allow_methods.contains("DELETE"));
    }

    /// A router shaped like the media routes (POST create, DELETE session) behind the layer.
    fn cors_app(origins: &[&str]) -> Router {
        let o: Vec<String> = origins.iter().map(|s| s.to_string()).collect();
        Router::new()
            .route(
                "/rtc/rooms/{room}/whip",
                axum::routing::post(|| async { StatusCode::CREATED }),
            )
            .route(
                "/rtc/rooms/{room}/sessions/{s}",
                axum::routing::delete(|| async { StatusCode::NO_CONTENT }),
            )
            .layer(media_cors_layer(&o))
    }

    async fn preflight(
        app: Router,
        uri: &str,
        origin: &str,
        method: &str,
    ) -> axum::response::Response {
        app.oneshot(
            Request::builder()
                .method(Method::OPTIONS)
                .uri(uri)
                .header(header::ORIGIN, origin)
                .header(header::ACCESS_CONTROL_REQUEST_METHOD, method)
                .header(
                    header::ACCESS_CONTROL_REQUEST_HEADERS,
                    "authorization,content-type",
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap()
    }

    fn hv(res: &axum::response::Response, h: header::HeaderName) -> Option<String> {
        res.headers()
            .get(h)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string)
    }

    #[tokio::test]
    async fn preflight_from_a_disallowed_origin_gets_no_allow_origin() {
        let app = cors_app(&["https://app.waddles.gg"]);
        for uri in ["/rtc/rooms/r1/whip", "/rtc/rooms/r1/sessions/s1"] {
            let res = preflight(app.clone(), uri, "https://evil.example", "POST").await;
            assert_eq!(hv(&res, header::ACCESS_CONTROL_ALLOW_ORIGIN), None, "{uri}");
        }
        // Exact match only: a different scheme/port/subdomain of an allowed host is refused.
        for o in [
            "http://app.waddles.gg",
            "https://app.waddles.gg:8443",
            "https://x.app.waddles.gg",
        ] {
            let res = preflight(app.clone(), "/rtc/rooms/r1/whip", o, "POST").await;
            assert_eq!(hv(&res, header::ACCESS_CONTROL_ALLOW_ORIGIN), None, "{o}");
        }
    }

    #[tokio::test]
    async fn allowed_origin_preflight_for_whip_and_session_delete_allows_sdp_headers() {
        let app = cors_app(&["https://app.waddles.gg"]);
        for (uri, method) in [
            ("/rtc/rooms/r1/whip", "POST"),
            ("/rtc/rooms/r1/sessions/s1", "DELETE"),
        ] {
            let res = preflight(app.clone(), uri, "https://app.waddles.gg", method).await;
            assert_eq!(
                hv(&res, header::ACCESS_CONTROL_ALLOW_ORIGIN).as_deref(),
                Some("https://app.waddles.gg")
            );
            let headers = hv(&res, header::ACCESS_CONTROL_ALLOW_HEADERS)
                .unwrap_or_default()
                .to_ascii_lowercase();
            assert!(headers.contains("authorization") && headers.contains("content-type"));
            assert!(hv(&res, header::ACCESS_CONTROL_ALLOW_METHODS)
                .unwrap_or_default()
                .contains(method));
        }
        // The real response carries Expose-Headers: Location for the allowed origin only.
        let res = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/rtc/rooms/r1/whip")
                    .header(header::ORIGIN, "https://app.waddles.gg")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            hv(&res, header::ACCESS_CONTROL_EXPOSE_HEADERS)
                .unwrap_or_default()
                .to_ascii_lowercase(),
            "location"
        );
    }

    #[tokio::test]
    async fn empty_allowlist_and_wildcard_entries_emit_no_cors() {
        for origins in [
            &[][..],
            &[""][..],
            &["*"][..],
            &["https://*.waddles.gg"][..],
        ] {
            let app = cors_app(origins);
            let res = preflight(app, "/rtc/rooms/r1/whip", "https://app.waddles.gg", "POST").await;
            assert_eq!(
                hv(&res, header::ACCESS_CONTROL_ALLOW_ORIGIN),
                None,
                "{origins:?}"
            );
        }
    }
}
