//! Caption overlay routes -- the Rust port of the Python
//! `core/browser_source_core_module`'s closed-caption trio:
//!
//! | Python | Rust (this module) |
//! |---|---|
//! | `GET /overlay/captions/<overlay_key>` (OBS page) | [`caption_page`] -- same URL |
//! | `WS /ws/captions/<community_id>?key=` | [`caption_ws`] -- same URL |
//! | `POST /api/v1/internal/captions` (`X-Service-Key`) | [`push_caption`] -- `POST /overlay/{community}/caption/push` (PUSH JWT) |
//!
//! The viewer URLs are unchanged, so an already-configured OBS browser source
//! keeps working across the cutover. The ingest route intentionally is NOT
//! URL-compatible: the legacy endpoint authenticated with one static shared
//! `X-Service-Key` and trusted a `community_id` in the request body
//! (`rules/security.md`: no long-lived static secrets; tenant comes from the
//! verified credential, never the body). The replacement is the standard
//! PUSH-guarded overlay route -- a per-community machine JWT, the same
//! credential every other surface's push uses -- so callers of the old
//! endpoint must be repointed at cutover.
//!
//! # Data flow
//!
//! ```text
//! POST .../caption/push --validate--> hub.publish(community, Caption) --> every connected /ws/captions
//!                         \--persist--> caption_events (reconnect replay, 7-day retention)
//! ```
//!
//! # PII
//!
//! A push carries a tokenized `user` UUID plus an already-detokenized
//! `display_name` (see `overlay_schema::CaptionPayload`). The display name is
//! forwarded to live viewers and **never stored or logged**; only `user_ref`
//! is persisted, so replayed history has no attribution name. No log line,
//! span field or metric label in this module carries caption text, a name, or
//! a user reference.
//!
//! # Auth
//!
//! The viewer routes authenticate with the same hashed VIEW key every overlay
//! route uses (`overlay_auth::validate_view_token`, `?key=` on the websocket;
//! the path segment on the page). They call it directly instead of mounting
//! the `with_view_guard` middleware because the legacy URLs name their
//! parameters differently (`{key}`, `{community_id}`) than the guard's
//! `{community}`/`{surface}` extraction expects.
//!
//! Every route is gated on [`crate::flags::CAPTIONS_FLAG`] (OFF by default),
//! so the Python path stays the live one until the flag is deliberately
//! switched on.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::extract::ws::rejection::WebSocketUpgradeRejection;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{DefaultBodyLimit, Path, Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use chrono::{DateTime, SecondsFormat, Utc};
use overlay_auth::{validate_view_token, OverlayAuthError, PushCredential};
use overlay_schema::{OverlayPush, Surface};
use serde::{Deserialize, Serialize};

use crate::error::ApiError;
use crate::http::overlay::HEARTBEAT_INTERVAL;
use crate::http::AppState;
use crate::overlay::caption_store::{
    CaptionStore, NewCaptionEvent, StoredCaption, HISTORY_LIMIT, HISTORY_WINDOW,
};
use crate::overlay::hub::{HubSubscription, RecvOutcome};
use crate::overlay::render::{render_caption, CaptionContent};
use crate::telemetry::CaptionMetrics;

/// The OBS browser-source page, embedded at compile time. Static: it holds no
/// community data, key or caption text.
const CAPTION_PAGE: &str = include_str!("templates/caption-overlay.html");

/// CSP for [`CAPTION_PAGE`]: inline script/style (the page is one
/// self-contained document), same-origin plus websocket connections only,
/// nothing else loadable. `frame-ancestors` is deliberately omitted -- the
/// legacy route was embeddable from any origin (`X-Frame-Options: ALLOWALL`,
/// "Required for OBS browser source").
const CAPTION_PAGE_CSP: &str = "default-src 'none'; script-src 'unsafe-inline'; \
     style-src 'unsafe-inline'; connect-src 'self' ws: wss:; base-uri 'none'; form-action 'none'";

/// Cap on a caption push body. A caption is at most a few KiB of text (two
/// 2000-char fields plus short metadata, ~17 KiB worst case in 4-byte
/// scalars); 64 KiB leaves headroom without letting a caller buffer megabytes.
const MAX_PUSH_BODY_BYTES: usize = 64 * 1024;

