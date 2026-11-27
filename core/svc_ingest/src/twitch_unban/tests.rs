//! Unit tests of the Twitch `channel.unban` forwarder: subscription binding, cross-tenant
//! refusal, live-state check, observed time and the signed envelope.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Mutex;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ring::signature::{Ed25519KeyPair, KeyPair, UnparsedPublicKey, ED25519};
use serde_json::{json, Value};

use super::*;
use crate::outbound_creds::TwitchCredentialSource;
use connection_resolver::SecretString;

const SEED: [u8; 32] = [7u8; 32];
const UUID: &str = "11111111-2222-4333-8444-555555555555";
const TS: &str = "2026-11-21T12:00:00.123456789Z";
const TS_MS: u64 = 1_795_262_400_123;

fn entry(tenant: i64, community: i64, connection: i64) -> LedgerEntry {
    LedgerEntry {
        secret: SecretString::new("s3cret".to_string()),
        tenant_id: tenant,
        community_id: community,
        connection_id: connection,
    }
}

struct NoLedger;
impl SubscriptionLedger for NoLedger {
    fn lookup<'a>(&'a self, _k: &'a str) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        Box::pin(async { Ok(None) })
    }
}

/// `(tenant, community, connection)` -> bound login.
type OwnerRow = ((i64, i64, i64), Result<BoundChannel, OwnerError>);

/// A bound channel with its (optional) stored numeric id.
fn bound(login: &str, id: Option<&str>) -> BoundChannel {
    BoundChannel {
        login: login.to_string(),
        channel_id: id.map(str::to_string),
    }
}
struct Owner(Vec<OwnerRow>);
impl ConnectionOwner for Owner {
    fn bound_channel<'a>(
        &'a self,
        t: i64,
        c: i64,
        n: i64,
    ) -> BoxFut<'a, Result<BoundChannel, OwnerError>> {
        let r = self
            .0
            .iter()
            .find(|(k, _)| *k == (t, c, n))
            .map_or(Err(OwnerError::NotFound), |(_, r)| r.clone());
        Box::pin(async move { r })
    }
}

struct Bans(Result<bool, ()>, Mutex<Vec<(i64, String, String, String)>>);
impl BanState for Bans {
    fn is_banned<'a>(
        &'a self,
        community_id: i64,
        login: &'a str,
        bcast: &'a str,
        user: &'a str,
    ) -> BoxFut<'a, Result<bool, ()>> {
        self.1.lock().unwrap().push((
            community_id,
            login.to_string(),
            bcast.to_string(),
            user.to_string(),
        ));
        let r = self.0;
        Box::pin(async move { r })
    }
}

struct Tok(
    Result<String, TokenizeError>,
    Mutex<Vec<(CallScope, String)>>,
);
impl IdentityTokenizer for Tok {
    fn tokenize<'a>(
        &'a self,
        scope: &'a CallScope,
        id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        self.1.lock().unwrap().push((scope.clone(), id.to_string()));
        let r = self.0.clone();
        Box::pin(async move { r })
    }
}

#[derive(Default)]
struct Sink {
    pushed: Mutex<Vec<String>>,
    fail: bool,
}
impl UnbanSink for Sink {
    fn push<'a>(&'a self, payload: String) -> BoxFut<'a, Result<(), ()>> {
        let ok = !self.fail;
        if ok {
            self.pushed.lock().unwrap().push(payload);
        }
        Box::pin(async move {
            if ok {
                Ok(())
            } else {
                Err(())
            }
        })
    }
}

struct Rig {
    unban: TwitchUnban,
    sink: Arc<Sink>,
    bans: Arc<Bans>,
    tok: Arc<Tok>,
}

