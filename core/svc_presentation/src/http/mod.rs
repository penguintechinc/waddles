//! HTTP layer: axum router wiring for the control-plane surface (health,
//! readiness, Prometheus metrics) plus P3/P4's live overlay viewer
//! (SSE + websocket) and push routes (see [`overlay`]) and P6's
//! PUSH-guarded image-upload route. Overlay routes are addressed by the
//! community's unguessable overlay code -- `/{overlay_code}/{surface}[/...]`
//! (see [`crate::overlay::code`]) -- never its integer id. Every overlay route
//! group is mounted already wrapped by `crate::overlay::router::with_view_guard`/
//! `with_push_guard`, which resolve the code to the real community id first --
//! see that module's doc for the check order and for why each guard is
//! applied per already-populated sub-router rather than once globally.

pub mod captions;
pub mod health;
pub mod overlay;
pub mod overlay_page;

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{DefaultBodyLimit, MatchedPath, Request, State};
use axum::middleware::Next;
use axum::response::Response;
use axum::routing::{get, post};
use axum::Router;
use sea_orm::DatabaseConnection;
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::images::store::ObjectStoreImageStore;
use crate::images::{AssetStore, ImageStore, SeaOrmImageAssetStore};
use crate::overlay::detok::{register_detok_metrics, DetokMetrics, OverlayDetokenizer};
use crate::overlay::render::{register_render_metrics, RenderMetrics, RenderedFrame};
use crate::overlay::{
    AppPushTrustSource, CaptionStore, CommunityContextStore, OverlayCodeResolver, PresentationHub,
    SeaOrmCaptionStore, SeaOrmCommunityContextStore, SeaOrmOverlayCodeResolver,
    SeaOrmViewCredentialStore,
};
use crate::telemetry::{CaptionMetrics, ImageMetrics, PushMetrics, RequestMetrics};

/// Shared state handed to every axum handler via `Router::with_state`.
/// Cheap to clone: everything behind an `Arc` (or already `Clone`, like
/// `DatabaseConnection`, which wraps an `Arc`-backed pool internally).
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub metrics: Arc<prometheus::Registry>,
    pub request_metrics: RequestMetrics,
    pub started_at: Instant,
    pub db: DatabaseConnection,
    /// Concrete `overlay_auth::ViewCredentialStore` this service mounts --
    /// shared with `crate::overlay::router::view_guarded_router` and
    /// (once P2/P3 add real routes) any render handler that also needs a
    /// validated `community_id`.
    pub view_store: Arc<SeaOrmViewCredentialStore>,
    /// Concrete `overlay_auth::PushTrustSource` this service mounts --
    /// shared with `crate::overlay::router::push_guarded_router`.
    pub push_trust_source: Arc<AppPushTrustSource>,
    /// P6/P9 image-upload/render additions.
    ///
    /// `None` when `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY`
    /// are unset -- a deployment that never enables
    /// `crate::flags::IMAGE_UPLOAD_FLAG` need not configure a bucket at
    /// all (`crate::images::store::ObjectStoreImageStore::from_config`'s
    /// own doc). `crate::images::upload::upload_image` returns a clear 500
    /// rather than panicking when this is `None` but the flag is ON.
    pub image_store: Option<Arc<dyn ImageStore>>,
    pub image_asset_store: Arc<dyn AssetStore>,
    pub image_upload_flag: Arc<dyn crate::flags::FeatureFlag>,
    pub image_metrics: ImageMetrics,
    /// The raw-push fan-out the caption pipeline owns (`crate::http::
    /// captions` validates and renders its own frames). Generic overlay
    /// routes never touch it: nothing here is detokenized or HTML-escaped, so
    /// it must never be subscribed to by a route that hands frames straight
    /// to a browser.
    pub hub: Arc<PresentationHub>,
    /// The fan-out `overlay::live_sse`/`live_ws` subscribe to and the
    /// `overlay::push` handler publishes into. It carries only
    /// [`RenderedFrame`]s -- output of the per-surface renderers run under
    /// [`Self::detokenizer`] -- so a frame that reaches a browser through
    /// these routes has already been detokenized and HTML-escaped.
    pub frame_hub: Arc<PresentationHub<RenderedFrame>>,
    /// Resolves `{user:<uuid>}` references to hub-api display names and
    /// renders the push with them in scope. Production replaces the default
    /// with a connected one via [`Self::with_hub_client`] (or an explicitly
    /// disabled one via [`Self::with_detokenization_disabled`]); a bare
    /// [`Self::new`] holds a loud, leak-free "unconfigured" one.
    pub detokenizer: Arc<OverlayDetokenizer>,
    /// The detokenizer's metric handles, kept so replacing the detokenizer
    /// (see [`Self::with_hub_client`]) never re-registers them.
    pub detok_metrics: DetokMetrics,
    /// Per-surface render duration/outcome metrics.
    pub render_metrics: RenderMetrics,
    /// Push-route metrics (end-to-end handling time, outcome).
    pub push_metrics: PushMetrics,
    /// Credential-community -> tenant + theme lookup for the push route.
    pub community_ctx: Arc<dyn CommunityContextStore>,
    /// Maps the overlay code in a URL (`/{overlay_code}/{surface}`) to the real
    /// community id; consulted by every route guard before any credential
    /// check. Production reads `communities.overlay_code` through a TTL cache;
    /// tests inject [`crate::overlay::StaticOverlayCodes`].
    pub overlay_codes: Arc<dyn OverlayCodeResolver>,
    /// Caption overlay (`crate::http::captions`): the reconnect-replay
    /// history store, its feature flag (OFF by default -- the Python
    /// `browser_source_core_module` stays the live caption path until it is
    /// flipped), and its metrics.
    pub caption_store: Arc<dyn CaptionStore>,
    pub captions_flag: Arc<dyn crate::flags::FeatureFlag>,
    pub caption_metrics: CaptionMetrics,
}

