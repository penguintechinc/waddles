//! The narrow set of Valkey operations the `kv` capability needs, behind a
//! trait -- mirrors `core/svc_action::capabilities::RelayQueue`'s own
//! rationale verbatim: easy to fake in unit tests without a live Valkey
//! server, with exactly one production implementation
//! (`redis::aio::MultiplexedConnection`, the same connection type both
//! stages already open for `relay`/usage metering).
//!
//! Every quota-sensitive operation (`set`, `increment`, `delete`) is one
//! Lua `EVAL`, not a check-then-act pair of round trips: two bundles
//! calling `set` on two different new keys for the same app at the same
//! instant must never both be admitted past [`crate::limits::MAX_KEYS_PER_APP`]
//! (a bare `GET count; if below max; INCR` from Rust has exactly that
//! race). Valkey (like Redis) runs a `EVAL` script to completion before
//! serving any other client on the same key slot, so the read-count/
//! write-count pair inside each script below is already atomic without a
//! separate lock.
//!
//! **Known eventual-consistency gap:** [`KvScope::count_key`]'s counter is
//! decremented only by an explicit `delete` running this crate's script --
//! a key that lapses via Valkey's own TTL sweep is never observed here, so
//! the counter can overcount relative to the true live-key set until
//! something next calls `delete` on an already-expired key (a no-op DEL,
//! harmless) or `set`/`increment` reuses the same guest key (which finds
//! `EXISTS` false and correctly re-admits it, but never repays the earlier
//! phantom count). This is a resource ceiling, not a tenant-isolation
//! boundary (`crate::scope::KvScope` is), so a bounded amount of drift
//! toward "looks fuller than it is" is an acceptable failure mode: it can
//! only make quota stricter than intended, never let an app exceed it.

use std::future::Future;
use std::pin::Pin;

use redis::AsyncCommands;

/// Outcome of a quota-checked write (`set`/`increment`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaOutcome<T> {
    Admitted(T),
    QuotaExceeded,
}

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The Valkey primitives `crate::KvHost` needs, generic enough to fake.
/// Every method takes fully-formed keys (`crate::scope::KvScope` has
/// already built them) and returns a backend-level `String` error for the
/// "Valkey itself failed" case -- `crate::KvHost` is the layer that turns
/// that into the WIT `kv.error::backend` variant.
pub trait KvBackend: Send + Sync {
    /// Plain `GET` -- reads never touch the per-app key counter.
    fn get<'a>(&'a self, data_key: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>>;

    /// Atomically: admits a *new* key only if `count_key` is below
    /// `max_keys`, then `SET`s `value` with `ttl_seconds` (`0` = no
    /// expiry, already clamped by the caller). An already-live key is
    /// always overwritten regardless of quota (overwriting never grows the
    /// app's key count).
    fn set_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>>;

    /// `DEL`s `data_key`; if it existed, decrements `count_key` in the same
    /// round trip. Returns whether the key existed.
    fn delete<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>>;

    /// Atomically: admits a *new* key the same way [`Self::set_with_quota`]
    /// does, then `INCRBY`s `delta` and applies `ttl_seconds` (`EXPIRE` if
    /// nonzero, `PERSIST` if `0` -- `stage.wit`'s `kv.increment` doc: "0
    /// means no expiry", applied identically to `set` on every call, not
    /// just the first). Returns the post-increment value when admitted.
    fn increment_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
        delta: i64,
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>>;

    /// Atomic fixed-window counter: `INCR`s `rate_key`, setting its expiry
    /// to `window_seconds` only on the first hit in the window. Returns the
    /// post-increment count so the caller compares it against
    /// [`crate::limits::MAX_OPS_PER_INVOKE`].
    fn increment_rate<'a>(
        &'a self,
        rate_key: &'a str,
        window_seconds: u64,
    ) -> BoxFuture<'a, Result<u64, String>>;
}

/// `KEYS[1]` = data key, `KEYS[2]` = count key.
/// `ARGV[1]` = value (raw bytes), `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `1` if quota-exceeded (rejected), `0` if admitted/written.
const SET_WITH_QUOTA_SCRIPT: &str = r#"
local existed = redis.call('EXISTS', KEYS[1])
if existed == 0 then
  local count = tonumber(redis.call('GET', KEYS[2]) or '0')
  if count >= tonumber(ARGV[3]) then
    return 1
  end
  redis.call('INCR', KEYS[2])
end
if tonumber(ARGV[2]) > 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
else
  redis.call('SET', KEYS[1], ARGV[1])
end
return 0
"#;

/// `KEYS[1]` = data key, `KEYS[2]` = count key. Returns `1` if `data_key`
/// existed (and `count_key` was decremented), else `0`.
const DELETE_SCRIPT: &str = r#"
local deleted = redis.call('DEL', KEYS[1])
if deleted == 1 then
  redis.call('DECR', KEYS[2])
end
return deleted
"#;

/// `KEYS[1]` = data key, `KEYS[2]` = count key.
/// `ARGV[1]` = delta, `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `{quota_exceeded, new_value}` -- `new_value` is `0` (unused)
/// when `quota_exceeded` is `1`.
const INCREMENT_WITH_QUOTA_SCRIPT: &str = r#"
local existed = redis.call('EXISTS', KEYS[1])
if existed == 0 then
  local count = tonumber(redis.call('GET', KEYS[2]) or '0')
  if count >= tonumber(ARGV[3]) then
    return {1, 0}
  end
  redis.call('INCR', KEYS[2])
