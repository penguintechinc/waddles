//! `RedisSpectrumStore` against a REAL Valkey (no silent skip): dedupe set with
//! rank-based eviction, the primed flag, snapshot round trip and unique-key
//! isolation. Requires `VALKEY_URL`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::time::Duration;

use svc_ingest::connector::spectrum::{RedisSpectrumStore, SpectrumStore};

const TTL: Duration = Duration::from_secs(60);

async fn store() -> RedisSpectrumStore {
    let url = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: spectrum store tests need a REAL Valkey (no silent skip)");
    let client = redis::Client::open(url).expect("valkey client");
    let conn = client
        .get_connection_manager()
        .await
        .expect("real valkey must be reachable");
    RedisSpectrumStore::new(conn)
}

fn key() -> String {
    format!("test:spectrum:{}", uuid::Uuid::new_v4())
}

fn ids(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| (*s).to_owned()).collect()
}

#[tokio::test]
async fn priming_dedupe_and_eviction_round_trip() {
    let s = store().await;
    let k = key();
    assert!(!s.is_primed(k.clone()).await.unwrap());
    assert_eq!(
        s.unseen(k.clone(), ids(&["a", "b"])).await.unwrap(),
        ["a", "b"],
        "nothing seen yet"
    );
    assert!(s.unseen(k.clone(), Vec::new()).await.unwrap().is_empty());
    s.remember(k.clone(), ids(&["a", "b"]), 2, TTL)
        .await
        .unwrap();
    assert!(s.is_primed(k.clone()).await.unwrap());
    assert_eq!(
        s.unseen(k.clone(), ids(&["a", "b", "c"])).await.unwrap(),
        ["c"]
    );
    // cap 2: adding c evicts the lowest-ranked entry
    tokio::time::sleep(Duration::from_millis(5)).await;
    s.remember(k.clone(), ids(&["c"]), 2, TTL).await.unwrap();
    let unseen = s.unseen(k.clone(), ids(&["a", "b", "c"])).await.unwrap();
    assert_eq!(
        unseen.len(),
        1,
        "exactly one of the two older ids was evicted"
    );
    assert!(!unseen.contains(&"c".to_owned()));
}

#[tokio::test]
async fn an_empty_first_poll_still_primes() {
    let s = store().await;
    let k = key();
    s.remember(k.clone(), Vec::new(), 10, TTL).await.unwrap();
    assert!(s.is_primed(k).await.unwrap(), "an empty window primes too");
}

#[tokio::test]
async fn snapshots_round_trip_and_keys_are_isolated() {
    let s = store().await;
    let (k1, k2) = (key(), key());
    assert_eq!(s.load_snapshot(k1.clone()).await.unwrap(), None);
    s.save_snapshot(k1.clone(), r#"{"v":1}"#.into(), TTL)
        .await
        .unwrap();
    assert_eq!(
        s.load_snapshot(k1).await.unwrap().as_deref(),
        Some(r#"{"v":1}"#)
    );
    assert_eq!(s.load_snapshot(k2.clone()).await.unwrap(), None);
    assert!(!s.is_primed(k2).await.unwrap());
}
