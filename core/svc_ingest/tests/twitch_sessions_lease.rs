//! Real-Valkey tests for `ValkeyLease` (acquire/renew/release exclusivity, TTL expiry, fail-closed
//! on a blackholed Valkey). Own test binary (own process) so no unit test that mutates
//! `VALKEY_URL` can race it. Needs `VALKEY_URL` (a real Valkey); an unset var FAILS, never skips.

use std::time::Duration;

use svc_ingest::twitch_sessions::{SessionLease, ValkeyLease};

fn url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: lease tests need a REAL Valkey (no silent skip)")
}

async fn lease_with(id: &str) -> ValkeyLease {
    let client = redis::Client::open(url()).expect("client");
    let cfg =
        redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(2)));
    let conn = client
        .get_multiplexed_async_connection_with_config(&cfg)
        .await
        .expect("connect");
    ValkeyLease::with_replica_id(conn, id.to_string())
}

fn fresh_key() -> (i64, i64) {
    let u = uuid::Uuid::new_v4().as_u128();
    ((u >> 64) as i64 & i64::MAX, u as i64 & i64::MAX)
}

#[tokio::test]
async fn acquire_is_exclusive_and_renewable() {
    let a = lease_with("replica-a").await;
    let k = fresh_key();
    assert!(a.acquire(k, Duration::from_secs(5)).await.expect("acquire"));
    assert!(!a.acquire(k, Duration::from_secs(5)).await.expect("again"));
    assert!(a.renew(k, Duration::from_secs(5)).await.expect("renew"));
    a.release(k).await.expect("release");
}

#[tokio::test]
async fn second_replica_cannot_acquire_a_held_lease() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = fresh_key();
    assert!(a.acquire(k, Duration::from_secs(5)).await.expect("a"));
    assert!(!b.acquire(k, Duration::from_secs(5)).await.expect("b"));
    a.release(k).await.expect("release");
    assert!(b.acquire(k, Duration::from_secs(5)).await.expect("b after"));
    b.release(k).await.expect("release");
}

#[tokio::test]
async fn renew_by_non_owner_returns_false_and_does_not_extend() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = fresh_key();
    assert!(a.acquire(k, Duration::from_millis(400)).await.expect("a"));
    assert!(!b.renew(k, Duration::from_secs(60)).await.expect("b renew"));
    tokio::time::sleep(Duration::from_millis(700)).await;
    // Not extended: lease lapsed, so b can now take it.
    assert!(b.acquire(k, Duration::from_secs(5)).await.expect("b"));
    // a's renew now reports lost ownership.
    assert!(!a.renew(k, Duration::from_secs(5)).await.expect("a renew"));
    b.release(k).await.expect("release");
}

#[tokio::test]
async fn release_only_deletes_own_lease() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = fresh_key();
    assert!(a.acquire(k, Duration::from_secs(5)).await.expect("a"));
    b.release(k).await.expect("b release is a no-op");
    assert!(!b
        .acquire(k, Duration::from_secs(5))
        .await
        .expect("still held"));
    a.release(k).await.expect("a release");
    assert!(b.acquire(k, Duration::from_secs(5)).await.expect("free"));
    b.release(k).await.expect("release");
}

#[tokio::test]
async fn lease_expires_after_ttl_and_can_be_taken() {
    let a = lease_with("replica-a").await;
    let b = lease_with("replica-b").await;
    let k = fresh_key();
    assert!(a.acquire(k, Duration::from_millis(200)).await.expect("a"));
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(b.acquire(k, Duration::from_secs(5)).await.expect("b"));
    b.release(k).await.expect("release");
}

#[tokio::test]
async fn valkey_error_is_lease_error_not_acquired() {
    // Proxy to the real Valkey that can be blackholed after connect: the
    // handshake succeeds, then every command times out.
    let target = redis::Client::open(url())
        .expect("client")
        .get_connection_info()
        .addr()
        .to_string();
    let blackhole = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("addr");
    let flag = blackhole.clone();
    tokio::spawn(async move {
        while let Ok((mut inbound, _)) = listener.accept().await {
            let flag = flag.clone();
            let target = target.clone();
            tokio::spawn(async move {
                let Ok(mut out) = tokio::net::TcpStream::connect(target).await else {
                    return;
                };
                let (mut ir, mut iw) = inbound.split();
                let (mut or, mut ow) = out.split();
                let up = async {
                    let mut buf = [0u8; 4096];
                    loop {
                        match tokio::io::AsyncReadExt::read(&mut ir, &mut buf).await {
                            Ok(n) if n > 0 => {
                                if !flag.load(std::sync::atomic::Ordering::SeqCst) {
                                    let _ = tokio::io::AsyncWriteExt::write_all(&mut ow, &buf[..n])
                                        .await;
                                }
                            }
                            _ => break,
                        }
                    }
                };
                let down = async {
                    let mut buf = [0u8; 4096];
                    loop {
                        match tokio::io::AsyncReadExt::read(&mut or, &mut buf).await {
                            Ok(n) if n > 0 => {
                                let _ =
                                    tokio::io::AsyncWriteExt::write_all(&mut iw, &buf[..n]).await;
                            }
                            _ => break,
                        }
                    }
                };
                tokio::join!(up, down);
            });
        }
    });
    let client = redis::Client::open(format!("redis://{addr}/")).expect("client");
    // Connection built with NO response timeout: only the lease's own
    // enforcement can bound the blackholed commands.
    let conn = client
        .get_multiplexed_async_connection()
        .await
        .expect("connect");
    let l = ValkeyLease::with_replica_id(conn, "r".to_string());
    let k = fresh_key();
    // Sanity: the path works before the blackhole.
    assert!(l.acquire(k, Duration::from_secs(5)).await.expect("pre"));
    l.release(k).await.expect("pre release");
    blackhole.store(true, std::sync::atomic::Ordering::SeqCst);
    let started = std::time::Instant::now();
    assert!(l.acquire(k, Duration::from_secs(5)).await.is_err());
    assert!(
        started.elapsed()
            < svc_ingest::twitch_sessions::lease::RESPONSE_TIMEOUT + Duration::from_secs(1)
    );
    assert!(l.renew(k, Duration::from_secs(5)).await.is_err());
    assert!(l.release(k).await.is_err());
}

#[tokio::test]
async fn new_gives_each_lease_a_distinct_random_replica_id() {
    // `ValkeyLease::new` (the production constructor) must mint a per-boot random id: two
    // instances are two replicas, so the second can never take a lease the first holds.
    let client = redis::Client::open(url()).expect("client");
    let mut leases = Vec::new();
    for _ in 0..2 {
        let conn = client
            .get_multiplexed_async_connection()
            .await
            .expect("connect");
        leases.push(ValkeyLease::new(conn));
    }
    let k = fresh_key();
    assert!(leases[0]
        .acquire(k, Duration::from_secs(5))
        .await
        .expect("first"));
    assert!(!leases[1]
        .acquire(k, Duration::from_secs(5))
        .await
        .expect("second"));
    leases[0].release(k).await.expect("release");
}