end
local newval = redis.call('INCRBY', KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
else
  redis.call('PERSIST', KEYS[1])
end
return {0, newval}
"#;

/// `KEYS[1]` = rate key. `ARGV[1]` = window_seconds. Returns the
/// post-increment count.
const INCREMENT_RATE_SCRIPT: &str = r#"
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"#;

/// Production [`KvBackend`] over the same connection type
/// `core/svc_action::usage::connect`/`core/svc_action::capabilities`
/// already use for `relay`/usage metering.
impl KvBackend for redis::aio::MultiplexedConnection {
    fn get<'a>(&'a self, data_key: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            AsyncCommands::get::<_, Option<Vec<u8>>>(&mut conn, data_key)
                .await
                .map_err(|e| e.to_string())
        })
    }

    fn set_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let quota_exceeded: i64 = redis::Script::new(SET_WITH_QUOTA_SCRIPT)
                .key(data_key)
                .key(count_key)
                .arg(value)
                .arg(ttl_seconds)
                .arg(max_keys)
                .invoke_async(&mut conn)
                .await
                .map_err(|e| e.to_string())?;
            Ok(if quota_exceeded == 1 {
                QuotaOutcome::QuotaExceeded
            } else {
                QuotaOutcome::Admitted(())
            })
        })
    }

    fn delete<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let deleted: i64 = redis::Script::new(DELETE_SCRIPT)
                .key(data_key)
                .key(count_key)
                .invoke_async(&mut conn)
                .await
                .map_err(|e| e.to_string())?;
            Ok(deleted == 1)
        })
    }

    fn increment_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        count_key: &'a str,
        delta: i64,
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let (quota_exceeded, new_value): (i64, i64) =
                redis::Script::new(INCREMENT_WITH_QUOTA_SCRIPT)
                    .key(data_key)
                    .key(count_key)
                    .arg(delta)
                    .arg(ttl_seconds)
                    .arg(max_keys)
                    .invoke_async(&mut conn)
                    .await
                    .map_err(|e| e.to_string())?;
            Ok(if quota_exceeded == 1 {
                QuotaOutcome::QuotaExceeded
            } else {
                QuotaOutcome::Admitted(new_value)
            })
        })
    }

    fn increment_rate<'a>(
        &'a self,
        rate_key: &'a str,
        window_seconds: u64,
    ) -> BoxFuture<'a, Result<u64, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let count: i64 = redis::Script::new(INCREMENT_RATE_SCRIPT)
                .key(rate_key)
                .arg(window_seconds)
                .invoke_async(&mut conn)
                .await
                .map_err(|e| e.to_string())?;
            Ok(count.max(0) as u64)
        })
    }
}

#[cfg(test)]
/// An in-memory [`KvBackend`] fake -- exercises `crate::KvHost`'s
/// orchestration (validation order, quota-rejection plumbing, metrics/log
/// call sites) without a live Valkey server. The real Lua scripts above are
/// only proven correct by `tests/valkey_integration.rs` against a real
/// Valkey container -- this fake deliberately re-implements the same
/// admit/quota/TTL semantics in plain Rust so the two can be cross-checked
/// by running the same `crate::KvHost` test cases against both.
pub(crate) mod fake {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    #[derive(Default)]
    struct Entry {
        value: Vec<u8>,
    }

    #[derive(Default)]
    pub struct FakeBackend {
        data: Mutex<HashMap<String, Entry>>,
        counts: Mutex<HashMap<String, u64>>,
        rates: Mutex<HashMap<String, u64>>,
    }

    impl FakeBackend {
        pub fn new() -> Self {
            Self::default()
        }

        pub fn seed_count(&self, count_key: &str, value: u64) {
            self.counts
                .lock()
                .unwrap()
                .insert(count_key.to_string(), value);
        }

        /// Inserts `data_key` directly, bypassing quota accounting --
        /// simulates "this key already existed before the app's count was
        /// (separately) seeded at its ceiling", for tests proving that
        /// overwriting an existing key is exempt from the key-count quota.
        pub fn seed_existing(&self, data_key: &str, value: &[u8]) {
            self.data.lock().unwrap().insert(
                data_key.to_string(),
                Entry {
                    value: value.to_vec(),
                },
            );
        }
    }

    impl KvBackend for FakeBackend {
        fn get<'a>(&'a self, data_key: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
            let value = self
                .data
                .lock()
                .unwrap()
                .get(data_key)
                .map(|e| e.value.clone());
            Box::pin(async move { Ok(value) })
        }

        fn set_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            value: &'a [u8],
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(async move { Ok(QuotaOutcome::QuotaExceeded) });
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            data.insert(
                data_key.to_string(),
                Entry {
                    value: value.to_vec(),
                },
            );
            Box::pin(async move { Ok(QuotaOutcome::Admitted(())) })
        }

        fn delete<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
        ) -> BoxFuture<'a, Result<bool, String>> {
            let existed = self.data.lock().unwrap().remove(data_key).is_some();
            if existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                counts.insert(count_key.to_string(), count.saturating_sub(1));
            }
            Box::pin(async move { Ok(existed) })
        }

        fn increment_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            delta: i64,
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(async move { Ok(QuotaOutcome::QuotaExceeded) });
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            let current = data
                .get(data_key)
                .and_then(|e| std::str::from_utf8(&e.value).ok())
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            let new_value = current + delta;
            data.insert(
                data_key.to_string(),
                Entry {
                    value: new_value.to_string().into_bytes(),
                },
            );
            Box::pin(async move { Ok(QuotaOutcome::Admitted(new_value)) })
        }

        fn increment_rate<'a>(
            &'a self,
            rate_key: &'a str,
            _window_seconds: u64,
        ) -> BoxFuture<'a, Result<u64, String>> {
            let mut rates = self.rates.lock().unwrap();
            let count = rates.entry(rate_key.to_string()).or_insert(0);
            *count += 1;
            let result = *count;
            Box::pin(async move { Ok(result) })
        }
    }
}
