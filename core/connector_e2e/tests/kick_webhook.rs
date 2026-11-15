//! Real-path e2e of Kick webhooks: a REAL `WebhookIngress` verifies deliveries with `Scheme::Kick`
//! against Kick's public key fetched by the REAL `HttpKickKeyFetch` + `KickKeyCache` from a local
//! fake Kick key endpoint, and the verified envelope is pumped by the REAL `ConnectorDriver` into
//! the REAL compiled Kick bundle (`fixtures/kick_connector.wasm`) behind svc_ingest's production
//! wire invoker. Deliveries are signed with a per-test RSA key (openssl CLI, `wix_sign.rs`; no
//! private key is ever committed).
//!
//! Doubles (and only these): the Kick key endpoint (wiremock), the dedup store (in memory; the
//! Valkey store is covered elsewhere) and the scripted identity stage the guest calls.

#![allow(clippy::unwrap_used, clippy::expect_used)]

#[path = "../../svc_ingest/tests/support/wix_sign.rs"]
mod wix_sign;

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::http::{HeaderMap, HeaderValue};
use base64::Engine as _;
use bundle_executor::config::CliConfig;
use bundle_executor::connector::ConnectorLimits;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::Executor;
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use bundle_executor::parity_support::scripted_bridge;
use bundle_executor::wire::run_connection;
use clap::Parser;
use connection_resolver::{ConnectionRef, SecretString};
use connector_e2e::{digest_of, NoSource};
use svc_ingest::connector::kick_keys::{HttpKickKeyFetch, KickKeyCache, KickKeySecrets};
use svc_ingest::connector::webhook::{
    BoxFut, DedupToken, RateLimit, SecretError, WebhookDedup, WebhookIngress, WebhookOutcome,
    WebhookRoute, WebhookSecretSource,
};
use svc_ingest::connector::webhook_verify::Scheme;
use svc_ingest::connector::{
    register_connector_metrics, ConnectionCtx, ConnectorDriver, ConnectorEvent, ConnectorRegistry,
    EventSink, ExecutorLink, PinnedConnector, SinkError, WireConnectorInvoker,
};
use tokio::sync::oneshot;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

const WASM: &[u8] = include_bytes!("fixtures/kick_connector.wasm");
const APP_ID: &str = "waddles.core.service.kick";
const MAX_FRAME: usize = 65_536;
const ROUTE: &str = "kick-route-key-0123456789abcdefghijklmnopqrstuv";
const NOW: i64 = 1_792_995_200;
const TS: &str = "2026-10-26T06:13:20Z";
const ID: &str = "01KBHE78QE4HZY1617DK5FC7YD";
/// The broadcaster this route's connection is bound to.
const BROADCASTER: &str = "1";
const FOLLOW: &[u8] = br#"{"broadcaster":{"user_id":1,"username":"Chan","channel_slug":"chan"},"follower":{"user_id":2,"username":"Fan","channel_slug":"fan"}}"#;

/// No community secret exists for Kick routes; any fallback to it is a bug.
struct NoInner;

impl WebhookSecretSource for NoInner {
    fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        Box::pin(async { Err(SecretError::Missing) })
    }
}

#[derive(Default)]
struct MemDedup(Mutex<HashSet<String>>);

impl WebhookDedup for MemDedup {
    fn first_seen<'a>(&'a self, key: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
        let fresh = self.0.lock().unwrap().insert(key.to_string());
        Box::pin(async move { Ok(fresh.then(|| DedupToken("t".into()))) })
    }
    fn release<'a>(&'a self, key: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
        self.0.lock().unwrap().remove(key);
        Box::pin(async { Ok(()) })
    }
}

/// The fake Kick key endpoint: serves whatever PEM is currently set, or a 500 when unset.
#[derive(Clone, Default)]
struct KeyEndpoint(Arc<Mutex<Option<String>>>);

impl Respond for KeyEndpoint {
    fn respond(&self, _r: &Request) -> ResponseTemplate {
        match &*self.0.lock().unwrap() {
            Some(pem) => ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"data": {"public_key": pem}, "message": "OK"})),
            None => ResponseTemplate::new(500),
        }
    }
}

#[derive(Clone, Default)]
struct RecordingSink(Arc<Mutex<Vec<ConnectorEvent>>>);

impl EventSink for RecordingSink {
    async fn publish(&self, _ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        self.0.lock().unwrap().push(event);
        Ok(())
    }
}

struct Rig {
    ingress: Arc<WebhookIngress>,
    key_endpoint: KeyEndpoint,
    key_server: MockServer,
    events: Arc<Mutex<Vec<ConnectorEvent>>>,
    shutdown: oneshot::Sender<()>,
    driver: tokio::task::JoinHandle<()>,
    _conn: tokio::task::JoinHandle<()>,
}

