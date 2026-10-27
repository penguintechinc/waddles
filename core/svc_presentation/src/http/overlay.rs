//! P4 routes: the live overlay viewer channel (SSE + websocket, `GET
//! /{overlay_code}/{surface}/live` and `.../live/ws`) and the push endpoint
//! (`POST /{overlay_code}/{surface}/push`) action-stage adapters call. Both
//! route groups are mounted already wrapped by the code-resolving guards (see
//! `crate::overlay::router::with_view_guard`/`with_push_guard`, wired in
//! `crate::http::router`) -- every handler in this module runs only after
//! that guard has resolved the overlay code to a community, validated the
//! caller's credential for *that* community and inserted the matching
//! `Extension<ViewCredential>`/`Extension<PushCredential>`, so handlers trust
//! `credential.community_id` and never re-parse or re-trust the raw path
//! segment (which is only the public overlay code).
//!
//! Fan-out itself is `crate::overlay::hub::PresentationHub` (P3) -- this
//! module owns only the HTTP/SSE/websocket framing on top of it.
//!
//! # Render before publish
//!
//! The push handler never forwards the caller's body. It looks up the
//! community's tenant + theme from the *verified* `community_id`, runs the
//! push through [`crate::overlay::detok::OverlayDetokenizer`] (hub-api
//! display-name resolution + per-surface renderer, which HTML-escapes every
//! free-text field and validates `image_url`), and publishes only the
//! resulting [`RenderedFrame`] into `AppState::frame_hub`. `/live` and
//! `/live/ws` subscribe to that hub, so every frame a browser can receive
//! through them is already detokenized and escaped: no raw `{user:<uuid>}`,
//! UUID or markup ever leaves this service.

use std::convert::Infallible;
use std::time::{Duration, Instant};

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::Response;
use axum::{Extension, Json};
use futures::stream::{self, Stream};
use overlay_auth::{PushCredential, ViewCredential};
use overlay_schema::{ConnectedFrame, OverlayPush, Surface};
use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::error::ApiError;
use crate::http::AppState;
use crate::overlay::community_ctx::CommunityContextError;
use crate::overlay::detok::DetokScope;
use crate::overlay::hub::{HubSubscription, RecvOutcome};
use crate::overlay::render::{RenderError, RenderTheme, RenderedFrame};

/// SSE keep-alive / websocket ping interval. Matches the legacy Python
/// scaffold's own `_HEARTBEAT_INTERVAL_SECONDS`
/// (`core/svc_presentation/blueprints/overlay.py`) exactly, so an
/// already-deployed OBS browser source or reverse-proxy timeout tuned
/// against that value doesn't need to change.
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(15);

/// Path params shared by every route in this module.
#[derive(Debug, Deserialize)]
pub struct OverlayRouteParams {
    /// The raw URL-path segment: the community's overlay code. Echoed back
    /// verbatim into [`ConnectedFrame::community`] (see that type's doc on why
    /// this is the slug, not the resolved numeric id -- it now genuinely is an
    /// opaque slug, so the integer id never leaves the service). Never used for
    /// authorization: `Extension<ViewCredential>`/`Extension<PushCredential>`'s
    /// already-validated `community_id` is what gates access, not this string.
    pub overlay_code: String,
    pub surface: String,
}

/// `POST /{overlay_code}/{surface}/push`'s response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct PushResponseBody {
    pub status: &'static str,
    /// The overlay code the caller addressed, echoed back (the caller already
    /// holds it; the integer community id is never exposed).
    pub overlay_code: String,
    pub surface: &'static str,
}

/// Parses a URL-path surface segment against the closed [`Surface`] set.
/// Not a method on `Surface` itself: that type belongs to `overlay_schema`
/// (contract C3), the single source of truth for wire *shape* -- parsing
/// a path segment is this service's own concern, not a wire-shape question.
fn parse_surface(raw: &str) -> Option<Surface> {
    Surface::ALL.iter().copied().find(|s| s.as_str() == raw)
}

fn unknown_surface(raw: &str) -> ApiError {
    ApiError::NotFound(format!("unknown surface: {raw}"))
}

/// Maps a renderer rejection to the HTTP error the pusher sees. Every
/// [`RenderError`] message is built from static field names/reasons only
/// (never push content), so it is safe to echo back.
fn render_error_to_api(err: RenderError) -> ApiError {
    match err {
        RenderError::NotYetImplemented { .. } => ApiError::Unimplemented(err.to_string()),
        RenderError::EmptyPush { .. }
        | RenderError::MissingField { .. }
        | RenderError::InvalidField { .. } => ApiError::BadRequest(err.to_string()),
    }
}