/// Cap on a frame the *client* may send us. The overlay only ever sends a
/// tiny keep-alive ping, so anything larger is abuse and is refused by the
/// websocket layer itself.
const MAX_CLIENT_MESSAGE_BYTES: usize = 1024;

/// Errors the caption viewer routes return: a rejected overlay credential
/// (mapped to 400/401/403/500 by `overlay_auth`, never leaking which check
/// failed) or a typed API error.
#[derive(Debug, thiserror::Error)]
pub enum CaptionRouteError {
    #[error(transparent)]
    Auth(#[from] OverlayAuthError),
    #[error(transparent)]
    Api(#[from] ApiError),
}

impl IntoResponse for CaptionRouteError {
    fn into_response(self) -> Response {
        match self {
            CaptionRouteError::Auth(err) => err.into_response(),
            CaptionRouteError::Api(err) => err.into_response(),
        }
    }
}

/// Builds every caption route: the two viewer routes (credential checked in
/// the handler) and the PUSH-guarded ingest route.
///
/// The ingest route spells the surface as the literal `caption` segment --
/// like P6's `image/push` -- so it takes precedence over the generic
/// `/overlay/{community}/{surface}/push` for exactly that surface (a caption
/// is validated, persisted and broadcast, never passed through raw), while
/// every other surface still reaches the generic route. Specificity is
/// pinned by `tests/routing.rs`.
pub fn routes(state: &AppState) -> Router<AppState> {
    let viewer = Router::new()
        .route("/overlay/captions/{key}", get(caption_page))
        .route("/ws/captions/{community_id}", get(caption_ws));

    let ingest = crate::overlay::router::with_push_guard(
        Router::new().route("/overlay/{community}/caption/push", post(push_caption)),
        state.push_trust_source.clone(),
    )
    .layer(DefaultBodyLimit::max(MAX_PUSH_BODY_BYTES));

    viewer.merge(ingest)
}

/// Fails with 403 unless [`crate::flags::CAPTIONS_FLAG`] is ON.
async fn ensure_enabled(state: &AppState) -> Result<(), ApiError> {
    if state.captions_flag.enabled().await {
        Ok(())
    } else {
        Err(ApiError::Forbidden(
            "captions are not enabled for this deployment".to_string(),
        ))
    }
}

/// Query string of the OBS page URL. The legacy page URL always carried
/// `community_id` too (its script needs it to build the websocket URL).
#[derive(Debug, Deserialize)]
pub struct PageQuery {
    pub community_id: Option<i64>,
}

/// `GET /overlay/captions/{key}?community_id=N` -- serves the caption page
/// once the VIEW key is validated for that community. Unlike the Python
/// route (which looked the key up by hash alone, so it could not know the
/// community) the key is validated *against* `community_id`, the same
/// community-scoped check every other overlay route makes.
#[tracing::instrument(name = "caption.page", skip_all)]
pub async fn caption_page(
    State(state): State<AppState>,
    Path(key): Path<String>,
    Query(query): Query<PageQuery>,
) -> Result<Response, CaptionRouteError> {
    ensure_enabled(&state).await?;
    let community_id = query.community_id.ok_or_else(|| {
        ApiError::BadRequest("the community_id query parameter is required".to_string())
    })?;
    validate_view_token(state.view_store.as_ref(), community_id, &key).await?;
    tracing::debug!(community_id, "serving caption overlay page");
    Ok(page_response())
}

/// The page response: the embedded HTML with its security headers. The page
/// URL carries the VIEW key, so `Referrer-Policy: no-referrer` stops it
/// leaking to anything the page might link to, and `no-store` keeps it out
/// of shared caches.
fn page_response() -> Response {
    let mut response = (StatusCode::OK, CAPTION_PAGE).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CAPTION_PAGE_CSP),
    );
    response
}

/// Query string of the websocket URL.
#[derive(Debug, Deserialize)]
pub struct WsQuery {
    pub key: Option<String>,
}

