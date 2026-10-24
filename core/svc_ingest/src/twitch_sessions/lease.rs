//! Per-session Valkey lease so exactly one svc-ingest replica owns each
//! `(community_id, connection_id)` Twitch session.
//!
//! Fail-closed: any Valkey error surfaces as [`LeaseError`], never as an
//! acquired lease, so a partitioned replica cannot double-publish. The key
//! carries numeric ids only and the value is a random per-boot replica UUID
//! (never a hostname, handle or other PII).

use std::time::Duration;

/// Key prefix for session leases (numeric ids are appended).
const KEY_PREFIX: &str = "waddles:ingest:twitch:session:";

/// Renew only if the stored owner is us: returns 1 on extend, 0 otherwise.
const RENEW_LUA: &str = "if redis.call('GET', KEYS[1]) == ARGV[1] then \
return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end";

/// Delete only if the stored owner is us.
const RELEASE_LUA: &str = "if redis.call('GET', KEYS[1]) == ARGV[1] then \
return redis.call('DEL', KEYS[1]) else return 0 end";

/// A lease operation failed to reach a trustworthy answer from Valkey.
#[derive(Debug, thiserror::Error)]
#[error("session lease error: {0}")]
pub struct LeaseError(pub String);

/// Builds the lease key for `(community_id, connection_id)`.
fn lease_key(key: (i64, i64)) -> String {
    format!("{KEY_PREFIX}{}:{}", key.0, key.1)
}

/// Converts a TTL to whole milliseconds; zero (rejected by `PX`) or overflow
/// is an error rather than a silent clamp.
fn ttl_ms(ttl: Duration) -> Result<u64, LeaseError> {
    match u64::try_from(ttl.as_millis()) {
        Ok(ms) if ms > 0 => Ok(ms),
        _ => Err(LeaseError(format!("invalid lease ttl {ttl:?}"))),
    }
}

/// Replica-singleton ownership of a Twitch session binding.
#[allow(async_fn_in_trait)]
pub trait SessionLease: Send + Sync {
    /// Takes the lease if free (`SET .. NX PX`); `Ok(false)` if held.
    async fn acquire(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError>;
    /// Extends the lease only if still ours; `Ok(false)` means it was lost.
    async fn renew(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError>;
    /// Deletes the lease only if it is ours.
    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError>;
}

/// Valkey-backed [`SessionLease`] identified by a random per-boot replica id.
pub struct ValkeyLease {
    conn: redis::aio::MultiplexedConnection,
    replica_id: String,
    renew: redis::Script,
    release: redis::Script,
}

impl ValkeyLease {
    /// Wraps a connected Valkey connection with a fresh random replica UUID.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self::with_replica_id(conn, uuid::Uuid::new_v4().to_string())
    }

    /// As [`Self::new`] with an explicit replica id (tests simulating peers).
    #[must_use]
    pub fn with_replica_id(conn: redis::aio::MultiplexedConnection, replica_id: String) -> Self {
        Self {
            conn,
            replica_id,
            renew: redis::Script::new(RENEW_LUA),
            release: redis::Script::new(RELEASE_LUA),
        }
    }
}

impl SessionLease for ValkeyLease {
    async fn acquire(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError> {
        let ms = ttl_ms(ttl)?;
        let mut conn = self.conn.clone();
        let res: Option<String> = redis::cmd("SET")
            .arg(lease_key(key))
            .arg(&self.replica_id)
            .arg("NX")
            .arg("PX")
            .arg(ms)
            .query_async(&mut conn)
            .await
            .map_err(|e| LeaseError(e.to_string()))?;
        Ok(res.is_some())
    }

    async fn renew(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError> {
        let ms = ttl_ms(ttl)?;
        let mut conn = self.conn.clone();
        let n: i64 = self
            .renew
            .key(lease_key(key))
            .arg(&self.replica_id)
            .arg(ms)
            .invoke_async(&mut conn)
            .await
            .map_err(|e| LeaseError(e.to_string()))?;
        Ok(n == 1)
    }

    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
        let mut conn = self.conn.clone();
        let _: i64 = self
            .release
            .key(lease_key(key))
            .arg(&self.replica_id)
            .invoke_async(&mut conn)
            .await
            .map_err(|e| LeaseError(e.to_string()))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

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
                                        let _ =
                                            tokio::io::AsyncWriteExt::write_all(&mut ow, &buf[..n])
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
                                    let _ = tokio::io::AsyncWriteExt::write_all(&mut iw, &buf[..n])
                                        .await;
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
        let cfg = redis::AsyncConnectionConfig::new()
            .set_response_timeout(Some(Duration::from_millis(200)));
        let conn = client
            .get_multiplexed_async_connection_with_config(&cfg)
            .await
            .expect("connect");
        let l = ValkeyLease::with_replica_id(conn, "r".to_string());
        let k = fresh_key();
        // Sanity: the path works before the blackhole.
        assert!(l.acquire(k, Duration::from_secs(5)).await.expect("pre"));
        l.release(k).await.expect("pre release");
        blackhole.store(true, std::sync::atomic::Ordering::SeqCst);
        assert!(l.acquire(k, Duration::from_secs(5)).await.is_err());
        assert!(l.renew(k, Duration::from_secs(5)).await.is_err());
        assert!(l.release(k).await.is_err());
    }

    #[test]
    fn key_carries_numeric_ids_only() {
        assert_eq!(
            lease_key((7, 9)),
            "waddles:ingest:twitch:session:7:9".to_string()
        );
    }
}

#[cfg(test)]
mod ttl_tests {
    use super::*;

    #[test]
    fn zero_ttl_is_rejected() {
        assert!(ttl_ms(Duration::ZERO).is_err());
        assert_eq!(ttl_ms(Duration::from_secs(2)).expect("ok"), 2000);
    }
}
