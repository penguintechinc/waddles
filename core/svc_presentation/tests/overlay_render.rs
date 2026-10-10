//! End-to-end proof that a push is detokenized and HTML-escaped BEFORE it can
//! reach a browser: the real `http::router` behind the real VIEW guard and the
//! real PUSH guard (real signed JWTs against a live local JWKS), a real
//! `PresentationHub<RenderedFrame>`, and the real renderers. Only hub-api
//! (display-name resolution) and the database are faked.
//!
//! regression: svc-presentation's push route published the caller's raw
//! `OverlayPush` straight to every viewer -- an un-detokenized
//! `{user:<uuid>}`, a user UUID, or `<script>` in a bundle-authored string
//! reached the browser verbatim. Each test here posts a hostile push over HTTP
//! and asserts on the exact bytes a viewer's SSE stream receives.

mod common;

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use http_body_util::BodyExt;
use overlay_auth::generate_view_token;
use serde_json::{json, Value};
use tower::ServiceExt;

use common::*;
use svc_presentation::http::router;

const COMMUNITY: i64 = 42;
const USER_A: &str = "11111111-1111-1111-1111-111111111111";
const USER_B: &str = "22222222-2222-2222-2222-222222222222";
const WAIT: Duration = Duration::from_secs(5);

/// A viewer connected to `/{CODE_42}/{surface}/live` through the real router.
struct Viewer {
    body: Body,
}

impl Viewer {
    /// Reads SSE chunks until a complete `data: ...\n\n` event is buffered and
    /// returns that event's raw text.
    async fn next_event(&mut self) -> String {
        let mut buf = String::new();
        loop {
            let frame = tokio::time::timeout(WAIT, self.body.frame())
                .await
                .expect("an SSE chunk arrives before the timeout")
                .expect("the stream is still open")
                .expect("no body-level error");
            let data = frame.into_data().expect("a data frame, not trailers");
            buf.push_str(std::str::from_utf8(&data).expect("utf8 SSE payload"));
            if buf.contains("\n\n") {
                return buf;
            }
        }
    }

    /// The JSON payload of the next event.
    async fn next_json(&mut self) -> (String, Value) {
        let raw = self.next_event().await;
        let json_text = raw
            .lines()
            .find_map(|l| l.strip_prefix("data:"))
            .map(|d| d.trim().to_string())
            .expect("an SSE data line");
        let value: Value = serde_json::from_str(&json_text).expect("event data is JSON");
        (json_text, value)
    }

    /// `true` if nothing arrives within `window` (heartbeats are 15s apart).
    async fn is_quiet_for(&mut self, window: Duration) -> bool {
        tokio::time::timeout(window, self.body.frame())
            .await
            .is_err()
    }
}