fn rig(
    owner: Vec<OwnerRow>,
    banned: Result<bool, ()>,
    token: Result<String, TokenizeError>,
    signer: bool,
    sink_fails: bool,
) -> Rig {
    let sink = Arc::new(Sink {
        fail: sink_fails,
        ..Sink::default()
    });
    let bans = Arc::new(Bans(banned, Mutex::default()));
    let tok = Arc::new(Tok(token, Mutex::default()));
    let unban = TwitchUnban::new(
        Arc::new(NoLedger),
        Arc::new(Owner(owner)),
        Arc::clone(&bans) as Arc<dyn BanState>,
        Arc::clone(&tok) as Arc<dyn IdentityTokenizer>,
        Arc::clone(&sink) as Arc<dyn UnbanSink>,
        signer.then(|| Signer::from_seed("k1", &SEED).unwrap()),
    );
    Rig {
        unban,
        sink,
        bans,
        tok,
    }
}

fn good_rig() -> Rig {
    rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Ok(false),
        Ok(UUID.to_string()),
        true,
        false,
    )
}

fn sub(bcast: &str) -> Value {
    json!({"id": "s1", "type": "channel.unban", "condition": {"broadcaster_user_id": bcast}})
}

fn ev(bcast: &str, login: &str, user: &str) -> Value {
    json!({"broadcaster_user_id": bcast, "broadcaster_user_login": login,
           "broadcaster_user_name": login, "user_id": user, "user_login": "bob",
           "moderator_user_id": "5"})
}

async fn run(r: &Rig, e: &LedgerEntry, s: &Value, v: &Value, ts: &str) -> Outcome {
    r.unban.handle(e, s, v, ts, "msg-1").await
}

fn pubkey() -> UnparsedPublicKey<Vec<u8>> {
    let pair = Ed25519KeyPair::from_seed_unchecked(&SEED).unwrap();
    UnparsedPublicKey::new(&ED25519, pair.public_key().as_ref().to_vec())
}

#[tokio::test]
async fn a_bound_unban_is_signed_with_ledger_ids_the_event_time_and_only_the_user_token() {
    let r = good_rig();
    let out = run(
        &r,
        &entry(7, 9, 33),
        &sub("141"),
        &ev("141", "Alice", "777"),
        TS,
    )
    .await;
    assert_eq!(out, Outcome::Forwarded);
    let pushed = r.sink.pushed.lock().unwrap().clone();
    assert_eq!(pushed.len(), 1);
    assert!(
        !pushed[0].contains("777"),
        "the raw Twitch user id is never queued"
    );
    let w: Value = serde_json::from_str(&pushed[0]).unwrap();
    assert_eq!(
        (
            w["tenant_id"].as_i64(),
            w["community_id"].as_i64(),
            w["connection_id"].as_i64()
        ),
        (Some(7), Some(9), Some(33)),
        "binding comes from the ledger row"
    );
    assert_eq!(w["observed_ms"], TS_MS);
    assert_eq!(w["platform"], "twitch");
    assert!(
        w.get("broadcaster_id").is_none(),
        "the raw broadcaster id is never queued"
    );
    assert_eq!(w["user_uuid"], UUID);
    // The signature covers every field and verifies under the public key.
    let msg = signing_bytes(
        w["event_id"].as_str().unwrap(),
        7,
        9,
        33,
        UUID,
        TS_MS,
        w["exp_ms"].as_u64().unwrap(),
    );
    let sig = B64.decode(w["sig"].as_str().unwrap()).unwrap();
    assert!(pubkey().verify(&msg, &sig).is_ok());
    let other = signing_bytes("x", 8, 9, 33, UUID, TS_MS, 1);
    assert!(pubkey().verify(&other, &sig).is_err());
    // The tokenizer was scoped to the ledger tenant/community/connection.
    let calls = r.tok.1.lock().unwrap().clone();
    assert_eq!(calls[0].0.tenant_id, 7);
    assert_eq!(calls[0].0.connection_id, 33);
    assert_eq!(calls[0].1, "777");
    // Live state was read for THIS community's binding.
    assert_eq!(
        r.bans.1.lock().unwrap()[0],
        (9, "alice".into(), "141".into(), "777".into())
    );
}