/// `GET /ws/captions/{community_id}?key=...` -- the live caption websocket.
/// Authenticates (and flag-gates) *before* the upgrade, so a bad key is an
/// ordinary 401/403 HTTP response rather than an accepted-then-closed socket.
#[tracing::instrument(name = "caption.ws_handshake", skip_all, fields(community_id = community_id))]
pub async fn caption_ws(
    State(state): State<AppState>,
    Path(community_id): Path<i64>,
    Query(query): Query<WsQuery>,
    ws: Result<WebSocketUpgrade, WebSocketUpgradeRejection>,
) -> Result<Response, CaptionRouteError> {
    let metrics = &state.caption_metrics;
    if let Err(err) = ensure_enabled(&state).await {
        metrics
            .ws_connections_total
            .with_label_values(&["disabled"])
            .inc();
        return Err(err.into());
    }

    let key = query.key.unwrap_or_default();
    if let Err(err) = validate_view_token(state.view_store.as_ref(), community_id, &key).await {
        metrics
            .ws_connections_total
            .with_label_values(&["denied"])
            .inc();
        return Err(err.into());
    }

    // Authenticated; if this isn't actually a websocket upgrade request,
    // answer with the extractor's own 400/426.
    let ws = match ws {
        Ok(ws) => ws,
        Err(rejection) => return Ok(rejection.into_response()),
    };

    // Subscribe before the history query so a caption pushed between the two
    // is delivered live rather than lost (the worst case is one duplicate,
    // matching the Python module's register-then-replay order).
    let subscription = state.hub.subscribe(community_id, Surface::Caption);
    metrics
        .ws_connections_total
        .with_label_values(&["accepted"])
        .inc();
    tracing::debug!(community_id, "caption websocket accepted");

    let store = Arc::clone(&state.caption_store);
    let metrics = state.caption_metrics.clone();
    Ok(ws
        .max_message_size(MAX_CLIENT_MESSAGE_BYTES)
        .max_frame_size(MAX_CLIENT_MESSAGE_BYTES)
        .on_upgrade(move |socket| {
            run_caption_ws(
                socket,
                subscription,
                store,
                community_id,
                metrics,
                HEARTBEAT_INTERVAL,
            )
        }))
}

/// One caption as sent down the websocket. Built only through
/// [`CaptionWsFrame::live`]/[`CaptionWsFrame::replayed`] from validated or
/// stored data -- an explicit DTO, never a raw row or push
/// (`rules/security.md` Output Validation). Deliberately omits the author's
/// UUID and the platform: the overlay needs neither, so neither is sent to
/// the browser.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CaptionWsFrame {
    /// Always `"caption"` (the overlay script dispatches on it).
    #[serde(rename = "type")]
    pub kind: &'static str,
    /// Present on live frames; absent on replayed history (never stored).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    pub original: String,
    pub translated: Option<String>,
    pub detected_lang: Option<String>,
    pub target_lang: Option<String>,
    pub confidence: Option<f64>,
    /// RFC 3339 UTC.
    pub timestamp: String,
}

impl CaptionWsFrame {
    /// A frame for a freshly-pushed, already-validated caption.
    pub fn live(content: &CaptionContent, now: DateTime<Utc>) -> Self {
        Self {
            kind: "caption",
            display_name: Some(content.display_name.clone()),
            original: content.original.clone(),
            translated: content.translated.clone(),
            detected_lang: content.detected_lang.clone(),
            target_lang: content.target_lang.clone(),
            confidence: content.confidence,
            timestamp: now.to_rfc3339_opts(SecondsFormat::Millis, true),
        }
    }

    /// A frame replaying a stored caption: no display name, original
    /// timestamp.
    pub fn replayed(stored: &StoredCaption) -> Self {
        Self {
            kind: "caption",
            display_name: None,
            original: stored.original.clone(),
            translated: stored.translated.clone(),
            detected_lang: stored.detected_lang.clone(),
            target_lang: stored.target_lang.clone(),
            confidence: stored.confidence,
            timestamp: stored
                .created_at
                .to_rfc3339_opts(SecondsFormat::Millis, true),
        }
    }
}

/// Serializes a frame. Infallible by construction: [`CaptionWsFrame`] holds
/// only strings, `Option<String>` and an `Option<f64>`, derives plain
/// `Serialize`, and `serde_json` writes a non-finite float as `null` rather
/// than erroring (pinned by `non_finite_confidence_serializes_as_null`) --
/// the same documented-infallible precedent as `ConnectedFrame` in
/// `crate::http::overlay`.
fn encode_frame(frame: &CaptionWsFrame) -> String {
    serde_json::to_string(frame).expect("CaptionWsFrame serialization is infallible")
}

/// Sends `frame` as one text message; `false` means the client is gone.
async fn send_frame(socket: &mut WebSocket, frame: &CaptionWsFrame) -> bool {
    socket
        .send(Message::Text(encode_frame(frame).into()))
        .await
        .is_ok()
}

