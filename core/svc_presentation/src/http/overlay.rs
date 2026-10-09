//! P4 routes: the live overlay viewer channel (SSE + websocket, `GET
//! .../live` and `GET .../live/ws`) and the push endpoint (`POST
//! .../push`) action-stage adapters call. Both route groups are mounted
//! already wrapped by `overlay_auth`'s guard middleware (see
//! `crate::overlay::router::with_view_guard`/`with_push_guard`, wired in
//! `crate::http::router`) -- every handler in this module runs only after
//! that guard has validated the caller's credential and inserted the
//! matching `Extension<ViewCredential>`/`Extension<PushCredential>`, so
//! handlers trust `credential.community_id` instead of re-parsing/
//! re-trusting the raw path segment.
//!
//! Fan-out itself is `crate::overlay::hub::PresentationHub` (P3) -- this
//! module owns only the HTTP/SSE/websocket framing on top of it.

use std::convert::Infallible;
use std::time::Duration;

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::Response;
use axum::{Extension, Json};
use futures::stream::{self, Stream};
use overlay_auth::{PushCredential, ViewCredential};
use overlay_schema::{ConnectedFrame, OverlayEnvelope, OverlayPush, Surface};
use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::error::ApiError;
use crate::http::AppState;
use crate::overlay::hub::{HubSubscription, RecvOutcome};

/// SSE keep-alive / websocket ping interval. Matches the legacy Python
/// scaffold's own `_HEARTBEAT_INTERVAL_SECONDS`
/// (`core/svc_presentation/blueprints/overlay.py`) exactly, so an
/// already-deployed OBS browser source or reverse-proxy timeout tuned
/// against that value doesn't need to change.
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(15);

/// Path params shared by every route in this module.
#[derive(Debug, Deserialize)]
pub struct OverlayRouteParams {
    /// The raw URL-path segment -- echoed back verbatim into
    /// [`ConnectedFrame::community`] (see that type's doc on why this is
    /// the slug, not the resolved numeric id). Never used for
    /// authorization: `Extension<ViewCredential>`/`Extension<
    /// PushCredential>`'s already-validated `community_id` is what gates
    /// access, not this string.
    pub community: String,
    pub surface: String,
}

/// `POST /overlay/{community}/{surface}/push`'s response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct PushResponseBody {
    pub status: &'static str,
    pub community: String,
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

/// `POST /overlay/{community}/{surface}/push` -- publishes one frame to
/// every current [`crate::overlay::hub::PresentationHub`] subscriber for
/// this community/surface. The request body is `overlay_schema::
/// OverlayPush` verbatim; axum's `Json` extractor itself fails the request
/// with 400 before this handler ever runs if the body doesn't deserialize
/// against that shape -- this is the "validate the push body against
/// overlay_schema" step, done by the extractor rather than hand-rolled
/// here.
///
/// No `#[utoipa::path(...)]` here (unlike `http::health`'s handlers):
/// utoipa's `axum_extras` integration derives the OpenAPI request body
/// schema from this handler's own `Json<OverlayPush>` extractor type, which
/// requires `OverlayPush: utoipa::ToSchema` -- `overlay_schema` (contract
/// C3) doesn't derive that, and annotating this service's own wire-shape
/// understanding onto a type it doesn't own isn't this chunk's call to
/// make. A documented, known gap rather than a silently-wrong spec.
pub async fn push(
    State(state): State<AppState>,
    Extension(credential): Extension<PushCredential>,
    Path(params): Path<OverlayRouteParams>,
    Json(body): Json<OverlayPush>,
) -> Result<Json<PushResponseBody>, ApiError> {
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    // Never echo or trust the unvalidated path segment: it must match the
    // community the verified credential was issued for.
    if params.community != credential.community_id.to_string() {
        return Err(ApiError::Forbidden(
            "path community does not match credential".to_string(),
        ));
    }
    state.hub.publish(credential.community_id, surface, body);
    tracing::debug!(
        community_id = credential.community_id,
        surface = surface.as_str(),
        "published overlay push"
    );
    Ok(Json(PushResponseBody {
        status: "published",
        community: params.community,
        surface: surface.as_str(),
    }))
}

