//! Real-Valkey tests for `ConnectorValkeyLease` (exclusivity, renewal,
//! release ownership, key namespace). Own test binary. Needs `VALKEY_URL`
//! (a real Valkey); an unset var FAILS, never skips.

use std::time::Duration;

use svc_ingest::connector_runtime::lease::{lease_tuple, ConnectorValkeyLease};
use svc_ingest::connector_runtime::ConnKey;
use svc_ingest::twitch_sessions::{SessionLease, ValkeyLease};

fn url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: lease tests need a REAL Valkey (no silent skip)")
}

async fn conn() -> redis::aio::ConnectionManager {
    let client = redis::Client::open(url()).expect("client");
    let cfg = redis::aio::ConnectionManagerConfig::new()
        .set_response_timeout(Some(Duration::from_secs(2)));
    client
        .get_connection_manager_with_config(cfg)
        .await
        .expect("connect")
}

async fn lease_with(id: &str) -> ConnectorValkeyLease {
    ConnectorValkeyLease::with_replica_id(conn().await, id.to_owned())
}

fn fresh_key(platform: &'static str) -> ConnKey {
    let u = uuid::Uuid::new_v4().as_u128();
    ConnKey {
        platform,
        community_id: 1,
        connection_id: u as i64 & i64::MAX,
        lane: Default::default(),
    }
}

#[tokio::test]
async fn acquire_is_exclusive_renewable_and_owner_checked() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = lease_tuple(&fresh_key("discord")).expect("known platform");
    let ttl = Duration::from_secs(5);
    assert!(a.acquire(k, ttl).await.expect("a acquires"));
    assert!(!a.acquire(k, ttl).await.expect("again"));
    assert!(!b.acquire(k, ttl).await.expect("b denied"));
    assert!(a.renew(k, ttl).await.expect("a renews"));
    assert!(!b.renew(k, ttl).await.expect("non-owner renew"));
    b.release(k).await.expect("non-owner release is a no-op");
    assert!(!b.acquire(k, ttl).await.expect("still held by a"));
    a.release(k).await.expect("a releases");
    assert!(b.acquire(k, ttl).await.expect("b after release"));
    b.release(k).await.expect("cleanup");
}

#[tokio::test]
async fn the_lease_expires_without_renewal() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = lease_tuple(&fresh_key("youtube")).expect("known platform");
    assert!(a.acquire(k, Duration::from_millis(300)).await.expect("a"));
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(b
        .acquire(k, Duration::from_secs(5))
        .await
        .expect("b after expiry"));
    assert!(!a.renew(k, Duration::from_secs(5)).await.expect("a lost it"));
    b.release(k).await.expect("cleanup");
}

#[tokio::test]
async fn connector_keys_never_collide_with_legacy_twitch_session_keys() {
    let connector = lease_with("replica-a").await;
    let legacy = ValkeyLease::with_replica_id(conn().await, "legacy".to_owned());
    let conn_key = fresh_key("twitch");
    let tuple = lease_tuple(&conn_key).expect("known platform");
    // The legacy lease for the "same-looking" tuple lives in another key space.
    assert!(legacy
        .acquire(tuple, Duration::from_secs(5))
        .await
        .expect("legacy"));
    assert!(connector
        .acquire(tuple, Duration::from_secs(5))
        .await
        .expect("connector"));
    legacy.release(tuple).await.expect("cleanup");
    connector.release(tuple).await.expect("cleanup");
}

#[tokio::test]
async fn an_unknown_platform_namespace_is_an_error_not_an_acquisition() {
    let a = lease_with("replica-a").await;
    assert!(a.acquire((99, 1), Duration::from_secs(1)).await.is_err());
    assert!(a.renew((99, 1), Duration::from_secs(1)).await.is_err());
    assert!(a.release((99, 1)).await.is_err());
    assert!(a.acquire((1, 1), Duration::ZERO).await.is_err());
}