impl AppState {
    /// Builds the shared application state from a loaded [`Config`], the
    /// Prometheus [`prometheus::Registry`] created during telemetry init,
    /// and an already-open database connection (see
    /// [`crate::db::get_or_connect`]).
    pub fn new(config: Config, metrics: prometheus::Registry, db: DatabaseConnection) -> Self {
        let request_metrics = crate::telemetry::register_request_metrics(&metrics);
        let image_metrics = crate::telemetry::register_image_metrics(&metrics);
        let hub_metrics = crate::overlay::hub::register_hub_metrics(&metrics);
        let caption_metrics = crate::telemetry::register_caption_metrics(&metrics);
        let push_metrics = crate::telemetry::register_push_metrics(&metrics);
        let render_metrics = register_render_metrics(&metrics);
        let detok_metrics = register_detok_metrics(&metrics);
        let view_store = Arc::new(SeaOrmViewCredentialStore::new(db.clone()));
        let push_trust_source = Arc::new(AppPushTrustSource::from_config(&config));
        let image_asset_store: Arc<dyn AssetStore> =
            Arc::new(SeaOrmImageAssetStore::new(db.clone()));
        // Best-effort: a deployment that never enables
        // `crate::flags::IMAGE_UPLOAD_FLAG` need not set
        // `IMAGE_BUCKET_ACCESS_KEY_ID`/`IMAGE_BUCKET_SECRET_ACCESS_KEY` at
        // all, so a construction failure here is logged and degrades to
        // `None`, never a process-wide startup failure
        // (`rules/general.md` Red Flags).
        let image_store: Option<Arc<dyn ImageStore>> =
            match ObjectStoreImageStore::from_config(&config) {
                Ok(store) => Some(Arc::new(store)),
                Err(err) => {
                    tracing::warn!(
                        error = %err,
                        "image bucket not configured; image upload/render will fail if enabled"
                    );
                    None
                }
            };
        let license_client = crate::flags::build_license_client();
        let image_upload_flag = crate::flags::image_upload_flag(&license_client);
        let captions_flag = crate::flags::captions_flag(&license_client);
        let caption_store: Arc<dyn CaptionStore> = Arc::new(SeaOrmCaptionStore::new(db.clone()));
        // Both hubs share one set of metric handles (labeled by surface), so
        // the registry sees each series exactly once.
        let hub = Arc::new(PresentationHub::new(hub_metrics.clone()));
        let frame_hub = Arc::new(PresentationHub::new(hub_metrics));
        let detokenizer =
            Arc::new(OverlayDetokenizer::unconfigured().with_metrics(detok_metrics.clone()));
        let community_ctx: Arc<dyn CommunityContextStore> =
            Arc::new(SeaOrmCommunityContextStore::new(db.clone()));
        let overlay_code_metrics = crate::telemetry::register_overlay_code_metrics(&metrics);
        let overlay_codes: Arc<dyn OverlayCodeResolver> = Arc::new(
            SeaOrmOverlayCodeResolver::with_ttl(
                db.clone(),
                std::time::Duration::from_secs(config.cli.overlay_code_cache_ttl_seconds),
            )
            .with_metrics(overlay_code_metrics),
        );
        Self {
            config: Arc::new(config),
            metrics: Arc::new(metrics),
            request_metrics,
            started_at: Instant::now(),
            db,
            view_store,
            push_trust_source,
            image_store,
            image_asset_store,
            image_upload_flag,
            image_metrics,
            hub,
            frame_hub,
            detokenizer,
            detok_metrics,
            render_metrics,
            push_metrics,
            community_ctx,
            overlay_codes,
            caption_store,
            captions_flag,
            caption_metrics,
        }
    }