#[tokio::test]
async fn a_valid_delivery_from_one_tenant_naming_anothers_broadcaster_forwards_nothing() {
    // Tenant 7's subscription (community 9, connection 33) is bound to `alice`. The event names
    // tenant 8's broadcaster `mallory` (id 999): signature-valid for tenant 7, but not its channel.
    let r = good_rig();
    let before = count("ignored", "broadcaster_mismatch");
    let out = run(
        &r,
        &entry(7, 9, 33),
        &sub("999"),
        &ev("999", "mallory", "777"),
        TS,
    )
    .await;
    assert_eq!(out, Outcome::Ignored("broadcaster_mismatch"));
    assert!(count("ignored", "broadcaster_mismatch") > before);
    assert!(r.sink.pushed.lock().unwrap().is_empty());
    assert!(r.tok.1.lock().unwrap().is_empty(), "nothing was tokenized");
    assert!(
        r.bans.1.lock().unwrap().is_empty(),
        "no Helix call was made"
    );
}

#[tokio::test]
async fn an_unknown_or_cross_tenant_binding_is_ignored_with_a_metric() {
    let r = rig(vec![], Ok(false), Ok(UUID.into()), true, false);
    let before = count("ignored", "unknown_broadcaster");
    assert_eq!(
        run(
            &r,
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Ignored("unknown_broadcaster")
    );
    assert!(count("ignored", "unknown_broadcaster") > before);
    let r = rig(
        vec![((7, 9, 33), Err(OwnerError::TenantMismatch))],
        Ok(false),
        Ok(UUID.into()),
        true,
        false,
    );
    assert_eq!(
        run(
            &r,
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Ignored("tenant_mismatch")
    );
    let r = rig(
        vec![((7, 9, 33), Err(OwnerError::Unavailable))],
        Ok(false),
        Ok(UUID.into()),
        true,
        false,
    );
    assert_eq!(
        run(
            &r,
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Retry("binding_unavailable")
    );
    // A connection of ANOTHER tenant/community is simply not this subscription's binding.
    let r = good_rig();
    assert_eq!(
        run(
            &r,
            &entry(8, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Ignored("unknown_broadcaster")
    );
    assert!(r.sink.pushed.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_rebanned_user_or_an_unreadable_state_is_dropped_never_lifted() {
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Ok(true),
        Ok(UUID.into()),
        true,
        false,
    );
    assert_eq!(
        run(
            &r,
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Ignored("rebanned")
    );
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Err(()),
        Ok(UUID.into()),
        true,
        false,
    );
    assert_eq!(
        run(
            &r,
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS
        )
        .await,
        Outcome::Retry("ban_state_unavailable")
    );
    assert!(r.sink.pushed.lock().unwrap().is_empty());
    assert!(r.tok.1.lock().unwrap().is_empty());
}

#[tokio::test]
async fn malformed_condition_timestamp_tokenizer_signer_and_sink_failures_all_fail_closed() {
    let e = entry(7, 9, 33);
    let r = good_rig();
    // subscription condition vs event broadcaster disagree
    assert_eq!(
        run(&r, &e, &sub("141"), &ev("142", "alice", "777"), TS).await,
        Outcome::Ignored("condition_mismatch")
    );
    // missing / non-numeric ids
    assert_eq!(
        run(
            &r,
            &e,
            &sub("141"),
            &json!({"broadcaster_user_id": "141"}),
            TS
        )
        .await,
        Outcome::Ignored("malformed")
    );
    assert_eq!(
        run(&r, &e, &sub("141"), &ev("141", "alice", "7x7"), TS).await,
        Outcome::Ignored("malformed")
    );
    // bad / far-future timestamp
    assert_eq!(
        run(&r, &e, &sub("141"), &ev("141", "alice", "777"), "yesterday").await,
        Outcome::Ignored("bad_timestamp")
    );
    assert_eq!(
        run(
            &r,
            &e,
            &sub("141"),
            &ev("141", "alice", "777"),
            "2999-01-01T00:00:00Z"
        )
        .await,
        Outcome::Ignored("bad_timestamp")
    );
    // tokenizer failures
    for (err, reason) in [
        (TokenizeError::Unavailable, "tokenize_unavailable"),
        (TokenizeError::Denied, "tokenize_refused"),
    ] {
        let r = rig(
            vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
            Ok(false),
            Err(err),
            true,
            false,
        );
        let expected = if reason == "tokenize_unavailable" {
            Outcome::Retry(reason)
        } else {
            Outcome::Ignored(reason)
        };
        assert_eq!(
            run(&r, &e, &sub("141"), &ev("141", "alice", "777"), TS).await,
            expected
        );
    }
    // no signing key, sink down
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Ok(false),
        Ok(UUID.into()),
        false,
        false,
    );
    assert_eq!(
        run(&r, &e, &sub("141"), &ev("141", "alice", "777"), TS).await,
        Outcome::Ignored("no_signing_key")
    );
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Ok(false),
        Ok(UUID.into()),
        true,
        true,
    );
    assert_eq!(
        run(&r, &e, &sub("141"), &ev("141", "alice", "777"), TS).await,
        Outcome::Retry("publish_failed")
    );
}

#[test]
fn the_twitch_domain_label_separates_it_from_the_discord_signature() {
    assert_ne!(DOMAIN, crate::platform_unban::DOMAIN);
    // Same field values under the Discord layout never produce Twitch's bytes.
    let t = signing_bytes("e", 7, 9, 33, UUID, 5, 6);
    let d = crate::platform_unban::signing_bytes("e", 7, 33, "twitch", "141", UUID, 5, 6);
    assert_ne!(t, d);
    assert!(t.starts_with(DOMAIN) && d.starts_with(crate::platform_unban::DOMAIN));
}

#[test]
fn callback_keys_are_validated_before_any_hub_call() {
    assert!(is_callback_key("abcdefgh-12_34"));
    for bad in ["", "short", "has space!!", "../etc/passwd", &"a".repeat(65)] {
        assert!(!is_callback_key(bad), "{bad}");
    }
}

// ---- Helix live-state read -------------------------------------------------------------

struct NoCreds;
impl TwitchCredentialSource for NoCreds {
    async fn credential(
        &self,
        _c: Option<i64>,
        _ch: &str,
    ) -> Result<crate::outbound_creds::TwitchCredential, crate::outbound_creds::CredError> {
        Err(crate::outbound_creds::CredError::NoBinding)
    }
}

fn helix(server: &wiremock::MockServer) -> HelixBanState<NoCreds> {
    HelixBanState::with_endpoint(
        NoCreds,
        "cid123".to_string(),
        bundle_host_http::guarded::GuardedClient::new(
            bundle_host_http::guarded::GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 64 * 1024,
            },
            vec![std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)],
        ),
        format!("{}/helix/moderation/banned", server.uri()),
    )
}

async fn helix_server(status: u16, body: Value) -> wiremock::MockServer {
    let s = wiremock::MockServer::start().await;
    wiremock::Mock::given(wiremock::matchers::any())
        .respond_with(wiremock::ResponseTemplate::new(status).set_body_json(body))
        .mount(&s)
        .await;
    s
}

#[tokio::test]
async fn the_live_state_read_is_scoped_by_ids_and_fails_closed() {
    // Not banned: Helix answers an empty list.
    let s = helix_server(200, json!({"data": []})).await;
    assert_eq!(
        helix(&s).state_with_token("tok", "141", "777").await,
        Ok(false)
    );
    let req = &s.received_requests().await.unwrap()[0];
    let q: std::collections::HashMap<_, _> = req.url.query_pairs().into_owned().collect();
    assert_eq!(
        (q["broadcaster_id"].as_str(), q["user_id"].as_str()),
        ("141", "777")
    );
    assert_eq!(
        req.headers.get("authorization").unwrap().to_str().unwrap(),
        "Bearer tok"
    );
    assert_eq!(
        req.headers.get("client-id").unwrap().to_str().unwrap(),
        "cid123"
    );
    // Banned (or timed out) now: a row is present.
    let s = helix_server(200, json!({"data": [{"user_id": "777", "expires_at": ""}]})).await;
    assert_eq!(
        helix(&s).state_with_token("tok", "141", "777").await,
        Ok(true)
    );
    // Any non-2xx or unparseable answer is an error, never "not banned".
    for (status, body) in [
        (401, json!({})),
        (500, json!({})),
        (200, json!({"nope": 1})),
    ] {
        let s = helix_server(status, body).await;
        assert_eq!(
            helix(&s).state_with_token("tok", "141", "777").await,
            Err(())
        );
    }
}

// ---- Ledger cache -----------------------------------------------------------------------

/// Counts hub lookups; serves a mutable secret (or "unknown" / an outage).
struct CountingLedger {
    hits: std::sync::atomic::AtomicUsize,
    secret: Mutex<Option<&'static str>>,
    down: std::sync::atomic::AtomicBool,
    delay_ms: std::sync::atomic::AtomicU64,
}

impl CountingLedger {
    fn new(secret: Option<&'static str>) -> Arc<Self> {
        Arc::new(Self {
            hits: std::sync::atomic::AtomicUsize::new(0),
            secret: Mutex::new(secret),
            down: std::sync::atomic::AtomicBool::new(false),
            delay_ms: std::sync::atomic::AtomicU64::new(0),
        })
    }
    fn hits(&self) -> usize {
        self.hits.load(std::sync::atomic::Ordering::SeqCst)
    }
}

impl SubscriptionLedger for Arc<CountingLedger> {
    fn lookup<'a>(&'a self, _key: &'a str) -> BoxFut<'a, Result<Option<LedgerEntry>, LedgerError>> {
        Box::pin(async move {
            self.hits.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let d = self.delay_ms.load(std::sync::atomic::Ordering::SeqCst);
            if d > 0 {
                tokio::time::sleep(Duration::from_millis(d)).await;
            }
            if self.down.load(std::sync::atomic::Ordering::SeqCst) {
                return Err(LedgerError::Unavailable);
            }
            Ok(self.secret.lock().unwrap().map(|s| LedgerEntry {
                secret: SecretString::new(s.to_string()),
                tenant_id: 7,
                community_id: 9,
                connection_id: 33,
            }))
        })
    }
}

#[tokio::test]
async fn the_ledger_cache_serves_repeats_from_memory_and_refetches_after_invalidation() {
    let inner = CountingLedger::new(Some("secret-1"));
    let cache = CachedLedger::new(Arc::clone(&inner));
    for _ in 0..5 {
        let e = cache.lookup("key-12345678").await.unwrap().unwrap();
        assert_eq!(
            (
                e.secret.expose(),
                e.tenant_id,
                e.community_id,
                e.connection_id
            ),
            ("secret-1", 7, 9, 33)
        );
    }
    assert_eq!(inner.hits(), 1, "one hub call for five deliveries");
    // Secret rotated at hub-api: still the old one from memory until invalidated...
    *inner.secret.lock().unwrap() = Some("secret-2");
    assert_eq!(
        cache
            .lookup("key-12345678")
            .await
            .unwrap()
            .unwrap()
            .secret
            .expose(),
        "secret-1"
    );
    cache.invalidate("key-12345678");
    assert_eq!(
        cache
            .lookup("key-12345678")
            .await
            .unwrap()
            .unwrap()
            .secret
            .expose(),
        "secret-2"
    );
    assert_eq!(inner.hits(), 2);
}

#[tokio::test]
async fn positive_entries_expire_negatives_expire_fast_and_errors_are_never_cached() {
    let inner = CountingLedger::new(Some("secret-1"));
    let cache = CachedLedger::with_limits(
        Arc::clone(&inner),
        Duration::from_millis(40),
        Duration::from_millis(10),
        8,
    );
    cache.lookup("key-12345678").await.unwrap();
    tokio::time::sleep(Duration::from_millis(60)).await;
    cache.lookup("key-12345678").await.unwrap();
    assert_eq!(inner.hits(), 2, "positive entry expired after its TTL");

    // Unknown subscription: remembered only briefly.
    *inner.secret.lock().unwrap() = None;
    assert!(cache.lookup("key-nope-0001").await.unwrap().is_none());
    assert!(cache.lookup("key-nope-0001").await.unwrap().is_none());
    assert_eq!(inner.hits(), 3, "negative answer served from memory");
    tokio::time::sleep(Duration::from_millis(25)).await;
    *inner.secret.lock().unwrap() = Some("secret-new");
    assert!(
        cache.lookup("key-nope-0001").await.unwrap().is_some(),
        "a new subscription is picked up once the short negative TTL passes"
    );

    // An outage is an error each time, never a cached answer.
    inner.down.store(true, std::sync::atomic::Ordering::SeqCst);
    let before = inner.hits();
    assert!(cache.lookup("key-other-001").await.is_err());
    assert!(cache.lookup("key-other-001").await.is_err());
    assert_eq!(inner.hits(), before + 2);
}

#[tokio::test]
async fn the_cache_is_bounded() {
    let inner = CountingLedger::new(Some("s"));
    let cache = CachedLedger::with_limits(Arc::clone(&inner), CACHE_TTL, NEGATIVE_TTL, 4);
    for i in 0..50 {
        cache.lookup(&format!("key-{i:08}")).await.unwrap();
    }
    assert!(cache.map.lock().unwrap().len() <= 4);
}

#[tokio::test]
async fn the_event_id_is_derived_from_the_message_id_so_a_redelivery_dedupes_downstream() {
    let r = good_rig();
    for _ in 0..2 {
        let out = r
            .unban
            .handle(
                &entry(7, 9, 33),
                &sub("141"),
                &ev("141", "alice", "777"),
                TS,
                "msg-same",
            )
            .await;
        assert_eq!(out, Outcome::Forwarded);
    }
    let out = r
        .unban
        .handle(
            &entry(7, 9, 33),
            &sub("141"),
            &ev("141", "alice", "777"),
            TS,
            "msg-other",
        )
        .await;
    assert_eq!(out, Outcome::Forwarded);
    let ids: Vec<String> = r
        .sink
        .pushed
        .lock()
        .unwrap()
        .iter()
        .map(|p| {
            serde_json::from_str::<Value>(p).unwrap()["event_id"]
                .as_str()
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(ids[0], ids[1], "same message id, same event id");
    assert_ne!(ids[0], ids[2], "different message id, different event id");
    assert_eq!(
        ids[0],
        Uuid::new_v5(&EVENT_ID_NAMESPACE, b"msg-same").to_string()
    );
}

#[tokio::test]
async fn identity_is_the_stored_numeric_id_a_renamed_channel_matches_a_mismatch_and_a_missing_id_do_not(
) {
    // Renamed: the binding's login is stale/different, the id is the same -> still lifts.
    let r = rig(
        vec![((7, 9, 33), Ok(bound("old_name", Some("141"))))],
        Ok(false),
        Ok(UUID.to_string()),
        true,
        false,
    );
    let out = run(
        &r,
        &entry(7, 9, 33),
        &sub("141"),
        &ev("141", "brand_new_name", "777"),
        TS,
    )
    .await;
    assert_eq!(out, Outcome::Forwarded);
    // The credential/Helix lookup used the binding's login, not the payload's.
    assert_eq!(r.bans.1.lock().unwrap()[0].1, "old_name");

    // Same login, different id (recycled login / forged payload) -> refused, nothing queued.
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", Some("141"))))],
        Ok(false),
        Ok(UUID.to_string()),
        true,
        false,
    );
    let out = run(
        &r,
        &entry(7, 9, 33),
        &sub("999"),
        &ev("999", "alice", "777"),
        TS,
    )
    .await;
    assert_eq!(out, Outcome::Ignored("broadcaster_mismatch"));
    assert!(r.sink.pushed.lock().unwrap().is_empty());

    // The binding has no stored id: refused (fail closed), never falling back to the login.
    let r = rig(
        vec![((7, 9, 33), Ok(bound("alice", None)))],
        Ok(false),
        Ok(UUID.to_string()),
        true,
        false,
    );
    let out = run(
        &r,
        &entry(7, 9, 33),
        &sub("141"),
        &ev("141", "alice", "777"),
        TS,
    )
    .await;
    assert_eq!(out, Outcome::Ignored("binding_id_missing"));
    assert!(r.sink.pushed.lock().unwrap().is_empty());
    assert!(
        r.bans.1.lock().unwrap().is_empty(),
        "no Helix call without an identity"
    );
}

#[tokio::test]
async fn waiters_that_asked_before_a_fresh_read_started_are_coalesced_into_it() {
    let inner = CountingLedger::new(Some("secret-1"));
    let cache = Arc::new(CachedLedger::new(Arc::clone(&inner)));
    // Hold the key's gate so every caller records `asked` and queues before any read starts.
    let gate = Arc::clone(
        cache
            .flights
            .lock()
            .unwrap()
            .entry("key-12345678".to_string())
            .or_default(),
    );
    let held = gate.lock().await;
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let c = Arc::clone(&cache);
        tasks.push(tokio::spawn(async move {
            c.lookup_fresh("key-12345678").await.unwrap().is_some()
        }));
    }
    tokio::time::sleep(Duration::from_millis(30)).await;
    drop(held);
    for t in tasks {
        assert!(t.await.unwrap());
    }
    assert_eq!(
        inner.hits(),
        1,
        "all eight asked before the read started: one hub call"
    );
}

#[tokio::test]
async fn a_fresh_read_that_started_before_the_caller_asked_is_not_reused() {
    let inner = CountingLedger::new(Some("secret-1"));
    inner
        .delay_ms
        .store(80, std::sync::atomic::Ordering::SeqCst);
    let cache = Arc::new(CachedLedger::new(Arc::clone(&inner)));
    let c1 = Arc::clone(&cache);
    let first = tokio::spawn(async move { c1.lookup_fresh("key-12345678").await });
    // Let read #1 start, then (as if a revocation just happened) ask again mid-flight.
    tokio::time::sleep(Duration::from_millis(30)).await;
    *inner.secret.lock().unwrap() = None;
    let second = cache.lookup_fresh("key-12345678").await.unwrap();
    first.await.unwrap().unwrap();
    assert!(
        second.is_none(),
        "the second caller must see the post-revocation row, not read #1's pre-revocation answer"
    );
    assert_eq!(
        inner.hits(),
        2,
        "read #1 started before the second caller asked: a new read"
    );
}

#[tokio::test]
async fn plain_cached_entries_never_satisfy_a_fresh_read() {
    let inner = CountingLedger::new(Some("secret-1"));
    let cache = CachedLedger::new(Arc::clone(&inner));
    cache.lookup("key-12345678").await.unwrap();
    assert_eq!(inner.hits(), 1);
    cache.lookup_fresh("key-12345678").await.unwrap();
    assert_eq!(inner.hits(), 2, "a fresh read always goes to hub-api");
}

#[tokio::test]
async fn forged_signatures_cannot_force_repeated_eviction_refetches() {
    let inner = CountingLedger::new(Some("secret-1"));
    let cache = CachedLedger::new(Arc::clone(&inner));
    cache.lookup("key-12345678").await.unwrap();
    for _ in 0..20 {
        cache.invalidate("key-12345678");
        cache.lookup("key-12345678").await.unwrap();
    }
    assert!(
        inner.hits() <= 2,
        "20 forged-signature retries cost at most one extra hub read, got {}",
        inner.hits()
    );
}

const T_UUID: &str = "0a0a0a0a-0000-4000-8000-000000000001";
const C_UUID: &str = "0b0b0b0b-0000-4000-8000-000000000002";
const N_UUID: &str = "0c0c0c0c-0000-4000-8000-000000000003";

fn secret_body(t: Value, c: Value, n: Value) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "success": true, "secret": "s3cret", "tenant_id": t, "community_id": c, "connection_id": n
    }))
    .unwrap()
}

