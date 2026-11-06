//! Real-Valkey proof of the IDENTIFY budget (connector runtime spec 5.4, row
//! I): parity with the legacy `ingest::discord_identify_budget` guarantees --
//! a burst across replicas never exceeds the budget, the window resets,
//! exhaustion is a hard stop -- plus the NEW fail-closed outage behaviour,
//! hash-only keys, and the `/gateway/bot` seed through the SSRF-guarded
//! client. Every test asserts a non-zero denominator before comparing.
//!
//! Valkey runs in a throwaway Docker container (per-test unique name via the
//! shared `core/test_support` helper); the `/gateway/bot` upstream is a real
//! loopback HTTP server (wiremock), not a hand-rolled stub.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::{ConnectionRef, SecretString};
use prometheus::Registry;
use redis::aio::MultiplexedConnection;
use svc_ingest::connector::identify_budget::{
    AcquireOutcome, BotIdentity, BudgetError, DiscordIdentifyGate, GatewayBotSource,
    GatewayInfoSource, IdentifyBudget, IdentifyBudgetMetrics, RedisIdentifyBudget,
    SessionStartLimit, MIN_RESET,
};
use svc_ingest::connector::secrets::{Destination, ResolvedAuth, TransportSecretSource};
use svc_ingest::connector::webhook::{BoxFut, SecretError};
use svc_ingest::connector::{GateCtx, GateError, HandshakeGate};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container, DockerContainer};

const VALKEY_IMAGE: &str = "valkey/valkey:8.1.10-alpine";
const TOKEN: &str = "Bot-Token-SECRET-abc123";

async fn valkey() -> (DockerContainer, String) {
    let c = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(60),
    )
    .await;
    let url = format!("redis://{}:{}", c.host(), c.port());
    (c, url)
}

async fn conn(url: &str) -> MultiplexedConnection {
    redis::Client::open(url)
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap()
}

/// One budget handle = one "replica" with its OWN connection to the shared
/// Valkey.
async fn replica(url: &str, window: Duration) -> RedisIdentifyBudget {
    RedisIdentifyBudget::new(conn(url).await, "discord").with_window(window)
}

fn limit(total: u32, remaining: u32, mc: u32) -> SessionStartLimit {
    SessionStartLimit::new(total, remaining, Duration::from_secs(3600), mc).unwrap()
}

async fn hget(url: &str, key_suffix: &str, field: &str) -> Option<String> {
    let mut c = conn(url).await;
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg("*")
        .query_async(&mut c)
        .await
        .unwrap();
    let k = keys
        .iter()
        .find(|k| k.ends_with(key_suffix))
        .unwrap_or_else(|| panic!("no key ending {key_suffix}: {keys:?}"));
    redis::cmd("HGET")
        .arg(k)
        .arg(field)
        .query_async(&mut c)
        .await
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_burst_across_replicas_never_exceeds_the_per_bucket_window() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    // Long window: within the test a bucket can be granted exactly once.
    let seed = replica(&url, Duration::from_secs(30)).await;
    seed.record_session_start_limit(&id, &limit(1000, 1000, 2))
        .await
        .unwrap();

    let mut tasks = Vec::new();
    for r in 0..4u32 {
        let b = Arc::new(replica(&url, Duration::from_secs(30)).await);
        for s in 0..10u32 {
            let (b, id) = (Arc::clone(&b), id.clone());
            tasks.push(tokio::spawn(async move {
                b.acquire(&id, r * 10 + s).await.unwrap()
            }));
        }
    }
    let mut granted = 0;
    let mut waited = 0;
    for t in tasks {
        match t.await.unwrap() {
            AcquireOutcome::Granted => granted += 1,
            AcquireOutcome::Wait(d) => {
                assert!(d > Duration::ZERO && d <= Duration::from_secs(30));
                waited += 1;
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(
        granted + waited,
        40,
        "denominator: all 40 attempts accounted"
    );
    // max_concurrency = 2 buckets, one IDENTIFY per bucket per window.
    assert_eq!(granted, 2, "exactly one grant per bucket");
    assert_eq!(waited, 38);
    // Wait never touches the daily counter: only the 2 grants decremented it.
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("998")
    );
}

#[tokio::test]
async fn the_window_resets_and_the_bucket_grants_again() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_millis(300)).await;
    b.record_session_start_limit(&id, &limit(1000, 1000, 1))
        .await
        .unwrap();
    assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);
    match b.acquire(&id, 0).await.unwrap() {
        AcquireOutcome::Wait(d) => assert!(d <= Duration::from_millis(300)),
        other => panic!("expected Wait, got {other:?}"),
    }
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("998")
    );
}