    /// Resolves overlay display names through `client` (hub-api's
    /// `IdentityService.ResolveDisplayNames`). The production path.
    #[must_use]
    pub fn with_hub_client(mut self, client: Arc<hub_client::HubClient>) -> Self {
        self.detokenizer = Arc::new(
            OverlayDetokenizer::from_hub_client(client).with_metrics(self.detok_metrics.clone()),
        );
        self
    }

    /// Swaps in an arbitrary detokenizer (tests inject a fake resolver; the
    /// metric handles are the caller's responsibility).
    #[must_use]
    pub fn with_detokenizer(mut self, detokenizer: OverlayDetokenizer) -> Self {
        self.detokenizer = Arc::new(detokenizer);
        self
    }

    /// Swaps in an arbitrary overlay-code resolver (tests inject a fixed table
    /// instead of standing up Postgres).
    #[must_use]
    pub fn with_overlay_codes(mut self, codes: Arc<dyn OverlayCodeResolver>) -> Self {
        self.overlay_codes = codes;
        self
    }

    /// The operator escape hatch (`PII_DETOKENIZATION_ENABLED=false`): no
    /// name is ever resolved, every user renders as the neutral label, and
    /// output stays escaped and leak-free.
    #[must_use]
    pub fn with_detokenization_disabled(mut self) -> Self {
        self.detokenizer =
            Arc::new(OverlayDetokenizer::disabled().with_metrics(self.detok_metrics.clone()));
        self
    }
}

/// The route *template* a request matched (`/overlay/captions/{key}`), or
/// `unmatched` for a 404. Used for every metric label and span field instead
/// of the raw URI: several overlay URLs carry a credential in the path or
/// query (`/overlay/captions/{key}`, `?key=`), and a raw path would copy that
/// VIEW key into `/metrics` (an unauthenticated scrape surface) and into
/// exported trace spans -- as well as minting one label series per community
/// id. The template set is closed and bounded.
fn route_label<B>(req: &axum::http::Request<B>) -> String {
    req.extensions().get::<MatchedPath>().map_or_else(
        || "unmatched".to_string(),
        |matched| matched.as_str().to_string(),
    )
}

/// Builds the per-request tracing span: method and route template only --
/// never the URI, which can carry a VIEW key (see [`route_label`]).
fn request_span<B>(req: &axum::http::Request<B>) -> tracing::Span {
    tracing::info_span!("http.request", method = %req.method(), route = %route_label(req))
}