/// A router + a connected viewer for `surface`, with the Connected frame
/// already consumed.
async fn connect_viewer(
    surface: &str,
    resolver: Arc<TableResolver>,
) -> (axum::Router, Viewer, Arc<TableResolver>) {
    let token = generate_view_token();
    let state = state_with_real_push_trust_and_view(COMMUNITY, &token, true).await;
    let state = with_overlay_fakes(state, resolver.clone(), "7");
    let app = router(state);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/{surface}/live?key={token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut viewer = Viewer {
        body: response.into_body(),
    };
    let (_, connected) = viewer.next_json().await;
    assert_eq!(connected["surface"], surface);
    // The opaque overlay code is the slug; the integer community id never
    // appears on the wire.
    assert_eq!(connected["community"], CODE_42);
    assert!(
        connected.get("content_type").is_none(),
        "the first frame is the Connected frame, not content"
    );
    (app, viewer, resolver)
}

async fn post_push(
    app: &axum::Router,
    code_in_path: &str,
    credential_community: i64,
    surface: &str,
    body: Value,
) -> (StatusCode, String) {
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/{code_in_path}/{surface}/push"))
                .header(header::CONTENT_TYPE, "application/json")
                .header(
                    header::AUTHORIZATION,
                    format!("Bearer {}", sign_push_token(credential_community)),
                )
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

fn assert_clean(wire: &str) {
    assert!(
        !wire.contains("<script"),
        "raw <script> on the wire: {wire}"
    );
    assert!(!wire.contains("<img"), "raw markup on the wire: {wire}");
    assert!(!wire.contains(USER_A), "user uuid on the wire: {wire}");
    assert!(!wire.contains(USER_B), "user uuid on the wire: {wire}");
    assert!(!wire.contains("{user:"), "raw token on the wire: {wire}");
    // Escaped text such as `&lt;img src=x onerror=alert(1)&gt;` is inert; the
    // raw `<img` check above is what proves no tag survived.
}

/// Negative control: the leak assertions can fail. A gate that cannot fail
/// proves nothing, so pin that `assert_clean` rejects each leak shape.
#[test]
fn assert_clean_rejects_every_leak_shape() {
    for leaked in [
        r#"{"text":"<script>alert(1)</script>"}"#,
        r#"{"text":"<img src=x>"}"#,
        r#"{"user":"11111111-1111-1111-1111-111111111111"}"#,
        r#"{"user":"22222222-2222-2222-2222-222222222222"}"#,
        r#"{"text":"{user:abc}"}"#,
    ] {
        let result = std::panic::catch_unwind(|| assert_clean(leaked));
        assert!(result.is_err(), "assert_clean accepted a leak: {leaked}");
    }
    assert_clean(r#"{"text":"&lt;script&gt;alert(1)&lt;/script&gt; Unknown User"}"#);
}

fn resolver() -> Arc<TableResolver> {
    Arc::new(TableResolver::with(&[
        (USER_A, "Al<i>ce"),
        (USER_B, "Bob \"the\" <b>"),
    ]))
}

#[tokio::test]
async fn a_hostile_chat_push_reaches_the_viewer_detokenized_and_escaped() {
    let (app, mut viewer, resolver) = connect_viewer("chat", resolver()).await;

    let (status, body) = post_push(
        &app,
        CODE_42,
        COMMUNITY,
        "chat",
        json!({"chat_message": {
            "user": USER_B,
            "display_name": "<img src=x onerror=alert(1)>",
            "platform": "twitch",
            "text": format!("<script>alert(1)</script> hi {{user:{USER_A}}}"),
        }}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");

    let (wire, frame) = viewer.next_json().await;
    assert_clean(&wire);
    assert_eq!(frame["content_type"], "chat");
    assert_eq!(frame["surface"], "chat");
    // hub-api's name for the author, escaped; the caller's display_name is gone.
    assert_eq!(frame["display_name"], "Bob &quot;the&quot; &lt;b&gt;");
    assert!(
        frame["text"]
            .as_str()
            .unwrap()
            .contains("&lt;script&gt;alert(1)&lt;/script&gt; hi Al&lt;i&gt;ce"),
        "{wire}"
    );
    assert!(frame["theme"]["primary_color"].is_string(), "{wire}");
    // Resolution was scoped to the community's own tenant, batched once.
    let tenants = resolver.tenants.lock().unwrap();
    assert_eq!(tenants.as_slice(), ["7"]);
}

#[tokio::test]
async fn a_hostile_alert_push_reaches_the_viewer_detokenized_and_escaped() {
    let (app, mut viewer, _) = connect_viewer("alert_box", resolver()).await;

    let (status, body) = post_push(
        &app,
        CODE_42,
        COMMUNITY,
        "alert_box",
        json!({"alert": {
            "alert_type": "sub",
            "user": USER_A,
            "display_name": "<script>alert(1)</script>",
            "amount": {"note": format!("<script>x</script>{{user:{USER_B}}}"), "tier": 3},
            "message": format!("thanks {{user:{USER_B}}} <img src=x onerror=alert(1)>"),
        }}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");

    let (wire, frame) = viewer.next_json().await;
    assert_clean(&wire);
    assert_eq!(frame["content_type"], "alert_box");
    assert_eq!(frame["display_name"], "Al&lt;i&gt;ce");
    assert_eq!(frame["amount"]["tier"], 3);
}

#[tokio::test]
async fn every_text_surface_streams_only_sanitized_frames() {
    let evil = format!("<script>alert(1)</script>{{user:{USER_A}}}");
    let cases: Vec<(&str, Value)> = vec![
        ("ticker", json!({"text": evil})),
        ("crawler", json!({"text": evil})),
        (
            "full_screen",
            json!({"title": evil, "body": evil, "image_url": "https://example.com/a.png"}),
        ),
        ("media", json!({"title": evil, "body": evil})),
        (
            "goals",
            json!({"goal": {"label": evil, "current": 1.0, "target": 10.0, "unit": evil}}),
        ),
    ];
    for (surface, push) in cases {
        let (app, mut viewer, _) = connect_viewer(surface, resolver()).await;
        let (status, body) = post_push(&app, CODE_42, COMMUNITY, surface, push).await;
        assert_eq!(status, StatusCode::OK, "{surface}: {body}");
        let (wire, frame) = viewer.next_json().await;
        assert_clean(&wire);
        assert_eq!(frame["content_type"], surface, "{wire}");
        assert!(
            wire.contains("&lt;script&gt;alert(1)&lt;/script&gt;Al&lt;i&gt;ce"),
            "{surface}: token not resolved+escaped: {wire}"
        );
    }
}

/// hub-api down: the push is NOT dropped, and still nothing raw leaks.
#[tokio::test]
async fn an_unresolvable_user_streams_the_neutral_label_never_the_token() {
    let (app, mut viewer, _) = connect_viewer("chat", Arc::new(TableResolver::failing())).await;
    let (status, body) = post_push(
        &app,
        CODE_42,
        COMMUNITY,
        "chat",
        json!({"chat_message": {
            "user": USER_B,
            "display_name": "spoofed",
            "platform": "twitch",
            "text": format!("<script>alert(1)</script> {{user:{USER_A}}}"),
        }}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (wire, frame) = viewer.next_json().await;
    assert_clean(&wire);
    assert_eq!(frame["display_name"], "Unknown User", "{wire}");
    assert!(
        !wire.contains("spoofed"),
        "caller display_name trusted: {wire}"
    );
}

#[tokio::test]
async fn a_rejected_push_is_a_400_and_streams_nothing() {
    let (app, mut viewer, _) = connect_viewer("media", resolver()).await;
    // A `javascript:` image_url is refused by the renderer.
    let (status, body) = post_push(
        &app,
        CODE_42,
        COMMUNITY,
        "media",
        json!({"image_url": "javascript:alert(1)"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert!(
        !body.contains("javascript:"),
        "the rejection must not echo push content: {body}"
    );
    assert!(viewer.is_quiet_for(Duration::from_millis(150)).await);
}

#[tokio::test]
async fn a_push_only_reaches_viewers_of_its_own_surface() {
    let (app, mut chat_viewer, _) = connect_viewer("chat", resolver()).await;
    let (status, _) = post_push(
        &app,
        CODE_42,
        COMMUNITY,
        "ticker",
        json!({"text": "not for chat"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(chat_viewer.is_quiet_for(Duration::from_millis(150)).await);
}

#[tokio::test]
async fn a_credential_for_another_community_cannot_push_here() {
    let (app, mut viewer, _) = connect_viewer("chat", resolver()).await;
    // The code names community 42 but the verified credential is for 99.
    let (status, _) = post_push(
        &app,
        CODE_42,
        99,
        "chat",
        json!({"chat_message": {"user": USER_A, "display_name": "x", "platform": "twitch", "text": "hi"}}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(viewer.is_quiet_for(Duration::from_millis(150)).await);
}

#[tokio::test]
async fn a_push_without_a_bearer_is_401_and_streams_nothing() {
    let (app, mut viewer, _) = connect_viewer("ticker", resolver()).await;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/{CODE_42}/ticker/push"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(json!({"text": "x"}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert!(viewer.is_quiet_for(Duration::from_millis(150)).await);
}

// -- the browser page ---------------------------------------------------

async fn get_page(surface: &str, key_ok: bool) -> (StatusCode, axum::http::HeaderMap, String) {
    let token = generate_view_token();
    let state = state_with_real_push_trust_and_view(COMMUNITY, &token, true).await;
    let used = if key_ok { token.as_str() } else { "wrong-key" };
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri(format!("/{CODE_42}/{surface}?key={used}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        headers,
        String::from_utf8_lossy(&bytes).into_owned(),
    )
}

#[tokio::test]
async fn the_browser_page_is_served_behind_the_view_key_for_each_page_surface() {
    for surface in [
        "alert_box",
        "chat",
        "goals",
        "ticker",
        "crawler",
        "full_screen",
        "media",
    ] {
        let (status, headers, body) = get_page(surface, true).await;
        assert_eq!(status, StatusCode::OK, "{surface}");
        assert_eq!(
            headers.get(header::CONTENT_TYPE).unwrap(),
            "text/html; charset=utf-8"
        );
        let csp = headers
            .get(header::CONTENT_SECURITY_POLICY)
            .expect("CSP header")
            .to_str()
            .unwrap();
        assert!(csp.contains("script-src 'sha256-"), "{csp}");
        assert!(
            !csp.contains("'unsafe-inline'; style") || !csp.contains("script-src 'unsafe-inline'")
        );
        assert_eq!(headers.get(header::CACHE_CONTROL).unwrap(), "no-store");
        assert!(body.contains("new EventSource("), "{surface}");
    }
}

#[tokio::test]
async fn the_browser_page_rejects_a_wrong_key() {
    let (status, _, body) = get_page("chat", false).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(!body.contains("EventSource"), "the page must not be served");
}

#[tokio::test]
async fn surfaces_without_a_page_are_404_even_with_a_valid_key() {
    for surface in ["music", "image", "caption"] {
        let (status, _, body) = get_page(surface, true).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{surface}: {body}");
    }
}
