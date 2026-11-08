//! Real-path proof of the bundle -> presentation surface path: the real stage-side host
//! (`bundle_host_surface::SurfaceHost`) publishes over a genuinely bound TCP port to the real
//! router + guards (PUSH JWT verified against a live local JWKS, the hub-api stand-in), and real
//! SSE viewers (reqwest, VIEW key through the real guard) receive, replay and are isolated.
//! Only the database (view-credential rows) and the community/hub-api lookups are faked.
//!
//! Counts are asserted -- frames received, replays, viewers reached -- never a bare "no errors".

mod common;

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use bundle_host_surface::{PushTokenSource, SurfaceError, SurfaceHost, SurfacePolicy};
use overlay_auth::{generate_view_token, hash_token};
use sea_orm::{DatabaseBackend, MockDatabase};
use serde_json::{json, Value};

use common::*;
use svc_presentation::db::entities::overlay_view_credential::Model as ViewCredentialModel;
use svc_presentation::http::{router, AppState};

const WAIT: Duration = Duration::from_secs(5);
const QUIET: Duration = Duration::from_millis(600);
const ALL_PERMS: [&str; 5] = [
    "overlay.surface.music.now_playing",
    "overlay.surface.music.queue",
    "overlay.surface.live.channels",
    "overlay.surface.clip.alert",
    "overlay.surface.alert",
];

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

/// Mints real signed PUSH JWTs for whichever community the host asks for.
struct SignedTokens;

#[async_trait]
impl PushTokenSource for SignedTokens {
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        Ok(Arc::from(sign_push_token(community_id)))
    }
}

struct Sse {
    response: reqwest::Response,
    buf: String,
}

impl Sse {
    async fn connect(url: &str) -> Self {
        let response = reqwest::Client::new()
            .get(url)
            .send()
            .await
            .expect("SSE connect");
        assert_eq!(response.status(), 200, "{url}");
        Self {
            response,
            buf: String::new(),
        }
    }

    async fn next_event(&mut self) -> Value {
        loop {
            if let Some(end) = self.buf.find("\n\n") {
                let raw: String = self.buf.drain(..end + 2).collect();
                if let Some(data) = raw.lines().find_map(|l| l.strip_prefix("data:")) {
                    return serde_json::from_str(data.trim()).expect("event data is JSON");
                }
                continue;
            }
            let chunk = tokio::time::timeout(WAIT, self.response.chunk())
                .await
                .expect("an SSE chunk arrives before the timeout")
                .expect("no transport error")
                .expect("stream still open");
            self.buf
                .push_str(std::str::from_utf8(&chunk).expect("utf8"));
        }
    }

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
    host: SurfaceHost,
    policy: SurfacePolicy,
    key_42: String,
    key_43: String,
}

/// `lookups` = VIEW-credential rows served in connection order (one per guarded GET).
async fn start(lookups: Vec<ViewCredentialModel>) -> Harness {
    let jwks_url = spawn_jwks_server().await;
    let mut db = MockDatabase::new(DatabaseBackend::Postgres);
    for row in lookups.clone() {
        db = db.append_query_results([vec![row]]);
    }
    let state = AppState::new(
        config_with(&["--push-jwks-url", &jwks_url]),
        prometheus::Registry::new(),
        db.into_connection(),
    );
    let mut state = state;
    state.overlay_codes = test_overlay_codes();
    let state = with_overlay_fakes(state, Arc::new(TableResolver::with(&[])), "7");
    let addr = serve(router(state)).await;
    let base = format!("http://{addr}");
    Harness {
        host: SurfaceHost::new(base.clone(), Arc::new(SignedTokens)).expect("host"),
        policy: SurfacePolicy::from_permissions(ALL_PERMS),
        base,
        key_42: String::new(),
        key_43: String::new(),
    }
}

fn keys() -> (String, String) {
    (generate_view_token(), generate_view_token())
}

fn stream_url(base: &str, code: &str, surfaces: &str, key: &str) -> String {
    format!("{base}/surfaces/{code}/live?surfaces={surfaces}&key={key}")
}

fn track(id: u64, title: &str) -> Value {
    json!({"id": id, "provider": "yt", "track_id": format!("t{id}"), "title": title,
           "artist": "Artist", "duration_s": 180, "url": "https://example.com/t"})
}