/// `min_refresh`/`overlap`: the key cache's real-time bounds (kept tiny so rotation is testable).
async fn rig(min_refresh: Duration, overlap: Duration, served: Option<&wix_sign::WixKey>) -> Rig {
    let key_endpoint = KeyEndpoint(Arc::new(Mutex::new(served.map(|k| k.public_pem.clone()))));
    let key_server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/public/v1/public-key"))
        .respond_with(key_endpoint.clone())
        .mount(&key_server)
        .await;
    let fetch =
        HttpKickKeyFetch::with_endpoint(format!("{}/public/v1/public-key", key_server.uri()))
            .unwrap();
    let cache = Arc::new(KickKeyCache::with_bounds(
        fetch,
        Duration::from_secs(3600),
        min_refresh,
        Duration::from_secs(86_400),
        overlap,
    ));
    let ingress = Arc::new(WebhookIngress::new(
        Arc::new(KickKeySecrets::new(NoInner, cache)),
        Arc::new(MemDedup::default()),
        None,
    ));
    ingress
        .register(
            WebhookRoute {
                route_key: ROUTE.into(),
                owner_bundle: "kick".into(),
                scheme: Scheme::kick(BROADCASTER),
                secret_ref: ConnectionRef {
                    community_id: 7,
                    connection_id: 9,
                },
                replay_window: Duration::from_secs(300),
                rate_limit: RateLimit {
                    max: 1000,
                    per: Duration::from_secs(60),
                },
                body_limit: MAX_FRAME,
            },
            7,
        )
        .expect("route registers");
    let transport = ingress.bind_transport(ROUTE, "kick").expect("transport");

    // The real Kick connector behind the production wire invoker.
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap();
    let executor = Arc::new(Executor::new(&cfg, NoSource).unwrap());
    let digest = digest_of(WASM);
    let manifest = VerifiedManifest::new(
        APP_ID,
        digest.clone(),
        WitWorld::ConnectorTools,
        HashSet::from([PERM_CONNECTOR_PII_READ.to_string()]),
    );
    let (bridge, _identity_calls) = scripted_bridge();
    executor
        .pin_connector(
            &manifest,
            WASM.to_vec(),
            ConnectorLimits::from_config(&cfg),
            Some(bridge),
        )
        .await
        .expect("the real kick connector pins");
    let mut registry = ConnectorRegistry::new();
    registry
        .register(PinnedConnector::new("kick", APP_ID, digest, MAX_FRAME))
        .unwrap();
    let registry = Arc::new(registry);
    let link = Arc::new(ExecutorLink::new());
    let invoker = Arc::new(WireConnectorInvoker::new(
        Arc::clone(&link),
        Arc::clone(&registry),
    ));
    let (stage_io, exec_io) = tokio::io::duplex(4 << 20);
    let hello = executor.hello(false, "runc");
    let conn = tokio::spawn(async move {
        let _ = run_connection(
            exec_io,
            hello,
            executor,
            "connector-e2e",
            Heartbeat::disabled(),
        )
        .await;
    });
    link.attach(stage_io).await.expect("host-api handshake");

    let sink = RecordingSink::default();
    let events = Arc::clone(&sink.0);
    let driver = ConnectorDriver::new(
        registry,
        invoker,
        Arc::new(sink),
        register_connector_metrics(&prometheus::Registry::new()).unwrap(),
    )
    .with_backoff_cap(Duration::from_millis(5))
    .with_heartbeat_enabled(false);
    let (shutdown, rx) = oneshot::channel();
    let driver = tokio::spawn(async move {
        driver.run(transport, rx).await.unwrap();
    });
    Rig {
        ingress,
        key_endpoint,
        key_server,
        events,
        shutdown,
        driver,
        _conn: conn,
    }
}

impl Rig {
    async fn stop(self) {
        let _ = self.shutdown.send(());
        tokio::time::timeout(Duration::from_secs(10), self.driver)
            .await
            .expect("driver stops")
            .unwrap();
    }