/// `POST /{overlay_code}/{surface}/push` -- renders one push and
/// publishes the sanitized frame to every current subscriber of this
/// community/surface.
///
/// The request body is `overlay_schema::OverlayPush` verbatim; axum's `Json`
/// extractor itself fails the request with 400 before this handler ever runs
/// if the body doesn't deserialize against that shape. After the community
/// check the handler:
///
/// 1. looks up the community's tenant + theme from the verified
///    `credential.community_id` (never from the body or path);
/// 2. resolves the push's user references through hub-api and renders it with
///    [`crate::overlay::detok::OverlayDetokenizer::render_with_metrics`] --
///    HTML-escaping every free-text field and detokenizing every
///    `{user:<uuid>}` -- so the frame carries no raw token, UUID or markup;
/// 3. publishes that [`RenderedFrame`] (never the raw body) to `frame_hub`.
///
/// A renderer rejection (empty push, missing field, bad `image_url`) is a 400
/// to the pusher and publishes nothing. If hub-api is unreachable the push
/// still renders -- every user as the neutral label, an `ERROR` logged and
/// counted -- rather than being dropped (`overlay::detok`'s fail-safe-empty
/// contract); if only the community lookup's database read fails the push is
/// published the same way, with the default theme.
///
/// No `#[utoipa::path(...)]` here (unlike `http::health`'s handlers):
/// utoipa's `axum_extras` integration derives the OpenAPI request body
/// schema from this handler's own `Json<OverlayPush>` extractor type, which
/// requires `OverlayPush: utoipa::ToSchema` -- `overlay_schema` (contract
/// C3) doesn't derive that, and annotating this service's own wire-shape
/// understanding onto a type it doesn't own isn't this chunk's call to
/// make. A documented, known gap rather than a silently-wrong spec.
#[tracing::instrument(
    name = "overlay.push",
    skip_all,
    fields(community_id = credential.community_id)
)]
pub async fn push(
    State(state): State<AppState>,
    Extension(credential): Extension<PushCredential>,
    Path(params): Path<OverlayRouteParams>,
    Json(body): Json<OverlayPush>,
) -> Result<Json<PushResponseBody>, ApiError> {
    let started = Instant::now();
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    // No path/credential comparison here: the PUSH guard resolved
    // `params.overlay_code` to a community and verified the credential is
    // scoped to exactly that community, so `credential.community_id` is
    // authoritative and the path segment is just the public handle.
    // `image` and `caption` have their own PUSH routes (literal path
    // segments that win over this generic one). Reaching here with either
    // means a routing regression; refuse loudly rather than render it with
    // the wrong pipeline.
    if matches!(surface, Surface::Image | Surface::Caption) {
        tracing::error!(
            surface = surface.as_str(),
            "generic push route reached for a surface with its own route"
        );
        return Err(ApiError::BadRequest(format!(
            "surface {} is pushed through its own route",
            surface.as_str()
        )));
    }

    let community_id = credential.community_id;
    let (scope, theme, degraded) = match state.community_ctx.context(community_id).await {
        Ok(ctx) => (
            DetokScope::new(ctx.tenant_id, community_id),
            ctx.theme,
            false,
        ),
        Err(err @ (CommunityContextError::NotFound(_) | CommunityContextError::OutOfRange(_))) => {
            tracing::warn!(community_id, error = %err, "push for an unknown community rejected");
            return Err(ApiError::NotFound("community not found".to_string()));
        }
        Err(err) => {
            tracing::error!(
                community_id,
                error = %err,
                "community context unavailable; rendering with no tenant (every user as the \
                 neutral label) and the default theme"
            );
            (
                DetokScope::new(String::new(), community_id),
                RenderTheme::default(),
                true,
            )
        }
    };

    let rendered = state
        .detokenizer
        .render_with_metrics(&scope, surface, &body, &theme, &state.render_metrics)
        .await;
    let outcome = match &rendered {
        Ok(_) if degraded => "degraded",
        Ok(_) => "published",
        Err(_) => "rejected",
    };
    state
        .push_metrics
        .pushes_total
        .with_label_values(&[surface.as_str(), outcome])
        .inc();
    state
        .push_metrics
        .push_duration_seconds
        .with_label_values(&[surface.as_str()])
        .observe(started.elapsed().as_secs_f64());

    let frame = rendered.map_err(render_error_to_api)?;
    state.frame_hub.publish(community_id, surface, frame);
    tracing::debug!(
        community_id,
        surface = surface.as_str(),
        degraded,
        "published rendered overlay frame"
    );
    Ok(Json(PushResponseBody {
        status: "published",
        overlay_code: params.overlay_code,
        surface: surface.as_str(),
    }))
}

