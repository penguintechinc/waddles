//! Per-connection Valkey lease for the connector runtime: exactly one
//! svc-ingest replica supervises each `connector:<platform>:<connection_id>`.
//!
//! The runtime is generic over the legacy [`SessionLease`] trait, whose key is
//! an `(i64, i64)` tuple. The runtime passes `(platform_ns, connection_id)`;
//! this implementation decodes `platform_ns` back to the platform slug and
//! builds the namespaced key, so it can never collide with the legacy
//! `twitch:session:<community>:<connection>` keys. Fail-closed like the
//! legacy lease: any Valkey error is a [`LeaseError`], never an acquisition.

use std::time::Duration;

use super::feed::{ConnKey, Lane};
use crate::twitch_sessions::lease::{LeaseError, SessionLease, RESPONSE_TIMEOUT};

/// Key prefix for connector leases.
const KEY_PREFIX: &str = "waddles:ingest:connector:";

/// `(slug, namespace)` for every platform the runtime may supervise.
const PLATFORMS: [(&str, i64); 6] = [
    ("twitch", 1),
    ("discord", 2),
    ("youtube", 3),
    ("wix", 4),
    ("spectrum", 5),
    ("kick", 6),
];

/// Added to a platform namespace for a binding's [`Lane::Webhook`] transport, so the webhook
/// receiver of a connection holds its OWN lease (`<platform>-webhook:<id>`), never contending
/// with the same connection's primary transport.
const WEBHOOK_LANE_OFFSET: i64 = 100;

const RENEW_LUA: &str = "if redis.call('GET', KEYS[1]) == ARGV[1] then \
return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end";

const RELEASE_LUA: &str = "if redis.call('GET', KEYS[1]) == ARGV[1] then \
return redis.call('DEL', KEYS[1]) else return 0 end";

/// The lease-tuple namespace for `platform`, or `None` for an unsupported slug.
#[must_use]
pub fn platform_ns(platform: &str) -> Option<i64> {
    PLATFORMS
        .iter()
        .find(|(p, _)| *p == platform)
        .map(|(_, ns)| *ns)
}

/// The `SessionLease` tuple for a connection, or `None` for an unsupported platform.
#[must_use]
pub fn lease_tuple(key: &ConnKey) -> Option<(i64, i64)> {
    let lane_offset = match key.lane {
        Lane::Primary => 0,
        Lane::Webhook => WEBHOOK_LANE_OFFSET,
    };
    platform_ns(key.platform).map(|ns| (ns + lane_offset, key.connection_id))
}

/// The Valkey key for a lease tuple built by [`lease_tuple`].
fn valkey_key(t: (i64, i64)) -> Result<String, LeaseError> {
    let (base, suffix) = if t.0 > WEBHOOK_LANE_OFFSET {
        (t.0 - WEBHOOK_LANE_OFFSET, "-webhook")
    } else {
        (t.0, "")
    };
    let (slug, _) = PLATFORMS
        .iter()
        .find(|(_, ns)| *ns == base)
        .ok_or_else(|| LeaseError("unknown connector platform namespace".to_owned()))?;
    Ok(format!("{KEY_PREFIX}{slug}{suffix}:{}", t.1))
}

fn ttl_ms(ttl: Duration) -> Result<u64, LeaseError> {
    match u64::try_from(ttl.as_millis()) {
        Ok(ms) if ms > 0 => Ok(ms),
        _ => Err(LeaseError(format!("invalid lease ttl {ttl:?}"))),
    }
}

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

/// Valkey-backed connector lease identified by a random per-boot replica id.
pub struct ConnectorValkeyLease {
    conn: redis::aio::MultiplexedConnection,
    replica_id: String,
    renew: redis::Script,
    release: redis::Script,
}

impl ConnectorValkeyLease {
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

impl SessionLease for ConnectorValkeyLease {
    async fn acquire(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError> {
        let ms = ttl_ms(ttl)?;
        let k = valkey_key(key)?;
        let mut conn = self.conn.clone();
        let res: Option<String> = bounded(
            redis::cmd("SET")
                .arg(k)
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
        let k = valkey_key(key)?;
        let mut conn = self.conn.clone();
        let n: i64 = bounded(
            self.renew
                .key(k)
                .arg(&self.replica_id)
                .arg(ms)
                .invoke_async(&mut conn),
        )
        .await?;
        Ok(n == 1)
    }

    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
        let k = valkey_key(key)?;
        let mut conn = self.conn.clone();
        let _: i64 = bounded(
            self.release
                .key(k)
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

    #[test]
    fn key_is_namespaced_by_platform_and_carries_numeric_ids_only() {
        let k = ConnKey {
            platform: "discord",
            community_id: 7,
            connection_id: 42,
            lane: Default::default(),
        };
        let t = lease_tuple(&k).expect("known platform");
        assert_eq!(
            valkey_key(t).expect("key"),
            "waddles:ingest:connector:discord:42"
        );
        // Never collides with the legacy twitch session key space.
        assert!(!valkey_key(t).expect("key").contains("twitch:session"));
    }

    #[test]
    fn kick_has_a_lease_and_its_webhook_lane_has_a_distinct_one() {
        let primary = ConnKey {
            platform: "kick",
            community_id: 7,
            connection_id: 42,
            lane: Lane::Primary,
        };
        let hook = ConnKey {
            lane: Lane::Webhook,
            ..primary.clone()
        };
        let (p, h) = (lease_tuple(&primary).unwrap(), lease_tuple(&hook).unwrap());
        assert_ne!(p, h);
        assert_eq!(valkey_key(p).unwrap(), format!("{KEY_PREFIX}kick:42"));
        assert_eq!(
            valkey_key(h).unwrap(),
            format!("{KEY_PREFIX}kick-webhook:42")
        );
    }

    #[test]
    fn unknown_platform_has_no_lease_tuple_and_unknown_ns_is_an_error() {
        let k = ConnKey {
            platform: "slack",
            community_id: 1,
            connection_id: 2,
            lane: Default::default(),
        };
        assert!(lease_tuple(&k).is_none());
        assert!(platform_ns("twitch").is_some());
        assert!(valkey_key((99, 1)).is_err());
        assert!(valkey_key((199, 1)).is_err());
    }

    #[test]
    fn zero_ttl_is_rejected() {
        assert!(ttl_ms(Duration::ZERO).is_err());
        assert_eq!(ttl_ms(Duration::from_secs(2)).expect("ok"), 2000);
    }

    #[test]
    fn lease_futures_are_send() {
        fn assert_send<T: Send>(_: &T) {}
        fn check<L: SessionLease>(l: &L) {
            assert_send(&l.acquire((1, 2), Duration::from_secs(1)));
        }
        let _ = check::<ConnectorValkeyLease>;
    }
}