    async fn wait_events(&self, n: usize) {
        for _ in 0..200 {
            if self.events.lock().unwrap().len() >= n {
                return;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        panic!(
            "expected {n} events, saw {}",
            self.events.lock().unwrap().len()
        );
    }

    async fn key_fetches(&self) -> usize {
        self.key_server.received_requests().await.unwrap().len()
    }
}

fn headers(sig: &[u8], ty: &str, id: &str, ts: &str) -> HeaderMap {
    let mut h = HeaderMap::new();
    for (n, v) in [
        ("kick-event-message-id", id.to_string()),
        ("kick-event-message-timestamp", ts.to_string()),
        (
            "kick-event-signature",
            base64::engine::general_purpose::STANDARD.encode(sig),
        ),
        ("kick-event-type", ty.to_string()),
        ("kick-event-version", "1".to_string()),
    ] {
        h.insert(n, HeaderValue::from_str(&v).unwrap());
    }
    h
}

/// Signs `body` as Kick would (`id.timestamp.body`) with `key` and delivers it.
async fn deliver(
    rig: &Rig,
    key: &wix_sign::WixKey,
    ty: &str,
    id: &str,
    body: &[u8],
    now: i64,
) -> WebhookOutcome {
    let msg = [format!("{id}.{TS}.").as_bytes(), body].concat();
    let h = headers(&wix_sign::sign(key, &msg), ty, id, TS);
    rig.ingress.handle(ROUTE, &h, body, now).await
}

fn is_uuid_shaped(s: &str) -> bool {
    let p: Vec<&str> = s.split('-').collect();
    p.iter().map(|x| x.len()).collect::<Vec<_>>() == [8, 4, 4, 4, 12]
}

const FAST: Duration = Duration::from_millis(50);
const SLOW_OVERLAP: Duration = Duration::from_secs(600);

/// The whole inbound webhook path: signature verified against the FETCHED key, the header-borne
/// event type reaches the guest through the envelope, and the guest emits a UUID-actor event.
#[tokio::test(flavor = "multi_thread")]
async fn a_signed_delivery_is_accepted_and_the_guest_normalizes_the_enveloped_event() {
    let kick = wix_sign::generate();
    let rig = rig(FAST, SLOW_OVERLAP, Some(&kick)).await;
    assert_eq!(
        deliver(&rig, &kick, "channel.followed", ID, FOLLOW, NOW).await,
        WebhookOutcome::Accepted
    );
    rig.wait_events(1).await;
    {
        let ev = rig.events.lock().unwrap();
        assert_eq!(ev.len(), 1);
        assert_eq!(ev[0].event_type, "channel.follow");
        assert!(
            is_uuid_shaped(ev[0].actor.as_deref().unwrap()),
            "{:?}",
            ev[0].actor
        );
        let payload: serde_json::Value = serde_json::from_str(&ev[0].payload_json).unwrap();
        assert_eq!(payload["broadcaster_id"], "1");
    }
    // The key was fetched from Kick once and cached for the next delivery.
    assert_eq!(rig.key_fetches().await, 1);
    // A replay of the same signed message is deduplicated, never re-dispatched.
    assert_eq!(
        deliver(&rig, &kick, "channel.followed", ID, FOLLOW, NOW).await,
        WebhookOutcome::Duplicate
    );
    rig.stop().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn bad_signature_stale_timestamp_and_tampered_body_are_rejected_and_nothing_reaches_the_guest(
) {
    let kick = wix_sign::generate();
    let attacker = wix_sign::generate();
    let rig = rig(FAST, SLOW_OVERLAP, Some(&kick)).await;
    // Signed by a key Kick does not publish.
    assert_eq!(
        deliver(&rig, &attacker, "channel.followed", ID, FOLLOW, NOW).await,
        WebhookOutcome::Rejected
    );
    // Genuinely signed, but the body was changed afterwards.
    let msg = [format!("{ID}.{TS}.").as_bytes(), FOLLOW].concat();
    let h = headers(&wix_sign::sign(&kick, &msg), "channel.followed", ID, TS);
    let tampered = FOLLOW
        .to_vec()
        .into_iter()
        .map(|b| if b == b'2' { b'3' } else { b })
        .collect::<Vec<_>>();
    assert_eq!(
        rig.ingress.handle(ROUTE, &h, &tampered, NOW).await,
        WebhookOutcome::Rejected
    );
    // Genuinely signed but outside the replay window (old and far-future).
    for now in [NOW + 10_000, NOW - 10_000] {
        assert_eq!(
            deliver(&rig, &kick, "channel.followed", ID, FOLLOW, now).await,
            WebhookOutcome::Rejected,
            "now={now}"
        );
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(rig.events.lock().unwrap().is_empty());
    rig.stop().await;
}

/// SECURITY: Kick signs with ONE global key, so a valid signature does not say which community a
/// delivery is for. A genuine delivery for ANOTHER broadcaster must not enter this connection.
#[tokio::test(flavor = "multi_thread")]
async fn a_genuinely_signed_delivery_for_another_broadcaster_is_rejected() {
    let kick = wix_sign::generate();
    let rig = rig(FAST, SLOW_OVERLAP, Some(&kick)).await;
    let other = br#"{"broadcaster":{"user_id":2,"username":"Other"},"follower":{"user_id":3,"username":"Fan"}}"#;
    assert_eq!(
        deliver(&rig, &kick, "channel.followed", ID, other, NOW).await,
        WebhookOutcome::Rejected,
        "valid signature + other broadcaster"
    );
    // No broadcaster at all is never bound either.
    let none = br#"{"follower":{"user_id":3}}"#;
    assert_eq!(
        deliver(
            &rig,
            &kick,
            "channel.followed",
            "01OTHER0000000000000000001",
            none,
            NOW
        )
        .await,
        WebhookOutcome::Rejected
    );
    // The type header is unsigned: a genuine follow replayed as a chat message is refused.
    assert_eq!(
        deliver(
            &rig,
            &kick,
            "chat.message.sent",
            "01OTHER0000000000000000002",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Rejected,
        "event type must match the signed body"
    );
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(rig.events.lock().unwrap().is_empty());
    // The bound broadcaster still works.
    assert_eq!(
        deliver(
            &rig,
            &kick,
            "channel.followed",
            "01OTHER0000000000000000003",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Accepted
    );
    rig.wait_events(1).await;
    rig.stop().await;
}

/// Key rotation: after Kick rotates, a delivery signed with the NEW key fails against the cached
/// key, triggers one forced key refresh, and is accepted; the retired key keeps verifying only for
/// the overlap window.
#[tokio::test(flavor = "multi_thread")]
async fn key_rotation_is_picked_up_and_the_retired_key_stops_after_the_overlap() {
    let old = wix_sign::generate();
    let new = wix_sign::generate();
    let overlap = Duration::from_millis(1500);
    let rig = rig(FAST, overlap, Some(&old)).await;
    assert_eq!(
        deliver(
            &rig,
            &old,
            "channel.followed",
            "01ROT00000000000000000000A",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Accepted
    );
    assert_eq!(rig.key_fetches().await, 1);

    // Kick rotates. The next delivery (new key) misses the cache, refreshes once, and verifies.
    *rig.key_endpoint.0.lock().unwrap() = Some(new.public_pem.clone());
    tokio::time::sleep(FAST * 2).await;
    assert_eq!(
        deliver(
            &rig,
            &new,
            "channel.followed",
            "01ROT00000000000000000000B",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Accepted
    );
    assert_eq!(rig.key_fetches().await, 2, "exactly one forced refresh");

    // Inside the overlap a delivery still in flight under the OLD key verifies ...
    assert_eq!(
        deliver(
            &rig,
            &old,
            "channel.followed",
            "01ROT00000000000000000000C",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Accepted
    );
    // ... and once it is over the retired key is refused.
    tokio::time::sleep(overlap + FAST).await;
    assert_eq!(
        deliver(
            &rig,
            &old,
            "channel.followed",
            "01ROT00000000000000000000D",
            FOLLOW,
            NOW
        )
        .await,
        WebhookOutcome::Rejected,
        "a retired key must not verify after the overlap"
    );
    rig.wait_events(3).await;
    rig.stop().await;
}

/// A forged-signature flood cannot amplify into Kick API calls: refreshes are rate-limited.
#[tokio::test(flavor = "multi_thread")]
async fn a_forged_flood_does_not_amplify_into_key_fetches() {
    let kick = wix_sign::generate();
    let attacker = wix_sign::generate();
    let rig = rig(Duration::from_secs(30), SLOW_OVERLAP, Some(&kick)).await;
    for i in 0..40 {
        let id = format!("01FLOOD{i:019}");
        assert_eq!(
            deliver(&rig, &attacker, "channel.followed", &id, FOLLOW, NOW).await,
            WebhookOutcome::Rejected
        );
    }
    assert_eq!(
        rig.key_fetches().await,
        1,
        "one fetch, refresh suppressed by the spacing"
    );
    rig.stop().await;
}

/// No usable key (Kick's key endpoint down, nothing cached): 503 so Kick redelivers, never an
/// accept and never a hard reject.
#[tokio::test(flavor = "multi_thread")]
async fn without_a_key_the_route_is_unavailable_not_open_or_closed() {
    let kick = wix_sign::generate();
    let rig = rig(FAST, SLOW_OVERLAP, None).await;
    assert_eq!(
        deliver(&rig, &kick, "channel.followed", ID, FOLLOW, NOW).await,
        WebhookOutcome::Unavailable
    );
    // The endpoint recovers: once the spacing window passes, the same delivery is accepted.
    *rig.key_endpoint.0.lock().unwrap() = Some(kick.public_pem.clone());
    tokio::time::sleep(FAST * 2).await;
    assert_eq!(
        deliver(&rig, &kick, "channel.followed", ID, FOLLOW, NOW).await,
        WebhookOutcome::Accepted
    );
    rig.wait_events(1).await;
    rig.stop().await;
}