#[tokio::test]
async fn shards_map_to_buckets_by_modulo_max_concurrency() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_secs(30)).await;
    b.record_session_start_limit(&id, &limit(1000, 1000, 4))
        .await
        .unwrap();
    // 0..4 are four distinct buckets (all granted); 4 collides with 0, 7 with 3.
    for s in 0..4 {
        assert_eq!(
            b.acquire(&id, s).await.unwrap(),
            AcquireOutcome::Granted,
            "shard {s}"
        );
    }
    assert!(matches!(
        b.acquire(&id, 4).await.unwrap(),
        AcquireOutcome::Wait(_)
    ));
    assert!(matches!(
        b.acquire(&id, 7).await.unwrap(),
        AcquireOutcome::Wait(_)
    ));
}

#[tokio::test]
async fn the_daily_counter_is_a_hard_stop_and_never_goes_negative() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_millis(30)).await;
    b.record_session_start_limit(&id, &limit(1000, 2, 1))
        .await
        .unwrap();
    for _ in 0..2 {
        assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);
        tokio::time::sleep(Duration::from_millis(60)).await;
    }
    for _ in 0..5 {
        match b.acquire(&id, 0).await.unwrap() {
            AcquireOutcome::Exhausted { retry_after } => {
                assert!(retry_after >= MIN_RESET && retry_after <= Duration::from_secs(3600));
            }
            other => panic!("expected Exhausted, got {other:?}"),
        }
    }
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("0")
    );
}

#[tokio::test]
async fn the_daily_counter_expires_at_reset_and_must_be_reseeded() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_millis(30)).await;
    let l = SessionStartLimit::new(10, 10, Duration::from_millis(1), 1).unwrap();
    assert_eq!(l.reset_after, MIN_RESET, "reset floored to 1s");
    b.record_session_start_limit(&id, &l).await.unwrap();
    assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);
    tokio::time::sleep(Duration::from_millis(1300)).await;
    assert_eq!(
        b.acquire(&id, 0).await.unwrap(),
        AcquireOutcome::Unseeded,
        "an expired window is unseeded (fail closed), not unlimited"
    );
}

#[tokio::test]
async fn an_unseeded_counter_is_refused_and_creates_no_state() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_secs(5)).await;
    assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Unseeded);
    let mut c = conn(&url).await;
    let n: u64 = redis::cmd("DBSIZE").query_async(&mut c).await.unwrap();
    assert_eq!(n, 0);
}

#[tokio::test]
async fn reseeding_keeps_the_lower_remaining_and_alerts_on_low_headroom() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_millis(20)).await;
    assert!(!b
        .record_session_start_limit(&id, &limit(1000, 900, 1))
        .await
        .unwrap());
    for _ in 0..3 {
        assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);
        tokio::time::sleep(Duration::from_millis(40)).await;
    }
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("897")
    );
    // A STALE fetch claiming 899 must not refund the 3 spent slots.
    b.record_session_start_limit(&id, &limit(1000, 899, 1))
        .await
        .unwrap();
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("897")
    );
    // A lower authoritative value wins; 200/1000 is at the 20% alert line.
    assert!(b
        .record_session_start_limit(&id, &limit(1000, 200, 1))
        .await
        .unwrap());
    assert_eq!(
        hget(&url, ":limit", "remaining").await.as_deref(),
        Some("200")
    );
}

#[tokio::test]
async fn keys_contain_only_hashes_and_buckets_never_the_token() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = replica(&url, Duration::from_secs(5)).await;
    b.record_session_start_limit(&id, &limit(1000, 1000, 2))
        .await
        .unwrap();
    assert_eq!(b.acquire(&id, 1).await.unwrap(), AcquireOutcome::Granted);
    let mut c = conn(&url).await;
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg("*")
        .query_async(&mut c)
        .await
        .unwrap();
    assert_eq!(keys.len(), 2, "limit hash + one bucket window: {keys:?}");
    for k in &keys {
        assert!(k.contains(id.as_hex()));
        assert!(!k.contains(TOKEN) && !k.contains("SECRET"), "{k}");
    }
    assert!(keys.iter().any(|k| k.ends_with(":b1")));
}

