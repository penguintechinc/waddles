//! Real-path beta slice: the real router and guards served on a genuinely bound TCP port,
//! driven by a real HTTP client (`reqwest`) exactly as an OBS browser source and svc-action
//! would -- no `tower::oneshot`. A signed PUSH JWT (verified against a live local JWKS, the
//! `hub-api` stand-in) posts an alert; a connected browser-page SSE client must receive the
//! rendered, detokenized, HTML-escaped frame; a viewer of ANOTHER community's overlay code must
//! receive nothing. Only hub-api's display-name resolution and the database are faked (they need
//! Postgres / a live hub-api); everything between the wire and the viewer is production code.
//!
//! Counts are asserted (frames received, viewers reached) -- never a bare "no errors".

mod common;

use std::sync::Arc;
use std::time::Duration;

use overlay_auth::{generate_view_token, hash_token};
use sea_orm::{DatabaseBackend, MockDatabase};
use serde_json::{json, Value};

use common::*;
use svc_presentation::db::entities::overlay_view_credential::Model as ViewCredentialModel;
use svc_presentation::flags::{boxed, StaticFlag};
use svc_presentation::http::{router, AppState};

const USER_A: &str = "11111111-1111-1111-1111-111111111111";
const WAIT: Duration = Duration::from_secs(5);
const QUIET: Duration = Duration::from_millis(700);

fn credential(id: i64, community_id: i64, token: &str) -> ViewCredentialModel {
    ViewCredentialModel {
        id,
        community_id,
        key_hash: hash_token(token),
        previous_key_hash: None,
        is_active: true,
        rotated_at: None,
    }
}

/// A browser-page SSE client over a real HTTP connection.
struct Sse {
    response: reqwest::Response,
    buf: String,
}

impl Sse {
    async fn connect(base: &str, code: &str, surface: &str, key: &str) -> Self {
        let response = reqwest::Client::new()
            .get(format!("{base}/{code}/{surface}/live?key={key}"))
            .send()
            .await
            .expect("SSE connect");
        assert_eq!(response.status(), 200);
        Self {
            response,
            buf: String::new(),
        }
    }

    /// Next complete `data:` event as `(raw JSON text, parsed)`.
    async fn next_event(&mut self) -> (String, Value) {
        loop {
            if let Some(end) = self.buf.find("\n\n") {
                let raw: String = self.buf.drain(..end + 2).collect();
                if let Some(data) = raw.lines().find_map(|l| l.strip_prefix("data:")) {
                    let text = data.trim().to_string();
                    let value = serde_json::from_str(&text).expect("event data is JSON");
                    return (text, value);
                }
                continue; // comment / heartbeat
            }
            let chunk = tokio::time::timeout(WAIT, self.response.chunk())
                .await
                .expect("an SSE chunk arrives before the timeout")
                .expect("no transport error")
                .expect("the stream is still open");
            self.buf
                .push_str(std::str::from_utf8(&chunk).expect("utf8"));
        }
    }

    /// Number of further data events received within `window`.
    async fn count_events(&mut self, window: Duration) -> usize {
        let mut n = 0;
        let deadline = tokio::time::Instant::now() + window;
        while tokio::time::timeout_at(deadline, self.next_event())
            .await
            .is_ok()
        {
            n += 1;
        }
        n
    }
}

struct Harness {
    base: String,
    key_42: String,
    key_43: String,
}

async fn start() -> Harness {
    let key_42 = generate_view_token();
    let key_43 = generate_view_token();
    let jwks_url = spawn_jwks_server().await;
    // VIEW lookups answer in connection order: the page GET, then each SSE viewer.
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![credential(1, 42, &key_42)]]) // page GET
        .append_query_results([vec![credential(1, 42, &key_42)]]) // viewer 42
        .append_query_results([vec![credential(2, 43, &key_43)]]) // viewer 43
        .into_connection();
    let mut state = AppState::new(
        config_with(&["--push-jwks-url", &jwks_url]),
        prometheus::Registry::new(),
        db,
    );
    state.captions_flag = boxed(StaticFlag(true));
    state.overlay_codes = test_overlay_codes();
    let resolver = Arc::new(TableResolver::with(&[(USER_A, "Al<i>ce")]));
    let state = with_overlay_fakes(state, resolver, "7");
    let addr = serve(router(state)).await;
    Harness {
        base: format!("http://{addr}"),
        key_42,
        key_43,
    }
}

