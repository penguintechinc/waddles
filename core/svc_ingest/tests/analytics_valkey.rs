//! Real-Valkey test of the analytics producer (`src/analytics.rs`): flag ON, a stamped token and a
//! tokenized raw id both land on `waddles:analytics` with the exact eight-field wire shape, no raw
//! id/handle/text reaches the stream, and `MAXLEN ~` bounds it. Needs `VALKEY_URL` (unset FAILS,
//! never skips), like the other real-Valkey tests in this crate.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use penguin_spine::PlatformEvent;
use svc_ingest::analytics::{
    register_metrics, spawn_worker, AnalyticsPublisher, Settings, TokenizeFail, UserTokenizer,
    ValkeySink,
};

const STAMPED: &str = "0b6f3f9e-8f7e-4c75-9b1f-0d6a4a2f1c11";
const MINTED: &str = "5d1c2a44-3b0e-4f8e-8a5a-2c9d7e6b1a22";

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A per-test stream key (pid + nonce + process counter) so tests sharing one Valkey, in parallel
/// or across earlier runs, can never see each other's entries.
fn unique_stream() -> String {
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    format!(
        "test:analytics:{}:{nonce}:{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed)
    )
}

/// DELs the test's stream key before (on creation) and after (on drop), pass or panic.
struct StreamGuard {
    client: redis::Client,
    key: String,
}

impl StreamGuard {
    fn new(client: redis::Client) -> Self {
        let g = Self {
            client,
            key: unique_stream(),
        };
        g.del();
        g
    }

    fn del(&self) {
        if let Ok(mut c) = self.client.get_connection() {
            let _: Result<i64, _> = redis::cmd("DEL").arg(&self.key).query(&mut c);
        }
    }
}

impl Drop for StreamGuard {
    fn drop(&mut self) {
        self.del();
    }
}

struct Tok;
impl UserTokenizer for Tok {
    async fn tokenize(
        &self,
        _tenant_id: i64,
        _community_id: i64,
        _platform: &'static str,
        raw_id: &str,
    ) -> Result<String, TokenizeFail> {
        if raw_id == "404" {
            Err(TokenizeFail::NotFound)
        } else {
            Ok(MINTED.to_string())
        }
    }
}

fn event(stamped: Option<&str>, raw: Option<&str>) -> PlatformEvent {
    let mut payload = serde_json::Map::new();
    payload.insert("text".into(), "hello secret text".into());
    if let Some(s) = stamped {
        payload.insert("user_uuid".into(), s.into());
    }
    if let Some(r) = raw {
        payload.insert("user_id".into(), r.into());
    }
    PlatformEvent {
        platform: "twitch".into(),
        event_type: "chat.message".into(),
        actor: Some("secret_handle".into()),
        payload,
        occurred_at: "2026-11-18T12:34:56.789Z".into(),
        source: None,
    }
}

fn settings(maxlen: usize) -> Settings {
    Settings {
        maxlen,
        timeout: Duration::from_secs(2),
        queue: 8192,
        tokenize_timeout: Duration::from_secs(2),
        concurrency: 4,
        cache_capacity: 64,
    }
}

async fn run(
    stream_events: Vec<PlatformEvent>,
    maxlen: usize,
) -> (redis::aio::ConnectionManager, u64, StreamGuard) {
    let url = std::env::var("VALKEY_URL").expect("VALKEY_URL must be set: needs a REAL Valkey");
    let client = redis::Client::open(url).unwrap();
    let conn = client.get_connection_manager().await.unwrap();
    let guard = StreamGuard::new(client.clone());
    let metrics = register_metrics(&prometheus::Registry::new());
    let (publisher, rx, warn) = AnalyticsPublisher::new(
        Arc::new(AtomicBool::new(true)),
        settings(maxlen).queue,
        metrics.clone(),
    );
    let sink = ValkeySink::new(client, Duration::from_secs(2)).with_stream(guard.key.clone());
    let worker = spawn_worker(sink, Tok, rx, settings(maxlen), metrics.clone(), warn);
    for e in &stream_events {
        publisher.observe(e, Some((7, 42)));
    }
    drop(publisher);
    worker.await.unwrap();
    (conn, metrics.published.get(), guard)
}

#[tokio::test(flavor = "multi_thread")]
async fn entries_have_the_exact_wire_shape_and_no_pii() {
    let (mut conn, published, guard) = run(
        vec![
            event(Some(STAMPED), Some("111")),
            event(None, Some("222")),
            event(None, Some("404")),
            event(None, None),
        ],
        10_000,
    )
    .await;
    assert_eq!(
        published, 2,
        "stamped + tokenized publish; not-found and no-id skip"
    );
    let reply: redis::streams::StreamRangeReply = redis::cmd("XRANGE")
        .arg(&guard.key)
        .arg("-")
        .arg("+")
        .query_async(&mut conn)
        .await
        .unwrap();
    assert_eq!(reply.ids.len(), 2);
    let mut users = Vec::new();
    for entry in &reply.ids {
        let mut keys: Vec<_> = entry.map.keys().cloned().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "community_id",
                "kind_code",
                "platform_code",
                "provenance_code",
                "tenant_id",
                "ts_ms",
                "user_uuid",
                "v"
            ]
        );
        let get = |k: &str| -> String { redis::from_redis_value(entry.map[k].clone()).unwrap() };
        assert_eq!(get("v"), "1");
        assert_eq!(get("tenant_id"), "7");
        assert_eq!(get("community_id"), "42");
        assert_eq!(get("platform_code"), "3");
        assert_eq!(get("provenance_code"), "3");
        assert_eq!(get("kind_code"), "1");
        assert_eq!(get("ts_ms"), "1795005296789");
        users.push(get("user_uuid"));
        let all = format!("{:?}", entry.map);
        for banned in ["secret_handle", "secret text", "111", "222"] {
            assert!(!all.contains(banned), "{banned} leaked onto the stream");
        }
    }
    users.sort();
    assert_eq!(users, [STAMPED.to_string(), MINTED.to_string()]);
}

#[tokio::test(flavor = "multi_thread")]
async fn maxlen_bounds_the_stream() {
    let events: Vec<_> = (0..5_000).map(|_| event(Some(STAMPED), None)).collect();
    let (mut conn, published, guard) = run(events, 1_000).await;
    assert_eq!(published, 5_000);
    let len: u64 = redis::cmd("XLEN")
        .arg(&guard.key)
        .query_async(&mut conn)
        .await
        .unwrap();
    assert!(
        len >= 1_000,
        "MAXLEN ~ never trims below the cap, got {len}"
    );
    assert!(len < 2_500, "stream must be bounded by MAXLEN ~, got {len}");
}