#[tokio::test]
async fn a_valkey_outage_fails_closed_with_a_typed_retryable_error() {
    let (_c, url) = valkey().await;
    let id = BotIdentity::from_token(TOKEN);
    let b = Arc::new(replica(&url, Duration::from_secs(5)).await);
    b.record_session_start_limit(&id, &limit(1000, 1000, 1))
        .await
        .unwrap();
    assert_eq!(b.acquire(&id, 0).await.unwrap(), AcquireOutcome::Granted);

    // Kill the server under the live connection.
    let mut killer = conn(&url).await;
    let _ = redis::cmd("SHUTDOWN")
        .arg("NOSAVE")
        .query_async::<()>(&mut killer)
        .await;
    tokio::time::sleep(Duration::from_millis(300)).await;

    for shard in 0..3 {
        match b.acquire(&id, shard).await {
            Err(BudgetError::Store(class)) => assert!(!class.is_empty()),
            other => panic!("expected Store error (fail closed), got {other:?}"),
        }
    }
    assert!(matches!(
        b.record_session_start_limit(&id, &limit(1000, 1000, 1))
            .await,
        Err(BudgetError::Store(_))
    ));

    // Through the gate: a typed retryable refusal, never a permit.
    struct Fixed(BotIdentity);
    impl GatewayInfoSource for Fixed {
        fn identity(&self) -> BoxFut<'_, Result<BotIdentity, BudgetError>> {
            let i = self.0.clone();
            Box::pin(async move { Ok(i) })
        }
        fn session_start_limit(&self) -> BoxFut<'_, Result<SessionStartLimit, BudgetError>> {
            Box::pin(async { Ok(limit(1000, 1000, 1)) })
        }
    }
    let reg = Registry::new();
    let m = IdentifyBudgetMetrics::register(&reg).unwrap();
    let gate = DiscordIdentifyGate::new(Arc::clone(&b), Arc::new(Fixed(id)), 0, m);
    let ctx = GateCtx {
        platform: "discord",
        source_id: "s",
        connection_id: "c",
        session_token: None,
    };
    assert_eq!(
        gate.acquire(&ctx).await.unwrap_err(),
        GateError::Retryable {
            reason: "budget store unavailable"
        }
    );
    assert_eq!(counter(&reg, "store_error"), 1);
    assert_eq!(counter(&reg, "granted"), 0);
}

fn counter(reg: &Registry, outcome: &str) -> u64 {
    reg.gather()
        .iter()
        .filter(|f| f.name() == "connector_identify_budget_total")
        .flat_map(|f| f.get_metric().iter())
        .find(|m| m.get_label().iter().any(|l| l.value() == outcome))
        .map_or(0, |m| m.get_counter().value() as u64)
}

/// Loopback `/gateway/bot` + a recording secret source = the real guarded
/// fetch path with a controllable upstream.
struct Secrets {
    token: &'static str,
    calls: AtomicUsize,
    mode: Mode,
}

#[derive(Clone, Copy)]
enum Mode {
    Ok,
    Missing,
    Unavailable,
}

impl TransportSecretSource for Secrets {
    fn resolve(
        &self,
        _r: ConnectionRef,
        _d: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let (mode, token) = (self.mode, self.token);
        Box::pin(async move {
            match mode {
                Mode::Ok => Ok(ResolvedAuth {
                    secret: SecretString::new(token.to_string()),
                    header: None,
                }),
                Mode::Missing => Err(SecretError::Missing),
                Mode::Unavailable => Err(SecretError::Unavailable),
            }
        })
    }
}

fn source(base: &str, mode: Mode) -> (GatewayBotSource<Secrets>, Arc<Secrets>) {
    let secrets = Arc::new(Secrets {
        token: TOKEN,
        calls: AtomicUsize::new(0),
        mode,
    });
    let client = GuardedClient::new(
        GuardedLimits {
            max_redirects: 0,
            timeout: Duration::from_secs(5),
            max_response_bytes: 64 * 1024,
        },
        vec!["127.0.0.1".parse().unwrap()],
    );
    (
        GatewayBotSource::new(
            client,
            Arc::clone(&secrets),
            ConnectionRef {
                community_id: 7,
                connection_id: 12,
            },
            base,
        ),
        secrets,
    )
}