/// Records every request into [`AppState::request_metrics`]: a labeled
/// counter and a latency histogram, labeled by route template (see
/// [`route_label`]). Applied to the whole control-plane router so
/// `/metrics` always has data once the service has served at least one
/// request (the `up` gauge covers the window before that).
async fn record_http_metrics(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let method = req.method().to_string();
    let path = route_label(&req);
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

/// Builds the control-plane router: public health/readiness (no auth), plus
/// P3/P4's overlay routes, P6's image-upload route
/// (`POST /{overlay_code}/image/push`) and the caption routes
/// ([`captions::routes`]), each wrapped by its own code-resolving guard
/// (or, for the caption viewer routes, validating the VIEW key in the
/// handler) before being merged in.
///
/// # URL scheme
///
/// Overlay routes live at `/{overlay_code}/{surface}[/...]`, where
/// `overlay_code` is the community's random 16-hex-char handle
/// ([`crate::overlay::code`]) -- never its sequential integer id. axum cannot
/// regex-constrain a path parameter, so the first segment is a plain capture
/// and the guards are the constraint: a segment that is not exactly
/// `[0-9a-f]{16}` (or names no community) is a `404` before any handler or
/// credential check runs. That also makes the root-level capture harmless to
/// its neighbours: `/health` and `/readyz` are literal one-segment routes that
/// win on specificity, `/overlay/...` and `/ws/...` are literal first
/// segments, and none of those words is 16 hex characters. `tests/routing.rs`
/// pins every pair.
///
/// Route specificity: the literal `image`/`caption` segments of the two
/// upload/ingest PUSH routes sit where the generic routes have a
/// `{surface}` capture. matchit gives a literal segment precedence over a
/// capture and backtracks to the capture when the literal's remaining path
/// doesn't match, so `.../image/live` and `.../caption/live` still reach the
/// generic VIEW routes while `.../image/push` and `.../caption/push` reach
/// their own handlers.
///
/// The browser overlay page (`GET /{overlay_code}/{surface}`, no
/// `/live`/`/push` suffix -- [`overlay_page`]) is mounted behind the same VIEW
/// guard as the live channels.
pub fn router(state: AppState) -> Router {
    let overlay_view = crate::overlay::router::with_view_guard(
        Router::new()
            .route("/{overlay_code}/{surface}", get(overlay_page::page))
            .route("/{overlay_code}/{surface}/live", get(overlay::live_sse))
            .route("/{overlay_code}/{surface}/live/ws", get(overlay::live_ws)),
        state.overlay_codes.clone(),
        state.view_store.clone(),
    );
    let overlay_push = crate::overlay::router::with_push_guard(
        Router::new().route("/{overlay_code}/{surface}/push", post(overlay::push)),
        state.overlay_codes.clone(),
        state.push_trust_source.clone(),
    );

    let public = Router::new()
        .route("/health", get(health::liveness))
        .route("/readyz", get(health::readiness))
        .merge(overlay_view)
        .merge(overlay_push);

    // The literal `image` segment plays the generic PUSH route's
    // `{surface}` role -- `overlay_auth::push_scope` is keyed on
    // `community_id` alone, so this reuses `with_push_guard` unmodified
    // (`crate::images::upload`'s own module doc).
    let image_upload = crate::overlay::router::with_push_guard(
        Router::new().route(
            "/{overlay_code}/image/push",
            post(crate::images::upload::upload_image),
        ),
        state.overlay_codes.clone(),
        state.push_trust_source.clone(),
    )
    // Bounds the raw request body independently of the post-decode
    // `IMAGE_MAX_BYTES` check in `crate::images::upload` -- defense in
    // depth against an oversized multipart body being buffered at all.
    // +64KiB headroom for multipart boundaries/field overhead.
    .layer(DefaultBodyLimit::max(
        state.config.cli.image_max_bytes as usize + 64 * 1024,
    ));

    public
        .merge(image_upload)
        .merge(captions::routes(&state))
        .with_state(state.clone())
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            record_http_metrics,
        ))
        .layer(TraceLayer::new_for_http().make_span_with(|req: &Request| request_span(req)))
}

/// Builds the secondary Prometheus metrics router, bound to its own port
/// (`METRICS_PORT`, default 9090) per `rules/critical-rules.md`
/// Observability -- kept separate from the control-plane router so a
/// scraper never needs auth and never shares a listener with user
/// traffic.
pub fn metrics_router(state: AppState) -> Router {
    Router::new()
        .route("/metrics", get(health::metrics))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use std::io::Write;
    use tower::ServiceExt;

    /// A `tracing_subscriber` writer that captures everything into a buffer.
    #[derive(Clone, Default)]
    struct Capture(Arc<std::sync::Mutex<Vec<u8>>>);

    impl Write for Capture {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Capture {
        type Writer = Capture;
        fn make_writer(&'a self) -> Capture {
            self.clone()
        }
    }

    impl Capture {
        fn text(&self) -> String {
            String::from_utf8(self.0.lock().unwrap().clone()).unwrap()
        }
    }

    #[test]
    fn route_label_is_unmatched_without_a_matched_path() {
        let req = axum::http::Request::builder()
            .uri("/overlay/captions/SECRET?key=SECRET")
            .body(())
            .unwrap();
        assert_eq!(route_label(&req), "unmatched");
    }

    /// The request span carries method and route template only. Several
    /// overlay URLs embed the VIEW key in the path or query; the default
    /// `TraceLayer` span would copy the full URI -- key included -- onto
    /// every exported span.
    #[tokio::test]
    async fn request_span_names_the_route_template_and_never_the_uri() {
        let capture = Capture::default();
        let subscriber = tracing_subscriber::fmt()
            .with_writer(capture.clone())
            .with_ansi(false)
            .finish();
        // Thread-local default: valid across awaits on this current-thread
        // test runtime.
        let _guard = tracing::subscriber::set_default(subscriber);

        let app: Router = Router::new()
            .route(
                "/overlay/captions/{key}",
                get(|| async {
                    tracing::info!("inside the handler");
                    "ok"
                }),
            )
            .layer(TraceLayer::new_for_http().make_span_with(|req: &Request| request_span(req)));
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/overlay/captions/SUPERSECRETKEY?community_id=1&key=SUPERSECRETKEY")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);

        let logged = capture.text();
        assert!(logged.contains("inside the handler"), "{logged}");
        assert!(
            logged.contains("route=/overlay/captions/{key}"),
            "span must carry the route template: {logged}"
        );
        assert!(logged.contains("method=GET"), "{logged}");
        assert!(
            !logged.contains("SUPERSECRETKEY"),
            "a VIEW key leaked into a span: {logged}"
        );
    }
}