/// `GET /overlay/{community}/{surface}/live` -- SSE live-update channel.
/// First frame is always [`OverlayEnvelope::Connected`]; every frame after
/// is a fanned-out [`OverlayEnvelope::Push`]. Keep-alive comments (axum's
/// built-in [`KeepAlive`]) are sent every [`HEARTBEAT_INTERVAL`] of
/// otherwise-idle time, so a reverse proxy or OBS's embedded Chromium
/// never times the connection out on a quiet overlay.
pub async fn live_sse(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Path(params): Path<OverlayRouteParams>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    let subscription = state.hub.subscribe(credential.community_id, surface);
    tracing::debug!(
        community_id = credential.community_id,
        surface = surface.as_str(),
        "overlay SSE subscriber connected"
    );
    let connected = ConnectedFrame {
        community: params.community,
        surface,
    };
    Ok(Sse::new(live_event_stream(subscription, connected))
        .keep_alive(KeepAlive::new().interval(HEARTBEAT_INTERVAL)))
}

enum StreamState {
    Connected(HubSubscription, ConnectedFrame),
    Streaming(HubSubscription),
}

/// Builds the actual frame stream [`live_sse`] serves: the one
/// [`OverlayEnvelope::Connected`] frame, then every subsequent fanned-out
/// push. Split out from the handler so it's unit-testable without going
/// through axum's `Sse`/`IntoResponse` wrapping.
///
/// A `Lagged` outcome or a (rare, non-finite-float-caused) serialization
/// failure is logged and skipped -- the connection stays open rather than
/// being torn down over one dropped/malformed frame.
fn live_event_stream(
    subscription: HubSubscription,
    connected: ConnectedFrame,
) -> impl Stream<Item = Result<Event, Infallible>> {
    stream::unfold(
        StreamState::Connected(subscription, connected),
        |mut state| async move {
            loop {
                match state {
                    StreamState::Connected(subscription, connected) => {
                        let envelope = OverlayEnvelope::Connected(connected);
                        // A `ConnectedFrame` is one plain string field plus
                        // a closed enum -- no floats, no non-UTF8 bytes, no
                        // non-string map keys -- so unlike an `OverlayPush`
                        // frame below, serialization cannot fail here.
                        let event = Event::default()
                            .json_data(&envelope)
                            .expect("ConnectedFrame serialization is infallible");
                        return Some((Ok(event), StreamState::Streaming(subscription)));
                    }
                    StreamState::Streaming(mut subscription) => match subscription.recv().await {
                        Some(RecvOutcome::Push(push)) => {
                            let envelope = OverlayEnvelope::Push(Box::new((*push).clone()));
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

/// `GET /overlay/{community}/{surface}/live/ws` -- websocket equivalent of
/// [`live_sse`]: same Connected-frame-then-pushes contract, same JSON
/// frame shape per message (a websocket text frame instead of SSE's
/// `data: ...\n\n` wrapping -- `overlay_schema::envelope`'s module doc:
/// the JSON itself is identical across both transports). Ping frames every
/// [`HEARTBEAT_INTERVAL`] serve the same dead-connection-reaping role as
/// the SSE keep-alive comment.
pub async fn live_ws(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Path(params): Path<OverlayRouteParams>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    let surface = parse_surface(&params.surface).ok_or_else(|| unknown_surface(&params.surface))?;
    let subscription = state.hub.subscribe(credential.community_id, surface);
    tracing::debug!(
        community_id = credential.community_id,
        surface = surface.as_str(),
        "overlay websocket subscriber connected"
    );
    let connected = ConnectedFrame {
        community: params.community,
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
    mut subscription: HubSubscription,
    connected: ConnectedFrame,
    heartbeat_interval: Duration,
) {
    let envelope = OverlayEnvelope::Connected(connected);
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
                    Some(RecvOutcome::Push(push)) => {
                        let envelope = OverlayEnvelope::Push(Box::new((*push).clone()));
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
    use crate::overlay::hub::{register_hub_metrics, PresentationHub};
    use axum::response::IntoResponse;
    use futures::StreamExt;

    fn test_hub() -> PresentationHub {
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

    fn test_state() -> AppState {
        use crate::config::{CliConfig, Config, Secret};
        use clap::Parser;
        use sea_orm::{DatabaseBackend, MockDatabase};

        let cli = CliConfig::parse_from(["svc-presentation"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        AppState::new(config, prometheus::Registry::new(), db)
    }

    /// A `PushCredential` as `overlay_auth::require_push_credential` would
    /// have inserted it -- constructed directly (not minted/verified as a
    /// real JWT) since these are handler-level unit tests exercising
    /// `push()` itself, not the auth guard in front of it (that's already
    /// covered by `overlay_auth`'s and `overlay::router`'s own test suites).
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

    #[tokio::test]
    async fn push_handler_publishes_through_the_real_hub_and_returns_200() {
        let state = test_state();
        let mut sub = state.hub.subscribe(42, Surface::Media);
        let body = OverlayPush {
            title: Some("hello".to_string()),
            ..Default::default()
        };

        let Json(response) = push(
            State(state),
            Extension(fake_push_credential(42)),
            Path(OverlayRouteParams {
                community: "42".to_string(),
                surface: "media".to_string(),
            }),
            Json(body),
        )
        .await
        .expect("push succeeds");

        assert_eq!(response.status, "published");
        assert_eq!(response.surface, "media");

        // Exercises the real `PresentationHub::publish` path reached
        // through the handler, not a mock.
        match sub.recv().await {
            Some(RecvOutcome::Push(push)) => assert_eq!(push.title.as_deref(), Some("hello")),
            _ => panic!("expected the published frame"),
        }
    }

    #[tokio::test]
    async fn push_handler_rejects_community_mismatch_with_403() {
        let state = test_state();
        let err = push(
            State(state),
            Extension(fake_push_credential(42)),
            Path(OverlayRouteParams {
                community: "43".to_string(),
                surface: "media".to_string(),
            }),
            Json(OverlayPush::default()),
        )
        .await
        .expect_err("mismatched community must be rejected");
        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::FORBIDDEN
        );
    }

    #[tokio::test]
    async fn push_handler_rejects_an_unknown_surface_with_404() {
        let state = test_state();
        let err = push(
            State(state),
            Extension(fake_push_credential(42)),
            Path(OverlayRouteParams {
                community: "42".to_string(),
                surface: "not-a-real-surface".to_string(),
            }),
            Json(OverlayPush::default()),
        )
        .await
        .expect_err("unknown surface must be rejected");

        assert_eq!(
            err.into_response().status(),
            axum::http::StatusCode::NOT_FOUND
        );
    }

    #[tokio::test]
    async fn live_event_stream_sends_connected_first_then_pushes() {
        let hub = test_hub();
        let subscription = hub.subscribe(7, Surface::Chat);
        let connected = ConnectedFrame {
            community: "my-community".to_string(),
            surface: Surface::Chat,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));

        hub.publish(
            7,
            Surface::Chat,
            OverlayPush {
                text: Some("hi".to_string()),
                ..Default::default()
            },
        );

        let first = stream
            .next()
            .await
            .expect("connected frame")
            .expect("infallible");
        assert!(format!("{first:?}").contains("\\\"community\\\":\\\"my-community\\\""));

        let second = stream
            .next()
            .await
            .expect("push frame")
            .expect("infallible");
        assert!(format!("{second:?}").contains("\\\"text\\\":\\\"hi\\\""));
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
        let hub = PresentationHub::with_capacity(1, metrics);
        let subscription = hub.subscribe(9, Surface::Ticker);
        let connected = ConnectedFrame {
            community: "c".to_string(),
            surface: Surface::Ticker,
        };
        let mut stream = Box::pin(live_event_stream(subscription, connected));
        let _ = stream.next().await.expect("connected frame");

        // Capacity 1: the second publish lags the first one out before the
        // stream ever reads it.
        hub.publish(
            9,
            Surface::Ticker,
            OverlayPush {
                text: Some("dropped".to_string()),
                ..Default::default()
            },
        );
        hub.publish(
            9,
            Surface::Ticker,
            OverlayPush {
                text: Some("kept".to_string()),
                ..Default::default()
            },
        );

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
}