fn gateway_body(remaining: u32, mc: u32) -> String {
    format!(
        r#"{{"url":"wss://gateway.discord.gg","shards":1,"session_start_limit":
        {{"total":1000,"remaining":{remaining},"reset_after":3600000,"max_concurrency":{mc}}}}}"#
    )
}

#[tokio::test]
async fn gateway_bot_fetch_sends_the_bot_token_and_parses_the_limit() {
    let up = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/gateway/bot"))
        .and(header("authorization", format!("Bot {TOKEN}").as_str()))
        .respond_with(ResponseTemplate::new(200).set_body_string(gateway_body(950, 16)))
        .expect(1)
        .mount(&up)
        .await;
    let (src, secrets) = source(&up.uri(), Mode::Ok);
    let l = src.session_start_limit().await.unwrap();
    assert_eq!((l.total, l.remaining, l.max_concurrency), (1000, 950, 16));
    assert_eq!(l.reset_after, Duration::from_secs(3600));
    assert_eq!(secrets.calls.load(Ordering::SeqCst), 1);
    assert_eq!(
        src.identity().await.unwrap(),
        BotIdentity::from_token(TOKEN)
    );
}

#[tokio::test]
async fn gateway_bot_failures_are_typed_and_never_leak_the_token() {
    type Check = fn(&BudgetError) -> bool;
    let cases: Vec<(u16, Check)> = vec![
        (401, |e| matches!(e, BudgetError::Credential(_))),
        (403, |e| matches!(e, BudgetError::Credential(_))),
        (429, |e| matches!(e, BudgetError::LimitUnavailable(_))),
        (500, |e| matches!(e, BudgetError::LimitUnavailable(_))),
    ];
    for (status, check) in cases {
        let up = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(status))
            .mount(&up)
            .await;
        let (src, _) = source(&up.uri(), Mode::Ok);
        let e = src.session_start_limit().await.unwrap_err();
        assert!(check(&e), "status {status} -> {e:?}");
        assert!(!e.to_string().contains(TOKEN) && !format!("{e:?}").contains(TOKEN));
    }
    // malformed 200 body
    let up = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string("{}"))
        .mount(&up)
        .await;
    let (src, _) = source(&up.uri(), Mode::Ok);
    assert!(matches!(
        src.session_start_limit().await,
        Err(BudgetError::LimitUnavailable(_))
    ));
    // secret backend: missing is terminal, unavailable is retryable
    let (src, _) = source("http://127.0.0.1:9", Mode::Missing);
    assert!(matches!(
        src.identity().await,
        Err(BudgetError::Credential(_))
    ));
    let (src, _) = source("http://127.0.0.1:9", Mode::Unavailable);
    assert!(matches!(src.identity().await, Err(BudgetError::Store(_))));
    // an SSRF-forbidden destination is refused by the guard (no allowlist)
    let secrets = Arc::new(Secrets {
        token: TOKEN,
        calls: AtomicUsize::new(0),
        mode: Mode::Ok,
    });
    let strict = GatewayBotSource::new(
        GuardedClient::new(GuardedLimits::default(), vec![]),
        secrets,
        ConnectionRef {
            community_id: 7,
            connection_id: 12,
        },
        "https://169.254.169.254",
    );
    assert!(matches!(
        strict.session_start_limit().await,
        Err(BudgetError::LimitUnavailable(_))
    ));
}

#[tokio::test]
async fn config_fallback_is_used_loudly_only_when_the_fetch_fails_not_on_a_dead_credential() {
    let reg = Registry::new();
    let m = IdentifyBudgetMetrics::register(&reg).unwrap();
    let fb = limit(1000, 500, 1);

    let up = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(500))
        .mount(&up)
        .await;
    let (src, _) = source(&up.uri(), Mode::Ok);
    let src = src.with_config_fallback(fb).with_metrics(m.clone());
    assert_eq!(src.session_start_limit().await.unwrap(), fb);
    assert_eq!(counter(&reg, "limit_fallback"), 1);

    let dead = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&dead)
        .await;
    let (src, _) = source(&dead.uri(), Mode::Ok);
    let src = src.with_config_fallback(fb).with_metrics(m);
    assert!(matches!(
        src.session_start_limit().await,
        Err(BudgetError::Credential(_))
    ));
    assert_eq!(
        counter(&reg, "limit_fallback"),
        1,
        "no fallback for a dead credential"
    );
}

struct Harness {
    _c: DockerContainer,
    url: String,
    reg: Registry,
    up: MockServer,
}