/// One frame on the live channel (SSE `data:` payload / websocket text
/// frame -- the same JSON on both transports). Untagged, like
/// `overlay_schema::OverlayEnvelope`: a [`ConnectedFrame`] is
/// `{community, surface}`; a rendered frame is the flat, `content_type`-tagged
/// object [`RenderedFrame`] serializes to, so a client tells them apart by the
/// presence of `content_type`. Borrowed so a frame fanned out to N
/// subscribers is serialized N times but cloned zero times.
#[derive(Serialize)]
#[serde(untagged)]
enum LiveEnvelope<'a> {
    Connected(&'a ConnectedFrame),
    Rendered(&'a RenderedFrame),
}

/// `GET /{overlay_code}/{surface}/live` -- SSE live-update channel.
/// First frame is always the [`ConnectedFrame`]; every frame after is a
/// fanned-out, already-sanitized [`RenderedFrame`]. Keep-alive comments (axum's
/// built-in [`KeepAlive`]) are sent every [`HEARTBEAT_INTERVAL`] of
/// otherwise-idle time, so a reverse proxy or OBS's embedded Chromium
/// never times the connection out on a quiet overlay.
pub async fn live_sse(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Path(params): Path<OverlayRouteParams>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    let subscription = state.frame_hub.subscribe(credential.community_id, surface);
    tracing::debug!(
        community_id = credential.community_id,
        surface = surface.as_str(),
        "overlay SSE subscriber connected"
    );
    let connected = ConnectedFrame {
        community: params.overlay_code,
        surface,
    };
    Ok(Sse::new(live_event_stream(subscription, connected))
        .keep_alive(KeepAlive::new().interval(HEARTBEAT_INTERVAL)))
}

enum StreamState {
    Connected(HubSubscription<RenderedFrame>, ConnectedFrame),
    Streaming(HubSubscription<RenderedFrame>),
}

/// Builds the actual frame stream [`live_sse`] serves: the one
/// [`ConnectedFrame`], then every subsequent fanned-out rendered frame.
/// Split out from the handler so it's unit-testable without going through
/// axum's `Sse`/`IntoResponse` wrapping.
///
/// A `Lagged` outcome or a (rare) serialization failure is logged and
/// skipped -- the connection stays open rather than being torn down over one
/// dropped/malformed frame.
fn live_event_stream(
    subscription: HubSubscription<RenderedFrame>,
    connected: ConnectedFrame,
) -> impl Stream<Item = Result<Event, Infallible>> {
    stream::unfold(
        StreamState::Connected(subscription, connected),
        |mut state| async move {
            loop {
                match state {
                    StreamState::Connected(subscription, connected) => {
                        let envelope = LiveEnvelope::Connected(&connected);
                        // A `ConnectedFrame` is one plain string field plus
                        // a closed enum -- no floats, no non-UTF8 bytes, no
                        // non-string map keys -- so unlike a rendered frame
                        // below, serialization cannot fail here.
                        let event = Event::default()
                            .json_data(&envelope)
                            .expect("ConnectedFrame serialization is infallible");
                        return Some((Ok(event), StreamState::Streaming(subscription)));
                    }
                    StreamState::Streaming(mut subscription) => match subscription.recv().await {
                        Some(RecvOutcome::Push(frame)) => {
                            let envelope = LiveEnvelope::Rendered(&frame);
                            match Event::default().json_data(&envelope) {
                                Ok(event) => {
                                    return Some((Ok(event), StreamState::Streaming(subscription)))
                                }
                                Err(err) => {
                                    tracing::error!(
                                        error = %err,
                                        "failed to serialize overlay push frame, skipping"
                                    );
                                    state = StreamState::Streaming(subscription);
                                    continue;
                                }
                            }
                        }
                        Some(RecvOutcome::Lagged(dropped)) => {
                            tracing::warn!(
                                dropped,
                                "overlay SSE subscriber lagged, frames dropped"
                            );
                            state = StreamState::Streaming(subscription);
                            continue;
                        }
                        None => return None,
                    },
                }
            }
        },
    )
}

/// `GET /{overlay_code}/{surface}/live/ws` -- websocket equivalent of
/// [`live_sse`]: same Connected-frame-then-rendered-frames contract, same JSON
/// frame shape per message (a websocket text frame instead of SSE's
/// `data: ...\n\n` wrapping -- the JSON itself is identical across both
/// transports). Ping frames every
/// [`HEARTBEAT_INTERVAL`] serve the same dead-connection-reaping role as
/// the SSE keep-alive comment.
pub async fn live_ws(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Path(params): Path<OverlayRouteParams>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    let subscription = state.frame_hub.subscribe(credential.community_id, surface);
    tracing::debug!(
        community_id = credential.community_id,
        surface = surface.as_str(),
        "overlay websocket subscriber connected"
    );
    let connected = ConnectedFrame {
        community: params.overlay_code,
        surface,
    };
    Ok(ws.on_upgrade(move |socket| {
        run_ws_connection(socket, subscription, connected, HEARTBEAT_INTERVAL)
    }))
}

/// The websocket connection's full lifecycle: sends the Connected frame,
/// then loops forwarding hub frames / sending heartbeat pings / watching
/// for the client closing the socket, until any one of those ends the
/// connection. Split out from [`live_ws`] (which can't itself be
/// unit-tested -- it needs a real `WebSocketUpgrade`) so the forwarding/
/// heartbeat/reaping logic is directly testable with a short interval
/// instead of [`HEARTBEAT_INTERVAL`]'s real 15 seconds.
async fn run_ws_connection(
    mut socket: WebSocket,
    mut subscription: HubSubscription<RenderedFrame>,
    connected: ConnectedFrame,
    heartbeat_interval: Duration,
) {
    let envelope = LiveEnvelope::Connected(&connected);
    // Infallible for the same reason as `live_event_stream`'s Connected
    // branch -- see that function's doc.
    let text =
        serde_json::to_string(&envelope).expect("ConnectedFrame serialization is infallible");
    if socket.send(Message::Text(text.into())).await.is_err() {
        return;
    }

    let mut heartbeat = tokio::time::interval(heartbeat_interval);
    // A stalled send must not trigger a burst of catch-up pings.
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    heartbeat.tick().await; // the first tick fires immediately; consume it

    loop {
        tokio::select! {
            outcome = subscription.recv() => {
                match outcome {
                    Some(RecvOutcome::Push(frame)) => {
                        let envelope = LiveEnvelope::Rendered(&frame);
                        match serde_json::to_string(&envelope) {
                            Ok(text) => {
                                if socket.send(Message::Text(text.into())).await.is_err() {
                                    break;
                                }
                            }
                            Err(err) => {
                                tracing::error!(
                                    error = %err,
                                    "failed to serialize overlay push frame, skipping"
                                );
                            }
                        }
                    }
                    Some(RecvOutcome::Lagged(dropped)) => {
                        tracing::warn!(dropped, "overlay websocket subscriber lagged, frames dropped");
                    }
                    None => break,
                }
            }
            _ = heartbeat.tick() => {
                if socket.send(Message::Ping(Vec::new().into())).await.is_err() {
                    break;
                }
            }
            incoming = socket.recv() => {
                match incoming {
                    None => break,
                    Some(Err(err)) => {
                        tracing::warn!(error = %err, "overlay websocket transport error, closing");
                        break;
                    }
                    Some(Ok(Message::Close(_))) => break,
                    // Pongs and any stray client->server frames need no
                    // response of their own -- axum already auto-replies
                    // to inbound pings -- just keep the loop alive.
                    Some(Ok(_)) => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::overlay::community_ctx::{StaticCommunityContextStore, StaticFailure};
    use crate::overlay::detok::test_support::{FakeResolver, USER_A, USER_B};
    use crate::overlay::detok::OverlayDetokenizer;
    use crate::overlay::hub::{register_hub_metrics, PresentationHub};
    use crate::overlay::render::RenderedContent;
    use axum::response::IntoResponse;
    use futures::StreamExt;
    use overlay_schema::{AlertPayload, ChatMessagePayload, GoalPayload};
    use std::sync::Arc;

    /// An arbitrary well-formed overlay code (the handlers only echo it).
    const CODE: &str = "a1b2c3d4e5f60718";

    fn test_hub() -> PresentationHub<RenderedFrame> {
        PresentationHub::new(register_hub_metrics(&prometheus::Registry::new()))
    }

    #[test]
    fn parse_surface_accepts_every_known_surface() {
        for surface in Surface::ALL {
            assert_eq!(parse_surface(surface.as_str()), Some(*surface));
        }
    }

    #[test]
    fn parse_surface_rejects_an_unknown_value() {
        assert_eq!(parse_surface("not-a-real-surface"), None);
    }

    #[test]
    fn render_errors_map_to_the_right_http_status() {
        let s = Surface::Chat;
        let cases = [
            (
                RenderError::EmptyPush { surface: s },
                axum::http::StatusCode::BAD_REQUEST,
            ),
            (
                RenderError::MissingField {
                    surface: s,
                    field: "chat_message",
                },
                axum::http::StatusCode::BAD_REQUEST,
            ),
            (
                RenderError::InvalidField {
                    surface: s,
                    field: "image_url",
                    reason: "x",
                },
                axum::http::StatusCode::BAD_REQUEST,
            ),
            (
                RenderError::NotYetImplemented { surface: s },
                axum::http::StatusCode::NOT_IMPLEMENTED,
            ),
        ];
        for (err, status) in cases {
            assert_eq!(render_error_to_api(err).into_response().status(), status);
        }
    }

    fn base_state() -> AppState {
        use crate::config::{CliConfig, Config, Secret};
        use clap::Parser;
        use sea_orm::{DatabaseBackend, MockDatabase};

        let cli = CliConfig::parse_from(["svc-presentation"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            image_bucket_access_key_id: None,
            image_bucket_secret_access_key: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        AppState::new(config, prometheus::Registry::new(), db)
    }

    /// State whose resolver knows `USER_A`/`USER_B` and whose community
    /// lookup answers tenant `7` -- the happy path.
    fn test_state() -> (AppState, Arc<FakeResolver>) {
        let resolver = Arc::new(FakeResolver::with(&[(USER_A, "Al<i>ce"), (USER_B, "Bob")]));
        let mut state = base_state();
        state.detokenizer = Arc::new(
            OverlayDetokenizer::new(resolver.clone()).with_metrics(state.detok_metrics.clone()),
        );
        state.community_ctx = Arc::new(StaticCommunityContextStore::ok("7"));
        (state, resolver)
    }

    /// A `PushCredential` as `overlay_auth::require_push_credential` would
    /// have inserted it -- constructed directly (not minted/verified as a
    /// real JWT) since these are handler-level unit tests exercising
    /// `push()` itself, not the auth guard in front of it (that's already
    /// covered by `overlay_auth`'s and `overlay::router`'s own test suites,
    /// and end to end by `tests/overlay_render.rs`).
    fn fake_push_credential(community_id: i64) -> PushCredential {
        PushCredential {
            claims: service_auth::ServiceClaims {
                iss: "hub-api".to_string(),
                aud: "waddlebot-internal".to_string(),
                sub: "spiffe://penguintech.io/alpha/test".to_string(),
                scope: overlay_auth::push_scope(community_id),
                iat: 0,
                nbf: 0,
                exp: 0,
                jti: "test-jti".to_string(),
            },
            community_id,
        }
    }

    async fn do_push(
        state: &AppState,
        overlay_code: &str,
        credential_community: i64,
        surface: &str,
        body: OverlayPush,
    ) -> Result<Json<PushResponseBody>, ApiError> {
        push(
            State(state.clone()),
            Extension(fake_push_credential(credential_community)),
            Path(OverlayRouteParams {
                overlay_code: overlay_code.to_string(),
                surface: surface.to_string(),
            }),
            Json(body),
        )
        .await
    }

    fn hostile_chat() -> OverlayPush {
        OverlayPush {
            chat_message: Some(ChatMessagePayload {
                user: USER_B.to_string(),
                display_name: "<img src=x onerror=alert(1)>".to_string(),
                platform: "twitch".to_string(),
                text: format!("<script>alert(1)</script> hi {{user:{USER_A}}}"),
            }),
            ..Default::default()
        }
    }

    fn frame_json(frame: &RenderedFrame) -> String {
        serde_json::to_string(frame).expect("rendered frame serializes")
    }

    /// The core guarantee: a hostile, tokenized push is detokenized and
    /// escaped BEFORE it reaches the hub every browser channel reads.
    /// regression: svc-presentation published the raw OverlayPush.
    #[tokio::test]
    async fn push_publishes_a_detokenized_escaped_frame_never_the_raw_body() {
        let (state, resolver) = test_state();
        let mut sub = state.frame_hub.subscribe(42, Surface::Chat);

        let Json(response) = do_push(&state, CODE, 42, "chat", hostile_chat())
            .await
            .expect("push succeeds");
        assert_eq!(response.status, "published");
        assert_eq!(response.surface, "chat");

        let frame = match sub.recv().await {
            Some(RecvOutcome::Push(frame)) => frame,
            _ => panic!("expected the published frame"),
        };
        let json = frame_json(&frame);
        assert!(!json.contains("<script"), "raw <script> leaked: {json}");
        assert!(!json.contains("onerror=alert(1)>"), "raw markup: {json}");
        assert!(
            !json.contains(USER_A) && !json.contains(USER_B),
            "uuid: {json}"
        );
        assert!(!json.contains("{user:"), "raw token leaked: {json}");
        assert!(!json.contains("Al<i>ce"), "name not escaped: {json}");
        // Resolved + escaped: the hub-api name for USER_B, and USER_A inline.
        assert!(json.contains("Bob"), "{json}");
        assert!(json.contains("Al&lt;i&gt;ce"), "{json}");
        match &frame.content {
            RenderedContent::Chat(chat) => {
                assert_eq!(chat.display_name, "Bob", "caller display_name ignored");
            }
            other => panic!("expected chat content, got {other:?}"),
        }

        // The tenant the resolver was asked about is the community's own.
        let calls = resolver.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "one batched hub-api call");
        assert_eq!(calls[0].0, "7");
    }

    /// The raw hub (caption-only) never sees a generic push.
    #[tokio::test]
    async fn push_does_not_touch_the_raw_hub() {
        let (state, _) = test_state();
        let mut raw = state.hub.subscribe(42, Surface::Chat);
        let _ = do_push(&state, CODE, 42, "chat", hostile_chat())
            .await
            .expect("push succeeds");
        let waited = tokio::time::timeout(Duration::from_millis(100), raw.recv()).await;
        assert!(waited.is_err(), "a raw frame reached the unsanitized hub");
    }

    #[tokio::test]
    async fn push_publishes_a_themed_frame() {
        let (mut state, _) = test_state();
        state.community_ctx = Arc::new(StaticCommunityContextStore::with_theme(
            "7",
            RenderTheme {
                primary_color: Some("#abcdef".to_string()),
                ..Default::default()
            },
        ));
        let mut sub = state.frame_hub.subscribe(42, Surface::Media);
        let _ = do_push(
            &state,
            CODE,
            42,
            "media",
            OverlayPush {
                title: Some("hello".to_string()),
                ..Default::default()
            },
        )
        .await
        .expect("push succeeds");
        match sub.recv().await {
            Some(RecvOutcome::Push(frame)) => assert_eq!(frame.theme.primary_color, "#abcdef"),
            _ => panic!("expected the published frame"),
        }
    }

    #[tokio::test]
    async fn push_still_renders_every_user_as_the_neutral_label_when_hub_api_fails() {
        let mut state = base_state();
        state.detokenizer = Arc::new(OverlayDetokenizer::new(Arc::new(FakeResolver::failing())));
        state.community_ctx = Arc::new(StaticCommunityContextStore::ok("7"));
        let mut sub = state.frame_hub.subscribe(42, Surface::Chat);
        let _ = do_push(&state, CODE, 42, "chat", hostile_chat())
            .await
            .expect("a hub-api outage must not drop the push");
        let frame = match sub.recv().await {
            Some(RecvOutcome::Push(frame)) => frame,
            _ => panic!("expected the published frame"),
        };
        let json = frame_json(&frame);
        assert!(json.contains(egress_detokenizer::NEUTRAL_LABEL), "{json}");
        assert!(!json.contains(USER_A) && !json.contains(USER_B), "{json}");
        assert!(
            !json.contains("{user:") && !json.contains("<script"),
            "{json}"
        );
    }

    #[tokio::test]
    async fn push_with_a_failing_community_lookup_degrades_to_neutral_labels() {
        let (mut state, resolver) = test_state();
        state.community_ctx = Arc::new(StaticCommunityContextStore::failing(StaticFailure::Db));
        let mut sub = state.frame_hub.subscribe(42, Surface::Chat);
        let _ = do_push(&state, CODE, 42, "chat", hostile_chat())
            .await
            .expect("a lookup failure must not drop the push");
        let frame = match sub.recv().await {
            Some(RecvOutcome::Push(frame)) => frame,
            _ => panic!("expected the published frame"),
        };
        let json = frame_json(&frame);
        assert!(json.contains(egress_detokenizer::NEUTRAL_LABEL), "{json}");
        assert!(
            !json.contains("Bob"),
            "no tenant => no name resolved: {json}"
        );
        assert_eq!(resolver.call_count(), 0, "no tenant => hub-api never asked");
        let metrics = crate::telemetry::render_metrics(&state.metrics).unwrap();
        assert!(metrics.contains(r#"outcome="degraded""#), "{metrics}");
    }

    #[tokio::test]
    async fn push_for_an_unknown_community_is_404_and_publishes_nothing() {
        let (mut state, _) = test_state();
        state.community_ctx = Arc::new(StaticCommunityContextStore::failing(
            StaticFailure::NotFound,
        ));
        let mut sub = state.frame_hub.subscribe(42, Surface::Chat);
        let err = do_push(&state, CODE, 42, "chat", hostile_chat())
            .await
            .expect_err("unknown community");
        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::NOT_FOUND
        );
        assert!(tokio::time::timeout(Duration::from_millis(50), sub.recv())
            .await
            .is_err());
    }

    #[tokio::test]
    async fn a_renderer_rejection_is_400_publishes_nothing_and_is_counted() {
        let (state, _) = test_state();
        let mut sub = state.frame_hub.subscribe(42, Surface::Chat);
        // `chat` with no `chat_message` is a missing-field rejection.
        let err = do_push(&state, CODE, 42, "chat", OverlayPush::default())
            .await
            .expect_err("empty chat push");
        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::BAD_REQUEST
        );
        assert!(tokio::time::timeout(Duration::from_millis(50), sub.recv())
            .await
            .is_err());
        let metrics = crate::telemetry::render_metrics(&state.metrics).unwrap();
        assert!(metrics.contains(r#"outcome="rejected""#), "{metrics}");
    }

    #[tokio::test]
    async fn a_hostile_image_url_is_rejected_not_published() {
        let (state, _) = test_state();
        let mut sub = state.frame_hub.subscribe(42, Surface::Media);
        let err = do_push(
            &state,
            CODE,
            42,
            "media",
            OverlayPush {
                image_url: Some("javascript:alert(1)".to_string()),
                ..Default::default()
            },
        )
        .await
        .expect_err("non-http(s) image_url");
        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::BAD_REQUEST
        );
        assert!(tokio::time::timeout(Duration::from_millis(50), sub.recv())
            .await
            .is_err());
    }

    /// The path segment is only the public handle: the frame is published
    /// under the *credential's* community, and the response echoes the code the
    /// caller already holds -- never the integer community id.
    #[tokio::test]
    async fn push_publishes_under_the_credentials_community_and_echoes_only_the_code() {
        let (state, _) = test_state();
        let mut sub_42 = state.frame_hub.subscribe(42, Surface::Media);
        let mut sub_43 = state.frame_hub.subscribe(43, Surface::Media);
        let Json(response) = do_push(
            &state,
            CODE,
            42,
            "media",
            OverlayPush {
                title: Some("hello".to_string()),
                ..Default::default()
            },
        )
        .await
        .expect("push succeeds");
        assert_eq!(response.overlay_code, CODE);
        let json = serde_json::to_string(&response).expect("response serializes");
        assert!(
            !json.contains("community"),
            "response leaks a community field: {json}"
        );
        assert!(matches!(sub_42.recv().await, Some(RecvOutcome::Push(_))));
        assert!(
            tokio::time::timeout(Duration::from_millis(50), sub_43.recv())
                .await
                .is_err(),
            "another community's subscriber must not see this push"
        );
    }

    #[tokio::test]
    async fn push_handler_rejects_an_unknown_surface_with_404() {
        let (state, _) = test_state();
        let err = do_push(
            &state,
            CODE,
            42,
            "not-a-real-surface",
            OverlayPush::default(),
        )
        .await
        .expect_err("unknown surface must be rejected");

        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::NOT_FOUND
        );
    }

    #[tokio::test]
    async fn push_handler_refuses_surfaces_that_own_a_dedicated_route() {
        let (state, _) = test_state();
        for surface in ["image", "caption"] {
            let err = do_push(&state, CODE, 42, surface, OverlayPush::default())
                .await
                .expect_err("dedicated-route surface must be refused here");
            assert_eq!(
                err.into_response().status(),
                axum::http::StatusCode::BAD_REQUEST,
                "{surface}"
            );
        }
    }

    fn rendered_text(text: &str) -> RenderedFrame {
        let push = OverlayPush {
            text: Some(text.to_string()),
            ..Default::default()
        };
        crate::overlay::render::render(Surface::Ticker, &push, &RenderTheme::default())
            .expect("ticker renders")
    }

    #[tokio::test]
    async fn live_event_stream_sends_connected_first_then_rendered_frames() {
        let hub = test_hub();
        let subscription = hub.subscribe(7, Surface::Ticker);
        let connected = ConnectedFrame {
            community: "my-community".to_string(),
            surface: Surface::Ticker,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));

        hub.publish(7, Surface::Ticker, rendered_text("hi"));

        let first = stream
            .next()
            .await
            .expect("connected frame")
            .expect("infallible");
        let first = format!("{first:?}");
        assert!(
            first.contains("\\\"community\\\":\\\"my-community\\\""),
            "{first}"
        );
        assert!(!first.contains("content_type"), "{first}");

        let second = stream
            .next()
            .await
            .expect("rendered frame")
            .expect("infallible");
        let second = format!("{second:?}");
        assert!(
            second.contains("\\\"content_type\\\":\\\"ticker\\\""),
            "{second}"
        );
        assert!(second.contains("\\\"text\\\":\\\"hi\\\""), "{second}");
    }

    #[tokio::test]
    async fn live_event_stream_ends_once_the_hub_is_dropped() {
        let hub = test_hub();
        let subscription = hub.subscribe(8, Surface::Goals);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::Goals,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));
        let _ = stream.next().await.expect("connected frame");
        drop(hub);
        assert!(stream.next().await.is_none());
    }

    #[tokio::test]
    async fn live_event_stream_skips_a_lagged_notification_and_keeps_streaming() {
        let metrics = register_hub_metrics(&prometheus::Registry::new());
        let hub: PresentationHub<RenderedFrame> = PresentationHub::with_capacity(1, metrics);
        let subscription = hub.subscribe(9, Surface::Ticker);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::Ticker,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));
        let _ = stream.next().await.expect("connected frame");

        // Capacity 1: the second publish lags the first one out before the
        // stream ever reads it.
        hub.publish(9, Surface::Ticker, rendered_text("dropped"));
        hub.publish(9, Surface::Ticker, rendered_text("kept"));

        // The lag is swallowed internally (logged, not surfaced as a
        // stream item) -- the very next item is the surviving frame, not
        // an error and not a second "connected" frame.
        let next = stream
            .next()
            .await
            .expect("surviving frame")
            .expect("infallible");
        assert!(format!("{next:?}").contains("\\\"text\\\":\\\"kept\\\""));
    }

    /// A goal with a non-finite value must not take the stream down:
    /// serde_json writes `null` for NaN, and either way the loop continues.
    #[tokio::test]
    async fn live_event_stream_survives_a_non_finite_goal_value() {
        let hub = test_hub();
        let subscription = hub.subscribe(11, Surface::Goals);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::Goals,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));
        let _ = stream.next().await.expect("connected frame");
        let push = OverlayPush {
            goal: Some(GoalPayload {
                label: "g".to_string(),
                current: f64::NAN,
                target: 10.0,
                unit: None,
            }),
            ..Default::default()
        };
        if let Ok(frame) =
            crate::overlay::render::render(Surface::Goals, &push, &RenderTheme::default())
        {
            hub.publish(11, Surface::Goals, frame);
        }
        hub.publish(11, Surface::Goals, rendered_text("after"));
        // Whatever the first frame did, the stream is still alive and
        // delivers the next one.
        let mut saw_after = false;
        for _ in 0..2 {
            let item = tokio::time::timeout(Duration::from_secs(1), stream.next())
                .await
                .expect("stream stays alive");
            if let Some(Ok(event)) = item {
                saw_after |= format!("{event:?}").contains("after");
            }
        }
        assert!(saw_after);
    }

    #[test]
    fn push_response_and_alert_shapes_are_unchanged() {
        // The pusher-facing response is a stable contract.
        let body = serde_json::to_value(PushResponseBody {
            status: "published",
            overlay_code: CODE.to_string(),
            surface: "chat",
        })
        .unwrap();
        assert_eq!(body["status"], "published");
        assert_eq!(body["overlay_code"], CODE);
        assert!(body.get("community").is_none());
        // And an alert push renders through the same path (no panics on the
        // optional-field shapes callers send).
        let alert = OverlayPush {
            alert: Some(AlertPayload {
                alert_type: "follow".to_string(),
                user: None,
                display_name: Some("Name".to_string()),
                amount: None,
                message: None,
            }),
            ..Default::default()
        };
        assert!(
            crate::overlay::render::render(Surface::AlertBox, &alert, &RenderTheme::default())
                .is_ok()
        );
    }

    #[tokio::test]
    async fn run_ws_connection_sends_a_heartbeat_ping_on_an_idle_connection() {
        use tokio_tungstenite::tungstenite::Message as TtMessage;

        let hub = test_hub();
        let subscription = hub.subscribe(10, Surface::FullScreen);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::FullScreen,
        };

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // axum handlers must be `Fn` (callable more than once) even though
        // this test only ever drives one real request -- a `HubSubscription`
        // isn't `Clone`, so it's parked behind an `Arc<Mutex<Option<_>>>` and
        // `take()`n the one time the handler actually runs, rather than
        // moved directly into a single-use closure.
        let once = std::sync::Arc::new(tokio::sync::Mutex::new(Some((subscription, connected))));
        let app = axum::Router::new().route(
            "/ws",
            axum::routing::get(move |ws: WebSocketUpgrade| {
                let once = once.clone();
                async move {
                    let (subscription, connected) =
                        once.lock().await.take().expect("handler called once");
                    ws.on_upgrade(move |socket| {
                        run_ws_connection(
                            socket,
                            subscription,
                            connected,
                            Duration::from_millis(20),
                        )
                    })
                }
            }),
        );
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let (mut client, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws"))
            .await
            .expect("client connects");

        // First frame: the Connected envelope.
        let connected_msg = client
            .next()
            .await
            .expect("connected frame")
            .expect("no transport error");
        assert!(matches!(connected_msg, TtMessage::Text(_)));

        // tokio-tungstenite auto-replies to pings internally and surfaces
        // the inbound ping itself as the next yielded item.
        let ping_msg = tokio::time::timeout(Duration::from_secs(2), client.next())
            .await
            .expect("a heartbeat ping arrives within the timeout")
            .expect("stream item")
            .expect("no transport error");
        assert!(matches!(ping_msg, TtMessage::Ping(_)));

        drop(client);
        server.abort();
    }

    #[tokio::test]
    async fn run_ws_connection_forwards_a_rendered_frame_as_text() {
        use tokio_tungstenite::tungstenite::Message as TtMessage;

        let hub = Arc::new(test_hub());
        let subscription = hub.subscribe(12, Surface::Ticker);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::Ticker,
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let once = Arc::new(tokio::sync::Mutex::new(Some((subscription, connected))));
        let app = axum::Router::new().route(
            "/ws",
            axum::routing::get(move |ws: WebSocketUpgrade| {
                let once = once.clone();
                async move {
                    let (subscription, connected) =
                        once.lock().await.take().expect("handler called once");
                    ws.on_upgrade(move |socket| {
                        run_ws_connection(socket, subscription, connected, Duration::from_secs(60))
                    })
                }
            }),
        );
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let (mut client, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws"))
            .await
            .expect("client connects");
        let _connected = client.next().await.expect("connected").expect("ok");

        hub.publish(12, Surface::Ticker, rendered_text("over the wire"));
        let msg = tokio::time::timeout(Duration::from_secs(2), client.next())
            .await
            .expect("frame arrives")
            .expect("stream item")
            .expect("no transport error");
        match msg {
            TtMessage::Text(text) => {
                assert!(text.contains("\"content_type\":\"ticker\""), "{text}");
                assert!(text.contains("over the wire"), "{text}");
            }
            other => panic!("expected a text frame, got {other:?}"),
        }
        drop(client);
        server.abort();
    }
}