/// [`OwnerIdResolver`] double: answers `found` and records the owners it was asked for.
struct FakeIds {
    found: Option<OwnerIds>,
    asked: Mutex<Vec<LedgerOwners>>,
}

impl OwnerIdResolver for FakeIds {
    fn resolve<'a>(
        &'a self,
        owners: LedgerOwners,
    ) -> BoxFut<'a, Result<Option<OwnerIds>, LedgerError>> {
        self.asked.lock().unwrap().push(owners);
        let found = self.found;
        Box::pin(async move { Ok(found) })
    }
}

fn fake_ids(found: Option<OwnerIds>) -> FakeIds {
    FakeIds {
        found,
        asked: Mutex::new(Vec::new()),
    }
}

#[test]
fn secret_body_owners_are_public_uuids() {
    let (secret, owners) =
        parse_ledger_body(&secret_body(json!(T_UUID), json!(C_UUID), json!(N_UUID))).unwrap();
    assert_eq!(secret.expose(), "s3cret");
    assert_eq!(owners.tenant.to_string(), T_UUID);
    assert_eq!(owners.community.to_string(), C_UUID);
    assert_eq!(owners.connection.to_string(), N_UUID);
}

/// Regression (PR #1514): a numeric PK in any owner field is refused, never coerced.
#[test]
fn secret_body_with_numeric_ids_is_refused() {
    let nil = Uuid::nil().to_string();
    for body in [
        secret_body(json!(7), json!(C_UUID), json!(N_UUID)),
        secret_body(json!(T_UUID), json!(70), json!(N_UUID)),
        secret_body(json!(T_UUID), json!(C_UUID), json!(700)),
        secret_body(json!("7"), json!(C_UUID), json!(N_UUID)),
        secret_body(json!(T_UUID), json!(nil), json!(N_UUID)),
        secret_body(json!(T_UUID), json!(C_UUID), Value::Null),
    ] {
        assert_eq!(
            parse_ledger_body(&body).err(),
            Some(LedgerError::Unavailable)
        );
    }
    let no_secret = serde_json::to_vec(&json!({
        "success": true, "secret": "", "tenant_id": T_UUID, "community_id": C_UUID,
        "connection_id": N_UUID
    }))
    .unwrap();
    assert_eq!(
        parse_ledger_body(&no_secret).err(),
        Some(LedgerError::Unavailable)
    );
}