async fn push(
    base: &str,
    code: &str,
    jwt_community: i64,
    surface: &str,
    body: Value,
) -> (u16, String) {
    let response = reqwest::Client::new()
        .post(format!("{base}/{code}/{surface}/push"))
        .bearer_auth(sign_push_token(jwt_community))
        .json(&body)
        .send()
        .await
        .expect("push request");
    let status = response.status().as_u16();
    (status, response.text().await.expect("push body"))
}

#[tokio::test]
async fn a_signed_alert_push_reaches_the_connected_browser_page_rendered_and_escaped() {
    let h = start().await;

    // The OBS browser-source page itself is served (real HTML over the real port).
    let page = reqwest::Client::new()
        .get(format!("{}/{}/alert_box?key={}", h.base, CODE_42, h.key_42))
        .send()
        .await
        .expect("page GET");
    assert_eq!(page.status(), 200);
    let content_type = page.headers()["content-type"]
        .to_str()
        .expect("ct")
        .to_string();
    assert!(content_type.starts_with("text/html"), "{content_type}");
    let html = page.text().await.expect("page body");
    assert!(html.contains("EventSource"), "the page streams via SSE");
    assert!(
        !html.contains(CODE_42) && !html.contains(&h.key_42),
        "static page embeds no secrets"
    );

    // Two viewers: the pushing community's and another community's.
    let mut viewer = Sse::connect(&h.base, CODE_42, "alert_box", &h.key_42).await;
    let mut other = Sse::connect(&h.base, CODE_43, "alert_box", &h.key_43).await;
    let (_, connected) = viewer.next_event().await;
    assert_eq!(connected["community"], CODE_42);
    let (_, connected_other) = other.next_event().await;
    assert_eq!(connected_other["community"], CODE_43);

    // The signed PUSH JWT for community 42 posts an alert with hostile + tokenized fields.
    let (status, body) = push(
        &h.base,
        CODE_42,
        42,
        "alert_box",
        json!({"alert": {
            "alert_type": "sub",
            "user": USER_A,
            "display_name": "<script>alert(1)</script>",
            "amount": {"tier": 3},
            "message": format!("thanks {{user:{USER_A}}} <img src=x onerror=alert(1)>"),
        }}),
    )
    .await;
    assert_eq!(status, 200, "{body}");

    let (wire, frame) = viewer.next_event().await;
    assert_eq!(frame["content_type"], "alert_box");
    assert_eq!(
        frame["display_name"], "Al&lt;i&gt;ce",
        "detokenized and escaped"
    );
    assert_eq!(frame["amount"]["tier"], 3);
    assert!(
        frame["message"]
            .as_str()
            .expect("message")
            .contains("Al&lt;i&gt;ce"),
        "{{user:}} in the message resolved to the escaped name: {wire}"
    );
    for leak in ["<script", "<img", USER_A, "{user:"] {
        assert!(!wire.contains(leak), "{leak} leaked to the browser: {wire}");
    }
    // Counts: the pushing viewer got exactly that one frame (no duplicate follows) and the
    // other community's viewer got zero.
    assert_eq!(
        viewer.count_events(QUIET).await,
        0,
        "no second frame for the pushing viewer"
    );
    assert_eq!(
        other.count_events(QUIET).await,
        0,
        "another community's viewer received a frame"
    );
}

#[tokio::test]
async fn a_push_through_another_communitys_code_or_credential_is_refused_and_streams_nothing() {
    let h = start().await;
    let mut viewer = Sse::connect(&h.base, CODE_42, "alert_box", &h.key_42).await;
    // VIEW lookups answer in queue order; the first queued row (community 42) serves this viewer.
    let _ = viewer.next_event().await;

    // JWT scoped to community 43 pushed at community 42's code -> 403, nothing delivered.
    let (status, _) = push(
        &h.base,
        CODE_42,
        43,
        "alert_box",
        json!({"alert": {"alert_type": "follow", "display_name": "x"}}),
    )
    .await;
    assert!(
        status == 401 || status == 403,
        "cross-community push got {status}"
    );
    assert_eq!(
        viewer.count_events(QUIET).await,
        0,
        "a refused push reached a viewer"
    );

    // Unknown overlay code -> 404, never a guess at a community.
    let (status, _) = push(
        &h.base,
        CODE_UNKNOWN,
        42,
        "alert_box",
        json!({"alert": {"alert_type": "follow", "display_name": "x"}}),
    )
    .await;
    assert_eq!(status, 404);

    // The legacy integer-id route the old adapter used is not served.
    let (status, _) = push(&h.base, "overlay/42", 42, "media", json!({"title": "x"})).await;
    assert_eq!(
        status, 404,
        "retired /overlay/{{id}}/media/push must stay dead"
    );
}
