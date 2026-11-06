//! Behavior of the profile-derived YouTube `PollConfig` through the real
//! `PollTransport` against a local fake YouTube API (no wasm): page token and
//! interval hand-off, quota back-off, park-on-403, and the credential layer's
//! retryable refusal of an about-to-expire token.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, Mutex};
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionRef, SecretString};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth, TransportSecretSource};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{FrameOutcome, PollConfig, PollTransport, Transport, TransportError};
use svc_ingest::connector_runtime::feed::TransportPlan;
use svc_ingest::connector_runtime::plan::{OverlayProfile, RuntimeEnv};
use svc_ingest::connector_runtime::profiles::youtube::YoutubeProfile;
use wiremock::matchers::{header, method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

const CHAT: &str = "Cg0KC-abc_123";
const TOKEN: &str = "ya29.test-token";

#[derive(Clone)]
enum Mode {
    Token,
    Unavailable,
    Missing,
}

/// A credential source standing in for hub-api's resolver; records the
/// connection each resolution asked for.
struct FakeSecrets {
    mode: Mode,
    asked: Arc<Mutex<Vec<ConnectionRef>>>,
}

impl FakeSecrets {
    fn new(mode: Mode) -> (Self, Arc<Mutex<Vec<ConnectionRef>>>) {
        let asked = Arc::new(Mutex::new(Vec::new()));
        (
            Self {
                mode,
                asked: Arc::clone(&asked),
            },
            asked,
        )
    }
}

impl TransportSecretSource for FakeSecrets {
    fn resolve(
        &self,
        requested: ConnectionRef,
        _dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        self.asked.lock().unwrap().push(requested);
        let mode = self.mode.clone();
        Box::pin(async move {
            match mode {
                Mode::Token => Ok(ResolvedAuth {
                    secret: SecretString::new(TOKEN.to_owned()),
                    header: None,
                }),
                Mode::Unavailable => Err(SecretError::Unavailable),
                Mode::Missing => Err(SecretError::Missing),
            }
        })
    }
}

fn binding(chat: &str) -> BindingSummary {
    BindingSummary {
        community_id: 7,
        connection_id: 12,
        provider: "youtube".to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata: [("live_chat_id", chat), ("tenant_id", "3")]
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

/// The profile's config, with ONLY the test-permitted overrides applied
/// (loopback http to the fake API); everything else is the host constants.
fn derived(server: &MockServer, chat: &str) -> PollConfig {
    let env = RuntimeEnv {
        environment: "test".to_owned(),
        max_frame_bytes: 262_144,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        heartbeat_enabled: false,
    };
    let mut plans = YoutubeProfile::new().plan(&binding(chat), &env).unwrap();
    assert_eq!(plans.len(), 1);
    let TransportPlan::Poll(mut cfg) = plans.remove(0).plan else {
        panic!("poll plan expected");
    };
    let port = reqwest::Url::parse(&server.uri())
        .unwrap()
        .port()
        .expect("mock port");
    let mut url = reqwest::Url::parse(&cfg.url).unwrap();
    url.set_scheme("http").unwrap();
    url.set_host(Some("127.0.0.1")).unwrap();
    url.set_port(Some(port)).unwrap();
    cfg.url = url.into();
    cfg.allow_loopback_insecure = true;
    *cfg
}

fn err_body(reason: &str) -> serde_json::Value {
    serde_json::json!({"error":{"code":403,"errors":[{"reason":reason}]}})
}

async fn mount_403(server: &MockServer, reason: &str, retry_after: Option<&str>) {
    let mut t = ResponseTemplate::new(403).set_body_json(err_body(reason));
    if let Some(v) = retry_after {
        t = t.insert_header("Retry-After", v);
    }
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/messages"))
        .respond_with(t)
        .mount(server)
        .await;
}

#[tokio::test]
async fn page_token_and_server_interval_drive_the_next_poll() {
    let server = MockServer::start().await;
    let page1 = serde_json::json!({
        "nextPageToken": "NP1", "pollingIntervalMillis": 100,
        "items": [{"id": "m1"}]
    });
    let page2 = serde_json::json!({
        "nextPageToken": "NP2", "pollingIntervalMillis": 7000,
        "items": [{"id": "m2"}]
    });
    // First poll: no pageToken. wiremock matches the most specific mount; the
    // pageToken mock below is only hit once the cursor advanced.
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/messages"))
        .and(query_param("liveChatId", CHAT))
        .and(query_param("part", "id,snippet,authorDetails"))
        .and(query_param("maxResults", "200"))
        .and(header("authorization", format!("Bearer {TOKEN}").as_str()))
        .respond_with(ResponseTemplate::new(200).set_body_json(page1))
        .up_to_n_times(1)
        .with_priority(5)
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/youtube/v3/liveChat/messages"))
        .and(query_param("pageToken", "NP1"))
        .and(query_param("liveChatId", CHAT))
        .and(header("authorization", format!("Bearer {TOKEN}").as_str()))
        .respond_with(ResponseTemplate::new(200).set_body_json(page2))
        .with_priority(1)
        .mount(&server)
        .await;

    let (secrets, asked) = FakeSecrets::new(Mode::Token);
    let mut t = PollTransport::new(derived(&server, CHAT), secrets).unwrap();
    t.connect().await.unwrap();

    let f1 = t.next_frame().await.unwrap().expect("first frame");
    assert!(String::from_utf8_lossy(&f1).contains("\"m1\""));
    assert_eq!(
        t.interval(),
        Duration::from_secs(2),
        "100 ms server hint clamps to the 2 s floor"
    );
    t.on_frame_outcome(FrameOutcome::Published).await;
    assert_eq!(t.cursor(), Some("NP1"));

    let f2 = t
        .next_frame()
        .await
        .unwrap()
        .expect("second frame uses the page token");
    assert!(String::from_utf8_lossy(&f2).contains("\"m2\""));
    assert_eq!(
        t.interval(),
        Duration::from_secs(7),
        "server interval applied"
    );
    t.on_frame_outcome(FrameOutcome::Published).await;
    assert_eq!(t.cursor(), Some("NP2"));

    let reqs = server.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 2, "exactly two polls");
    assert!(reqs[0].url.query_pairs().all(|(k, _)| k != "pageToken"));
    assert_eq!(
        asked.lock().unwrap().as_slice(),
        &[ConnectionRef {
            community_id: 7,
            connection_id: 12
        }; 2],
        "the bearer resolved for the binding's own connection, each poll"
    );
}

#[tokio::test]
async fn quota_403_backs_off_and_does_not_park() {
    for reason in [
        "quotaExceeded",
        "rateLimitExceeded",
        "dailyLimitExceeded",
        "userRateLimitExceeded",
    ] {
        let server = MockServer::start().await;
        mount_403(&server, reason, Some("120")).await;
        let (secrets, _) = FakeSecrets::new(Mode::Token);
        let mut t = PollTransport::new(derived(&server, CHAT), secrets).unwrap();
        t.connect().await.unwrap();
        // Backing off = no frame, no error: the read just stays pending.
        let r = tokio::time::timeout(Duration::from_millis(600), t.next_frame()).await;
        assert!(r.is_err(), "{reason}: must back off silently, got {r:?}");
        let n = server.received_requests().await.unwrap().len();
        assert_eq!(n, 1, "{reason}: one request, then paused");
    }
}

#[tokio::test]
async fn forbidden_and_chat_ended_park_the_transport() {
    let mut parked = 0;
    for reason in [
        "forbidden",
        "insufficientPermissions",
        "liveChatEnded",
        "someUnlistedReason",
    ] {
        let server = MockServer::start().await;
        mount_403(&server, reason, None).await;
        let (secrets, _) = FakeSecrets::new(Mode::Token);
        let mut t = PollTransport::new(derived(&server, CHAT), secrets).unwrap();
        t.connect().await.unwrap();
        let e = t.next_frame().await.unwrap_err();
        assert!(e.is_fatal(), "{reason}: {e:?}");
        parked += 1;
    }
    assert_eq!(parked, 4);
}

#[tokio::test]
async fn token_refused_as_expiring_is_a_retry_not_a_park() {
    // The credential layer refuses a token expiring within 30 s with a
    // retryable (Unavailable) error; the transport must surface it as a
    // non-fatal failure so the driver reconnects under backoff.
    let server = MockServer::start().await;
    let (secrets, asked) = FakeSecrets::new(Mode::Unavailable);
    let mut t = PollTransport::new(derived(&server, CHAT), secrets).unwrap();
    t.connect().await.unwrap();
    let e = t.next_frame().await.unwrap_err();
    assert!(!e.is_fatal(), "retryable credential refusal parked: {e:?}");
    assert!(matches!(e, TransportError::Handshake(_)), "{e:?}");
    assert_eq!(asked.lock().unwrap().len(), 1);
    assert!(
        server.received_requests().await.unwrap().is_empty(),
        "no request is sent without a usable token"
    );

    // Contrast: a deterministically missing credential IS terminal.
    let (secrets, _) = FakeSecrets::new(Mode::Missing);
    let mut t = PollTransport::new(derived(&server, CHAT), secrets).unwrap();
    t.connect().await.unwrap();
    assert!(t.next_frame().await.unwrap_err().is_fatal());
}

#[tokio::test]
async fn a_changed_live_chat_id_derives_a_distinct_source_and_url() {
    let server = MockServer::start().await;
    let a = derived(&server, CHAT);
    let b = derived(&server, "NewChatId_9");
    assert_ne!(a.url, b.url);
    assert_ne!(a.source_key, b.source_key);
    assert!(b.url.contains("liveChatId=NewChatId_9"));
}