#[tokio::test]
async fn entry_resolves_owner_uuids_to_numeric_keys() {
    let ids = fake_ids(Some(OwnerIds {
        tenant_id: 7,
        community_id: 70,
        connection_id: 700,
    }));
    let body = secret_body(json!(T_UUID), json!(C_UUID), json!(N_UUID));
    let e = entry_from_body(&body, &ids).await.unwrap();
    assert_eq!((e.tenant_id, e.community_id, e.connection_id), (7, 70, 700));
    assert_eq!(e.secret.expose(), "s3cret");
    let asked = ids.asked.lock().unwrap();
    assert_eq!(asked.len(), 1);
    assert_eq!(asked[0].community.to_string(), C_UUID);
    assert_eq!(asked[0].connection.to_string(), N_UUID);
}

#[tokio::test]
async fn unresolved_owners_fail_closed() {
    let body = secret_body(json!(T_UUID), json!(C_UUID), json!(N_UUID));
    let before = count("error", "owner_ids_unresolved");
    assert_eq!(
        entry_from_body(&body, &fake_ids(None)).await.err(),
        Some(LedgerError::Unavailable)
    );
    assert!(count("error", "owner_ids_unresolved") > before);
    let zero = fake_ids(Some(OwnerIds {
        tenant_id: 0,
        community_id: 70,
        connection_id: 700,
    }));
    assert_eq!(
        entry_from_body(&body, &zero).await.err(),
        Some(LedgerError::Unavailable)
    );
}

#[test]
fn owner_sql_never_reads_tenants_and_requires_one_tenant() {
    let sql = OWNER_IDS_SQL.to_lowercase();
    assert!(!sql.contains("from tenants") && !sql.contains("join tenants"));
    assert!(sql.contains("pc.tenant_id = c.tenant_id"));
    assert!(!sql.contains("access_token") && !sql.contains("refresh_token"));
}