#[tokio::test]
async fn a_bundle_publish_reaches_the_connected_overlay_and_not_another_community() {
    let (k42, k43) = keys();
    let mut h = start(vec![credential(1, 42, &k42), credential(2, 43, &k43)]).await;
    (h.key_42, h.key_43) = (k42, k43);
    let mut viewer = Sse::connect(&stream_url(
        &h.base,
        CODE_42,
        "music.now_playing",
        &h.key_42,
    ))
    .await;
    let mut other = Sse::connect(&stream_url(
        &h.base,
        CODE_43,
        "music.now_playing",
        &h.key_43,
    ))
    .await;
    assert_eq!(viewer.next_event().await["connected"], true);
    assert_eq!(other.next_event().await["connected"], true);

    let payload = json!({"now_playing": track(1, "Song <b>One</b>"), "updated_ms": 10});
    h.host
        .publish(&h.policy, 42, "music.now_playing", &payload.to_string())
        .await
        .expect("publish accepted");

    let frame = viewer.next_event().await;
    assert_eq!(frame["surface"], "music.now_playing");
    assert_eq!(frame["replay"], false);
    // Plain text, unmodified: consumers render as text (the page uses textContent).
    assert_eq!(frame["payload"]["now_playing"]["title"], "Song <b>One</b>");
    assert_eq!(viewer.count_events(QUIET).await, 0, "no duplicate frame");
    assert_eq!(
        other.count_events(QUIET).await,
        0,
        "another community received a frame"
    );
}

#[tokio::test]
async fn a_freshly_connected_overlay_gets_the_last_state_replayed_but_never_events() {
    let (k42, k43) = keys();
    let mut h = start(vec![credential(1, 42, &k42), credential(2, 43, &k43)]).await;
    (h.key_42, h.key_43) = (k42, k43);

    // Published BEFORE anyone is connected.
    for (surface, payload) in [
        (
            "music.now_playing",
            json!({"now_playing": track(1, "Old"), "updated_ms": 1}),
        ),
        (
            "music.now_playing",
            json!({"now_playing": track(2, "Latest"), "updated_ms": 2}),
        ),
        (
            "music.queue",
            json!({"queue": [track(3, "Next")], "updated_ms": 2}),
        ),
        (
            "clip.alert",
            json!({"clip_id": "Clip_1", "url": "https://clips.twitch.tv/Clip_1", "title": "wow", "created_ms": 5}),
        ),
    ] {
        h.host
            .publish(&h.policy, 42, surface, &payload.to_string())
            .await
            .expect("publish");
    }

    let url = stream_url(
        &h.base,
        CODE_42,
        "music.now_playing,music.queue,clip.alert",
        &h.key_42,
    );
    let mut late = Sse::connect(&url).await;
    assert_eq!(late.next_event().await["connected"], true);
    let mut replayed = [late.next_event().await, late.next_event().await];
    replayed.sort_by_key(|f| f["surface"].as_str().unwrap_or_default().to_string());
    assert_eq!(replayed[0]["surface"], "music.now_playing");
    assert_eq!(replayed[0]["replay"], true);
    assert_eq!(
        replayed[0]["payload"]["now_playing"]["title"], "Latest",
        "last state only"
    );
    assert_eq!(replayed[1]["surface"], "music.queue");
    assert_eq!(replayed[1]["replay"], true);
    assert_eq!(
        late.count_events(QUIET).await,
        0,
        "exactly 2 replay frames: clip.alert is an event and is never replayed"
    );

    // Another community connecting sees no replay of community 42's state.
    let mut stranger = Sse::connect(&stream_url(
        &h.base,
        CODE_43,
        "music.now_playing,music.queue",
        &h.key_43,
    ))
    .await;
    assert_eq!(stranger.next_event().await["connected"], true);
    assert_eq!(
        stranger.count_events(QUIET).await,
        0,
        "replay leaked across communities"
    );

    // Live frames keep flowing after the replay, with a newer seq.
    h.host
        .publish(
            &h.policy,
            42,
            "music.now_playing",
            &json!({"now_playing": null, "updated_ms": 3}).to_string(),
        )
        .await
        .expect("publish");
    let live = late.next_event().await;
    assert_eq!(live["replay"], false);
    assert!(live["seq"].as_u64() > replayed[0]["seq"].as_u64());
}