impl Harness {
    async fn new(remaining: u32, mc: u32) -> Self {
        let (c, url) = valkey().await;
        let up = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/gateway/bot"))
            .respond_with(ResponseTemplate::new(200).set_body_string(gateway_body(remaining, mc)))
            .mount(&up)
            .await;
        Self {
            _c: c,
            url,
            reg: Registry::new(),
            up,
        }
    }

    async fn gate(
        &self,
        shard: u32,
        window: Duration,
        max_wait: Duration,
        m: &IdentifyBudgetMetrics,
    ) -> DiscordIdentifyGate<RedisIdentifyBudget, GatewayBotSource<Secrets>> {
        let (src, _) = source(&self.up.uri(), Mode::Ok);
        DiscordIdentifyGate::new(
            Arc::new(replica(&self.url, window).await),
            Arc::new(src),
            shard,
            m.clone(),
        )
        .with_max_wait(max_wait)
    }

    async fn fetches(&self) -> usize {
        self.up.received_requests().await.unwrap().len()
    }
}

fn ctx(token: Option<&str>) -> GateCtx<'_> {
    GateCtx {
        platform: "discord",
        source_id: "s",
        connection_id: "c",
        session_token: token,
    }
}

#[tokio::test]
async fn resume_skips_the_budget_and_identify_consumes_exactly_one_slot() {
    let h = Harness::new(1000, 1).await;
    let m = IdentifyBudgetMetrics::register(&h.reg).unwrap();
    let gate = h
        .gate(0, Duration::from_millis(50), Duration::from_secs(5), &m)
        .await;

    let p = gate
        .acquire(&ctx(Some(r#"{"session_id":"s-1","seq":9}"#)))
        .await
        .unwrap();
    assert!(!p.consumed);
    assert_eq!(
        h.fetches().await,
        0,
        "a RESUME never even fetches the limit"
    );
    assert_eq!(counter(&h.reg, "resume_skipped"), 1);

    let p = gate.acquire(&ctx(None)).await.unwrap();
    assert!(p.consumed);
    assert_eq!(
        h.fetches().await,
        1,
        "the first IDENTIFY seeds from /gateway/bot"
    );
    assert_eq!(
        hget(&h.url, ":limit", "remaining").await.as_deref(),
        Some("999")
    );

    // A session token with no session_id is NOT a resume: it burns a slot.
    tokio::time::sleep(Duration::from_millis(80)).await;
    let p = gate.acquire(&ctx(Some(r#"{"seq":1}"#))).await.unwrap();
    assert!(p.consumed);
    assert_eq!(
        hget(&h.url, ":limit", "remaining").await.as_deref(),
        Some("998")
    );
    assert_eq!(h.fetches().await, 1, "seeded once, not per IDENTIFY");
    assert_eq!(counter(&h.reg, "granted"), 2);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn contention_serializes_per_bucket_and_spends_exactly_one_slot_each() {
    let window = Duration::from_millis(150);
    let h = Harness::new(1000, 4).await;
    let m = IdentifyBudgetMetrics::register(&h.reg).unwrap();
    // 4 buckets x 3 contenders (shards b, b+4, b+8), separate replicas.
    let mut tasks = Vec::new();
    for shard in 0..12u32 {
        let gate = h.gate(shard, window, Duration::from_secs(10), &m).await;
        tasks.push(tokio::spawn(async move {
            let p = gate.acquire(&ctx(None)).await.unwrap();
            (shard % 4, Instant::now(), p)
        }));
    }
    let mut by_bucket: Vec<Vec<Instant>> = vec![Vec::new(); 4];
    let mut n = 0;
    for t in tasks {
        let (b, at, p) = t.await.unwrap();
        assert!(p.consumed);
        by_bucket[b as usize].push(at);
        n += 1;
    }
    assert_eq!(n, 12, "denominator");
    for (b, mut v) in by_bucket.into_iter().enumerate() {
        assert_eq!(v.len(), 3, "bucket {b}");
        v.sort();
        for w in v.windows(2) {
            let gap = w[1] - w[0];
            assert!(
                gap >= window - Duration::from_millis(20),
                "bucket {b} granted twice {gap:?} apart (window {window:?})"
            );
        }
    }
    assert_eq!(
        hget(&h.url, ":limit", "remaining").await.as_deref(),
        Some("988")
    );
    assert_eq!(counter(&h.reg, "granted"), 12);
    assert!(counter(&h.reg, "waited") > 0, "contention must have waited");
    assert!(
        h.fetches().await >= 1,
        "denominator: at least one seed fetch"
    );
}

#[tokio::test]
async fn a_wait_longer_than_max_wait_is_typed_exhaustion_with_retry_after() {
    let h = Harness::new(1000, 1).await;
    let m = IdentifyBudgetMetrics::register(&h.reg).unwrap();
    let gate = h
        .gate(0, Duration::from_secs(30), Duration::from_millis(300), &m)
        .await;
    assert!(gate.acquire(&ctx(None)).await.unwrap().consumed);
    let started = Instant::now();
    match gate.acquire(&ctx(None)).await.unwrap_err() {
        GateError::Exhausted { retry_after } => {
            assert!(
                retry_after > Duration::from_secs(20) && retry_after <= Duration::from_secs(30)
            );
        }
        other => panic!("expected Exhausted, got {other:?}"),
    }
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "must park/back off, not sleep out the window"
    );
    assert_eq!(counter(&h.reg, "exhausted"), 1);
    assert_eq!(
        hget(&h.url, ":limit", "remaining").await.as_deref(),
        Some("999")
    );
}

#[tokio::test]
async fn daily_exhaustion_through_the_gate_is_exhausted_not_a_grant() {
    let h = Harness::new(1, 1).await;
    let m = IdentifyBudgetMetrics::register(&h.reg).unwrap();
    let gate = h
        .gate(0, Duration::from_millis(30), Duration::from_secs(5), &m)
        .await;
    assert!(gate.acquire(&ctx(None)).await.unwrap().consumed);
    tokio::time::sleep(Duration::from_millis(60)).await;
    match gate.acquire(&ctx(None)).await.unwrap_err() {
        GateError::Exhausted { retry_after } => assert!(retry_after >= MIN_RESET),
        other => panic!("expected Exhausted, got {other:?}"),
    }
    assert_eq!(counter(&h.reg, "granted"), 1);
    assert_eq!(counter(&h.reg, "exhausted"), 1);
}

#[tokio::test]
async fn low_headroom_on_seed_is_alerted_and_a_dead_credential_is_fatal() {
    let h = Harness::new(100, 1).await; // 100/1000 = 10% <= 20%
    let m = IdentifyBudgetMetrics::register(&h.reg).unwrap();
    let gate = h
        .gate(0, Duration::from_millis(30), Duration::from_secs(5), &m)
        .await;
    assert!(gate.acquire(&ctx(None)).await.unwrap().consumed);
    assert_eq!(counter(&h.reg, "low_headroom"), 1);

    // dead credential: /gateway/bot 401 -> Fatal (driver parks), nothing granted
    let (c, url) = valkey().await;
    let up = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&up)
        .await;
    let reg = Registry::new();
    let m = IdentifyBudgetMetrics::register(&reg).unwrap();
    let (src, _) = source(&up.uri(), Mode::Ok);
    let gate = DiscordIdentifyGate::new(
        Arc::new(replica(&url, Duration::from_secs(5)).await),
        Arc::new(src),
        0,
        m,
    );
    assert_eq!(
        gate.acquire(&ctx(None)).await.unwrap_err(),
        GateError::Fatal {
            reason: "bot credential rejected"
        }
    );
    assert_eq!(counter(&reg, "credential_rejected"), 1);
    assert_eq!(counter(&reg, "granted"), 0);
    drop(c);
}

#[tokio::test]
async fn an_unavailable_limit_with_no_config_fallback_fails_loud_and_retryable() {
    let (_c, url) = valkey().await;
    let up = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&up)
        .await;
    let reg = Registry::new();
    let m = IdentifyBudgetMetrics::register(&reg).unwrap();
    let (src, _) = source(&up.uri(), Mode::Ok);
    let gate = DiscordIdentifyGate::new(
        Arc::new(replica(&url, Duration::from_secs(5)).await),
        Arc::new(src),
        0,
        m,
    );
    assert_eq!(
        gate.acquire(&ctx(None)).await.unwrap_err(),
        GateError::Retryable {
            reason: "session_start_limit unavailable"
        }
    );
    assert_eq!(counter(&reg, "limit_unavailable"), 1);
    // Nothing was seeded, nothing granted.
    let mut c = conn(&url).await;
    let n: u64 = redis::cmd("DBSIZE").query_async(&mut c).await.unwrap();
    assert_eq!(n, 0);
}
