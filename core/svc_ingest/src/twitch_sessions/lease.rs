//! Per-session Valkey lease so exactly one svc-ingest replica owns each
//! `(community_id, connection_id)` Twitch session.
//!
//! Fail-closed: any Valkey error surfaces as [`LeaseError`], never as an
//! acquired lease, so a partitioned replica cannot double-publish. The key
//! carries numeric ids only and the value is a random per-boot replica UUID
//! (never a hostname, handle or other PII).

use std::time::Duration;

/// Upper bound on any single Valkey round trip. Enforced inside every lease
/// operation (not on the connection) so no caller can stall on a blackhole.
pub const RESPONSE_TIMEOUT: Duration = Duration::from_secs(2);

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
pub trait SessionLease: Send + Sync {
    /// Takes the lease if free (`SET .. NX PX`); `Ok(false)` if held.
    fn acquire(
        &self,
        key: (i64, i64),
        ttl: Duration,
    ) -> impl std::future::Future<Output = Result<bool, LeaseError>> + Send;
    /// Extends the lease only if still ours; `Ok(false)` means it was lost.
    fn renew(
        &self,
        key: (i64, i64),
        ttl: Duration,
    ) -> impl std::future::Future<Output = Result<bool, LeaseError>> + Send;
    /// Deletes the lease only if it is ours.
    fn release(
        &self,
        key: (i64, i64),
    ) -> impl std::future::Future<Output = Result<(), LeaseError>> + Send;
}

/// Runs one Valkey round trip under [`RESPONSE_TIMEOUT`]; an elapsed timeout
/// or a Valkey error is a [`LeaseError`] (fail-closed).
async fn bounded<T>(
    fut: impl std::future::Future<Output = Result<T, redis::RedisError>>,
) -> Result<T, LeaseError> {
    match tokio::time::timeout(RESPONSE_TIMEOUT, fut).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(LeaseError(e.to_string())),
        Err(_) => Err(LeaseError(format!(
            "valkey did not respond within {RESPONSE_TIMEOUT:?}"
        ))),
    }
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
        let res: Option<String> = bounded(
            redis::cmd("SET")
                .arg(lease_key(key))
                .arg(&self.replica_id)
                .arg("NX")
                .arg("PX")
                .arg(ms)
                .query_async(&mut conn),
        )
        .await?;
        Ok(res.is_some())
    }

    async fn renew(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError> {
        let ms = ttl_ms(ttl)?;
        let mut conn = self.conn.clone();
        let n: i64 = bounded(
            self.renew
                .key(lease_key(key))
                .arg(&self.replica_id)
                .arg(ms)
                .invoke_async(&mut conn),
        )
        .await?;
        Ok(n == 1)
    }

    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
        let mut conn = self.conn.clone();
        let _: i64 = bounded(
            self.release
                .key(lease_key(key))
                .arg(&self.replica_id)
                .invoke_async(&mut conn),
        )
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // The Valkey-backed lease tests live in `tests/twitch_sessions_lease.rs` (their own
    // process): unit tests in this binary mutate `VALKEY_URL` (lib.rs spine-env tests), which
    // raced these and made them fail with "VALKEY_URL must be set".
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

#[cfg(test)]
mod send_tests {
    use super::*;

    /// Compile-time proof the trait futures are `Send` (needed by `tokio::spawn`).
    #[test]
    fn lease_futures_are_send() {
        fn assert_send<T: Send>(_: &T) {}
        fn check<L: SessionLease>(l: &L) {
            assert_send(&l.acquire((1, 2), Duration::from_secs(1)));
            assert_send(&l.renew((1, 2), Duration::from_secs(1)));
            assert_send(&l.release((1, 2)));
        }
        let _ = check::<ValkeyLease>;
    }
}