#[tokio::test]
async fn schema_violations_ungranted_surfaces_and_rate_limits_are_refused_loudly() {
    let (k42, _) = keys();
    let mut h = start(vec![credential(1, 42, &k42)]).await;
    h.key_42 = k42;
    let mut viewer = Sse::connect(&stream_url(
        &h.base,
        CODE_42,
        "music.queue,clip.alert",
        &h.key_42,
    ))
    .await;
    assert_eq!(viewer.next_event().await["connected"], true);

    // Schema violation: extra field + wrong type -> Invalid carrying locations, not values.
    let err = h
        .host
        .publish(
            &h.policy,
            42,
            "music.queue",
            &json!({"queue": "SECRET-VALUE", "updated_ms": 1, "html": "<script>"}).to_string(),
        )
        .await
        .expect_err("violation must be refused");
    assert_eq!(err.code(), "invalid");
    let msg = err.to_string();
    assert!(
        msg.contains("/queue") || msg.contains("additionalProperties"),
        "{msg}"
    );
    assert!(
        !msg.contains("SECRET-VALUE") && !msg.contains("<script>"),
        "{msg}"
    );

    // javascript: and http: URLs are refused by the https-only rule.
    for url in ["javascript:alert(1)", "http://insecure.example/c"] {
        let err = h
            .host
            .publish(
                &h.policy,
                42,
                "clip.alert",
                &json!({"clip_id": "abc", "url": url, "title": "t", "created_ms": 1}).to_string(),
            )
            .await
            .expect_err("non-https url refused");
        assert_eq!(err.code(), "invalid", "{url}");
    }

    // Ungranted surface never leaves the host.
    let narrow = SurfacePolicy::from_permissions(["overlay.surface.alert"]);
    let err = h
        .host
        .publish(&narrow, 42, "music.queue", r#"{"queue":[],"updated_ms":1}"#)
        .await
        .expect_err("ungranted");
    assert!(matches!(err, SurfaceError::NotGranted(_)));

    // Rate limit: clip.alert allows a burst of 5 per community, then 429 with a delay.
    let ok = json!({"clip_id": "abc", "url": "https://clips.twitch.tv/abc", "title": "t", "created_ms": 1}).to_string();
    let mut accepted = 0;
    let mut limited = None;
    for _ in 0..8 {
        match h.host.publish(&h.policy, 42, "clip.alert", &ok).await {
            Ok(()) => accepted += 1,
            Err(e) => {
                limited = Some(e);
                break;
            }
        }
    }
    // Burst is 5; the two refused non-https attempts above already spent 2 tokens (rejected
    // publishes count against the limit, so a hostile bundle cannot probe for free).
    assert_eq!(accepted, 3, "burst 5 minus the 2 refused attempts");
    assert!(
        matches!(limited, Some(SurfaceError::RateLimited { retry_after_ms }) if retry_after_ms >= 1000)
    );

    // Viewer received exactly the 3 accepted clip frames and nothing invalid.
    assert_eq!(viewer.count_events(QUIET).await, 3);
}

#[tokio::test]
async fn publish_route_demands_a_push_jwt_scoped_to_the_path_community() {
    let (k42, _) = keys();
    let mut h = start(vec![]).await;
    h.key_42 = k42;
    let client = reqwest::Client::new();
    let url = format!("{}/internal/surfaces/42/alert/publish", h.base);
    let body = json!({"alert_type": "info", "message": "m"});

    let r = client.post(&url).json(&body).send().await.expect("req");
    assert_eq!(r.status(), 401, "no bearer");
    let r = client
        .post(&url)
        .bearer_auth(sign_push_token(43))
        .json(&body)
        .send()
        .await
        .expect("req");
    assert!(
        r.status() == 401 || r.status() == 403,
        "JWT for community 43 on community 42's path got {}",
        r.status()
    );
    let ok = client
        .post(&url)
        .bearer_auth(sign_push_token(42))
        .json(&body)
        .send()
        .await
        .expect("req");
    assert_eq!(ok.status(), 200);
    let unknown = client
        .post(format!(
            "{}/internal/surfaces/42/not.a.surface/publish",
            h.base
        ))
        .bearer_auth(sign_push_token(42))
        .json(&body)
        .send()
        .await
        .expect("req");
    assert_eq!(unknown.status(), 404);
    let huge = client
        .post(format!("{}/internal/surfaces/42/alert/publish", h.base))
        .bearer_auth(sign_push_token(42))
        .body(vec![b' '; 40_000])
        .send()
        .await
        .expect("req");
    assert_eq!(huge.status(), 413);
}

#[tokio::test]
async fn the_view_routes_are_read_only_key_gated_and_the_page_is_static() {
    let (k42, _) = keys();
    let wrong = generate_view_token();
    let mut h = start(vec![
        credential(1, 42, &k42),
        credential(1, 42, &k42),
        credential(1, 42, &k42),
        credential(1, 42, &k42),
    ])
    .await;
    h.key_42 = k42;
    let client = reqwest::Client::new();

    // Wrong key -> refused, no stream.
    let r = client
        .get(stream_url(&h.base, CODE_42, "alert", &wrong))
        .send()
        .await
        .expect("req");
    assert_eq!(r.status(), 403);
    // Unknown surface and an empty list are refused.
    let r = client
        .get(stream_url(&h.base, CODE_42, "nope", &h.key_42))
        .send()
        .await
        .expect("req");
    assert_eq!(r.status(), 404);
    // VIEW key cannot publish (publish needs a PUSH JWT, a VIEW key is not one).
    let r = client
        .post(format!(
            "{}/internal/surfaces/42/alert/publish?key={}",
            h.base, h.key_42
        ))
        .json(&json!({"alert_type": "info", "message": "m"}))
        .send()
        .await
        .expect("req");
    assert_eq!(r.status(), 401);
    // The page: static, CSP-hashed, embeds no secret or code.
    let page = client
        .get(format!(
            "{}/surfaces/{CODE_42}/view?surfaces=music.now_playing&key={}",
            h.base, h.key_42
        ))
        .send()
        .await
        .expect("req");
    assert_eq!(page.status(), 200);
    let csp = page.headers()["content-security-policy"]
        .to_str()
        .expect("csp")
        .to_string();
    assert!(csp.contains("script-src 'sha256-") && !csp.contains("unsafe-eval"));
    let html = page.text().await.expect("body");
    assert!(html.contains("EventSource") && html.contains("textContent"));
    assert!(!html.contains(CODE_42) && !html.contains(&h.key_42));
}

#[tokio::test]
async fn an_alert_surface_publish_also_reaches_the_alert_box_overlay_escaped_and_pii_free() {
    let (k42, _) = keys();
    let mut h = start(vec![credential(1, 42, &k42), credential(1, 42, &k42)]).await;
    h.key_42 = k42;
    let mut alert_box = Sse::connect(&format!(
        "{}/{CODE_42}/alert_box/live?key={}",
        h.base, h.key_42
    ))
    .await;
    let mut surface = Sse::connect(&stream_url(&h.base, CODE_42, "alert", &h.key_42)).await;
    let _ = alert_box.next_event().await;
    assert_eq!(surface.next_event().await["connected"], true);

    h.host
        .publish(
            &h.policy,
            42,
            "alert",
            &json!({"alert_type": "follow",
                    "message": "hi <img src=x onerror=alert(1)> {user:11111111-1111-1111-1111-111111111111}"})
                .to_string(),
        )
        .await
        .expect("publish");

    let rendered = alert_box.next_event().await;
    assert_eq!(rendered["content_type"], "alert_box");
    let wire = rendered.to_string();
    assert!(
        wire.contains("&lt;img"),
        "alert_box output is HTML-escaped: {wire}"
    );
    assert!(
        !wire.contains("<img") && !wire.contains("11111111-1111"),
        "{wire}"
    );

    let frame = surface.next_event().await;
    let message = frame["payload"]["message"].as_str().expect("message");
    assert!(
        message.contains("Unknown User"),
        "user token neutralised: {message}"
    );
    assert!(!message.contains("11111111-1111"), "{message}");
}