/// `true` when a client text message is a keep-alive ping: the legacy plain
/// `ping` string or the page's `{"type":"ping"}`.
fn is_client_ping(text: &str) -> bool {
    #[derive(Deserialize)]
    struct ClientMessage {
        #[serde(rename = "type")]
        kind: String,
    }
    text == "ping"
        || serde_json::from_str::<ClientMessage>(text).is_ok_and(|message| message.kind == "ping")
}

/// The websocket connection's whole lifecycle: replay recent history, then
/// forward live captions, answer client pings, send a heartbeat ping every
/// `heartbeat_interval`, and stop when the client goes away. `pub` (with the
/// interval as a parameter) so the integration tests can drive it with a
/// short interval instead of [`HEARTBEAT_INTERVAL`]'s real 15 seconds.
///
/// A history-query failure is logged at ERROR and counted, then the live
/// stream continues without a replay -- live captions are the feature, the
/// replay is a convenience, so one failing dependency degrades rather than
/// breaks it.
#[tracing::instrument(name = "caption.ws", skip_all, fields(community_id = community_id))]
pub async fn run_caption_ws(
    mut socket: WebSocket,
    mut subscription: HubSubscription,
    store: Arc<dyn CaptionStore>,
    community_id: i64,
    metrics: CaptionMetrics,
    heartbeat_interval: Duration,
) {
    let since = Utc::now() - chrono::Duration::seconds(HISTORY_WINDOW.as_secs() as i64);
    match store.recent(community_id, since, HISTORY_LIMIT).await {
        Ok(history) => {
            metrics.history_replayed.observe(history.len() as f64);
            tracing::debug!(replayed = history.len(), "replaying caption history");
            for stored in &history {
                if !send_frame(&mut socket, &CaptionWsFrame::replayed(stored)).await {
                    return;
                }
            }
        }
        Err(err) => {
            metrics.history_replayed.observe(0.0);
            metrics
                .ws_connections_total
                .with_label_values(&["history_unavailable"])
                .inc();
            tracing::error!(
                error = %err,
                error_debug = ?err,
                "caption history unavailable; continuing with live captions only"
            );
        }
    }

    let mut heartbeat = tokio::time::interval(heartbeat_interval);
    // A stalled send must not trigger a burst of catch-up pings.
    heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    heartbeat.tick().await; // the first tick fires immediately; consume it

    loop {
        tokio::select! {
            outcome = subscription.recv() => match outcome {
                Some(RecvOutcome::Push(push)) => match render_caption(&push) {
                    Ok(content) => {
                        if !send_frame(&mut socket, &CaptionWsFrame::live(&content, Utc::now())).await {
                            break;
                        }
                    }
                    Err(err) => {
                        // Only validated captions are ever published to this
                        // surface, so this means a bug upstream of the hub.
                        // The message is PII-free by construction.
                        tracing::error!(error = %err, "invalid caption reached the hub, skipping");
                    }
                },
                Some(RecvOutcome::Lagged(dropped)) => {
                    tracing::warn!(dropped, "caption websocket subscriber lagged, frames dropped");
                }
                None => break,
            },
            _ = heartbeat.tick() => {
                if socket.send(Message::Ping(Vec::new().into())).await.is_err() {
                    break;
                }
            }
            incoming = socket.recv() => match incoming {
                None => break,
                Some(Err(err)) => {
                    tracing::warn!(error = %err, "caption websocket transport error, closing");
                    break;
                }
                Some(Ok(Message::Close(_))) => break,
                Some(Ok(Message::Text(text))) => {
                    if is_client_ping(text.as_str())
                        && socket
                            .send(Message::Text(r#"{"type":"pong"}"#.into()))
                            .await
                            .is_err()
                    {
                        break;
                    }
                }
                // Binary frames, pongs and anything else need no reply; axum
                // already auto-replies to inbound protocol pings.
                Some(Ok(_)) => {}
            },
        }
    }
}

/// `POST /overlay/{community}/caption/push`'s response body.
#[derive(Debug, Serialize)]
pub struct CaptionPushResponse {
    pub status: &'static str,
    pub community: String,
    pub surface: &'static str,
    /// `false` when the caption was broadcast live but could not be written
    /// to history (the database failed). Surfaced rather than hidden so the
    /// caller can tell; the failure is also logged at ERROR and counted.
    pub persisted: bool,
}

/// `POST /overlay/{community}/caption/push` -- validates one caption (see
/// `crate::overlay::render::render_caption`), broadcasts it to every
/// connected viewer of this community, and persists it for reconnect replay.
///
/// Publish happens before persist on purpose: live captions must not depend
/// on the database being up. A persist failure is therefore reported in the
/// response (`persisted: false`) instead of failing a caption viewers have
/// already seen -- a retry would show it twice.
#[tracing::instrument(
    name = "caption.ingest",
    skip_all,
    fields(community_id = credential.community_id)
)]
pub async fn push_caption(
    State(state): State<AppState>,
    Extension(credential): Extension<PushCredential>,
    Path(community): Path<String>,
    Json(body): Json<OverlayPush>,
) -> Result<Json<CaptionPushResponse>, ApiError> {
    let started = Instant::now();
    let metrics = &state.caption_metrics;

    if let Err(err) = ensure_enabled(&state).await {
        metrics.ingest_total.with_label_values(&["disabled"]).inc();
        return Err(err);
    }
    // Never trust the path segment: it must name the community the verified
    // credential was issued for.
    if community != credential.community_id.to_string() {
        return Err(ApiError::Forbidden(
            "path community does not match credential".to_string(),
        ));
    }

    let content = match render_caption(&body) {
        Ok(content) => content,
        Err(err) => {
            metrics.ingest_total.with_label_values(&["rejected"]).inc();
            tracing::warn!(error = %err, "caption push rejected");
            return Err(ApiError::BadRequest(err.to_string()));
        }
    };
    metrics
        .text_chars
        .observe(content.original.chars().count() as f64);

    let community_id = credential.community_id;
    state.hub.publish(community_id, Surface::Caption, body);

    let persisted = match state
        .caption_store
        .insert(NewCaptionEvent {
            community_id,
            user_ref: content.user,
            platform: content.platform,
            original: content.original,
            translated: content.translated,
            detected_lang: content.detected_lang,
            target_lang: content.target_lang,
            confidence: content.confidence,
        })
        .await
    {
        Ok(()) => true,
        Err(err) => {
            tracing::error!(
                error = %err,
                error_debug = ?err,
                "caption broadcast but not persisted; replay history will miss it"
            );
            false
        }
    };

    let outcome = if persisted { "ok" } else { "ok_not_persisted" };
    metrics.ingest_total.with_label_values(&[outcome]).inc();
    metrics
        .ingest_duration_seconds
        .observe(started.elapsed().as_secs_f64());
    tracing::debug!(persisted, "caption published");

    Ok(Json(CaptionPushResponse {
        status: "published",
        community,
        surface: Surface::Caption.as_str(),
        persisted,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use uuid::Uuid;

    fn content() -> CaptionContent {
        CaptionContent {
            user: Uuid::parse_str("11111111-1111-1111-1111-111111111111").unwrap(),
            display_name: "Display Name".to_string(),
            platform: "twitch".to_string(),
            original: "hola".to_string(),
            translated: Some("hello".to_string()),
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence: Some(0.9),
        }
    }

    fn stored(confidence: Option<f64>) -> StoredCaption {
        StoredCaption {
            user_ref: Some(Uuid::parse_str("11111111-1111-1111-1111-111111111111").unwrap()),
            platform: "twitch".to_string(),
            original: "hola".to_string(),
            translated: None,
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence,
            created_at: Utc.with_ymd_and_hms(2026, 10, 9, 12, 30, 0).unwrap(),
        }
    }

    #[test]
    fn live_frame_carries_the_display_name_and_a_utc_timestamp() {
        let now = Utc.with_ymd_and_hms(2026, 10, 9, 12, 0, 0).unwrap();
        let frame = CaptionWsFrame::live(&content(), now);
        assert_eq!(frame.kind, "caption");
        assert_eq!(frame.display_name.as_deref(), Some("Display Name"));
        assert_eq!(frame.timestamp, "2026-10-09T12:00:00.000Z");
    }

    #[test]
    fn frames_never_expose_the_user_uuid_platform_or_a_username() {
        let live = serde_json::to_value(CaptionWsFrame::live(&content(), Utc::now())).unwrap();
        let replayed = serde_json::to_value(CaptionWsFrame::replayed(&stored(None))).unwrap();
        for value in [live, replayed] {
            for forbidden in ["user", "user_ref", "username", "platform"] {
                assert!(value.get(forbidden).is_none(), "{forbidden} leaked");
            }
            assert_eq!(value["type"], "caption");
        }
    }

    #[test]
    fn replayed_frame_has_no_display_name_and_keeps_the_stored_timestamp() {
        let frame = CaptionWsFrame::replayed(&stored(Some(0.5)));
        assert!(frame.display_name.is_none());
        assert_eq!(frame.timestamp, "2026-10-09T12:30:00.000Z");
        assert_eq!(frame.confidence, Some(0.5));
        let json = serde_json::to_value(&frame).unwrap();
        assert!(json.get("display_name").is_none());
    }

    /// The invariant `encode_frame`'s `expect` rests on: even a non-finite
    /// float (which a database row could hold) serializes -- as `null` --
    /// instead of making the whole frame unserializable.
    #[test]
    fn non_finite_confidence_serializes_as_null() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let text = encode_frame(&CaptionWsFrame::replayed(&stored(Some(bad))));
            let json: serde_json::Value = serde_json::from_str(&text).unwrap();
            assert!(json["confidence"].is_null(), "{bad}");
        }
    }

    #[test]
    fn client_ping_recognizes_both_wire_forms_and_nothing_else() {
        assert!(is_client_ping("ping"));
        assert!(is_client_ping(r#"{"type":"ping"}"#));
        assert!(!is_client_ping("pong"));
        assert!(!is_client_ping(r#"{"type":"caption"}"#));
        assert!(!is_client_ping(r#"{"kind":"ping"}"#));
        assert!(!is_client_ping("not json"));
        assert!(!is_client_ping(""));
    }

    #[test]
    fn page_response_carries_the_security_headers() {
        let response = page_response();
        assert_eq!(response.status(), StatusCode::OK);
        let headers = response.headers();
        assert_eq!(
            headers.get(header::CONTENT_TYPE).unwrap(),
            "text/html; charset=utf-8"
        );
        assert_eq!(headers.get(header::CACHE_CONTROL).unwrap(), "no-store");
        assert_eq!(
            headers.get(header::X_CONTENT_TYPE_OPTIONS).unwrap(),
            "nosniff"
        );
        assert_eq!(headers.get(header::REFERRER_POLICY).unwrap(), "no-referrer");
        let csp = headers
            .get(header::CONTENT_SECURITY_POLICY)
            .unwrap()
            .to_str()
            .unwrap();
        assert!(csp.starts_with("default-src 'none'"));
        // The legacy route was embeddable from anywhere.
        assert!(!csp.contains("frame-ancestors"));
        assert!(headers.get(header::X_FRAME_OPTIONS).is_none());
    }

    /// The page must write every dynamic value with `textContent` only. A
    /// markup-injecting sink anywhere in the script would turn a hostile
    /// chat line into script execution in the streamer's OBS.
    #[test]
    fn page_script_uses_no_markup_injecting_sinks() {
        let code: String = CAPTION_PAGE
            .lines()
            .filter(|line| !line.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        for sink in [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            "setTimeout(\"",
            "setTimeout('",
        ] {
            assert!(!code.contains(sink), "page uses forbidden sink {sink}");
        }
        assert!(code.contains("textContent"));
    }

    #[test]
    fn page_never_logs_the_overlay_key_to_the_console() {
        // The legacy page logged its whole config -- including the key.
        assert!(!CAPTION_PAGE.contains("console.log"));
    }

    #[test]
    fn page_connects_to_the_websocket_url_the_server_serves() {
        assert!(CAPTION_PAGE.contains("/ws/captions/"));
        assert!(CAPTION_PAGE.contains("encodeURIComponent"));
        // Reads the same wire keys `CaptionWsFrame` serializes.
        for key in ["display_name", "original", "translated", "target_lang"] {
            assert!(CAPTION_PAGE.contains(key), "page never reads {key}");
        }
    }

    #[test]
    fn route_errors_map_to_the_underlying_status_codes() {
        let auth: CaptionRouteError = OverlayAuthError::InvalidKey.into();
        assert_eq!(auth.into_response().status(), StatusCode::FORBIDDEN);
        let api: CaptionRouteError = ApiError::BadRequest("x".into()).into();
        assert_eq!(api.into_response().status(), StatusCode::BAD_REQUEST);
    }
}
