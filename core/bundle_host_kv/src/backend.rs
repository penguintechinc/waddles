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
//! **Quota accounting -- the live index.** The key quota is enforced
//! against [`KvScope::live_key`], a Valkey sorted set whose members are the
//! app's data keys and whose scores are each key's absolute expiry in epoch
//! milliseconds (`+inf` for a key with no TTL, so it is never trimmed). Data keys carry
//! Valkey-native TTLs, so the host never observes a key lapsing -- which is
//! why the earlier `INCR`/`DECR` counter leaked one quota slot per TTL key
//! forever (a bundle using cooldown keys hit the 10k ceiling and stayed
//! there). The index fixes that without a background sweeper: every
//! quota-checked script first trims lapsed members (`ZREMRANGEBYSCORE
//! live -inf (now`, `now` from Valkey's own `TIME` -- the same clock that
//! expires the data keys) and then counts what is left, so the count is
//! *derived from* the live key set and cannot drift from it. Whether a
//! write is "new" (consumes a slot) or an overwrite (exempt) is decided by
//! index membership, never by a separate counter. `set`/`increment`
//! refresh the member's score (TTL extended, shortened, or cleared with
//! `ttl == 0`); `delete` removes it. The trim costs `O(log N + M)` for `M`
//! lapsed members (amortised against the writes that created them); the
//! count is `O(1)` (`ZCARD`).
//!
//! The zset always carries one never-expiring sentinel member
//! ([`LIVE_INDEX_SENTINEL`], which no guest key can equal -- it contains
//! `:`) so the index key exists exactly when it has been initialised: an
//! app whose last TTL key just lapsed leaves the sentinel behind, while a
//! *missing* index key unambiguously means "never built, or evicted" and
//! triggers [`KvBackend::reconcile_count_if_missing`]. Without the
//! sentinel, Valkey deleting the emptied zset would look like an eviction
//! and force a keyspace `SCAN` on the next cooldown write.
//!
//! **Bounded residuals (all in the fail-closed direction):**
//! - A lapsed member stays in the index (memory only, never counted) until
//!   that app's next quota-checked write trims it.
//! - A `volatile-*` `maxmemory-policy` can evict a TTL'd data key early;
//!   its index member then lingers, counted, until its own expiry score
//!   passes (at most [`crate::limits::MAX_TTL_SECONDS`]) -- a permanent
//!   leak before, a bounded overcount now.
//! - Score and key expiry are both derived from Valkey's clock but sampled
//!   up to ~1 ms apart inside one script; membership is decided by the
//!   index alone, so this can never desynchronise the count.
//!
//! This is a resource ceiling, not a tenant-isolation boundary
//! (`crate::scope::KvScope` is).

use std::future::Future;
use std::pin::Pin;

use redis::AsyncCommands;

/// The live index's reserved never-expiring member (see the module doc).
/// A macro so the same literal feeds both the Rust constant and the Lua
/// prelude below via `concat!`. Contains `:`, which
/// [`crate::scope::validate_guest_key`] rejects, so no guest key -- and
/// therefore no data key -- can ever equal it.
macro_rules! live_index_sentinel {
    () => {
        ":init"
    };
}

/// See [`live_index_sentinel!`] and the module doc.
pub const LIVE_INDEX_SENTINEL: &str = live_index_sentinel!();

/// Shared Lua prelude: `now` is Valkey's own clock in epoch milliseconds
/// (the clock that expires the data keys), `live_count` is the number of
/// real members in a live index, `expiry_score` maps a ttl in seconds to
/// the member score (`+inf` for `0`), and `trim` drops every member whose
/// expiry has strictly passed (Valkey treats a key as expired once
/// `now > expiry`, so a score equal to `now` is still live).
macro_rules! lua_prelude {
    () => {
        concat!(
            "local SENTINEL = '",
            live_index_sentinel!(),
            "'\n",
            r#"
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local function fmt(n) return string.format('%.0f', n) end
local function trim(live)
  redis.call('ZREMRANGEBYSCORE', live, '-inf', '(' .. fmt(now))
end
local function live_count(live)
  local n = redis.call('ZCARD', live)
  if redis.call('ZSCORE', live, SENTINEL) then n = n - 1 end
  return n
end
local function expiry_score(ttl)
  if ttl > 0 then return fmt(now + ttl * 1000) end
  return '+inf'
end
"#
        )
    };
}

/// Outcome of a quota-checked write (`set`/`increment`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaOutcome<T> {
    Admitted(T),
    QuotaExceeded,
}

/// Outcome of [`KvBackend::reconcile_count_if_missing`] (self-heal, low-
/// severity fix on PR #425's security review: the live index has no TTL,
/// but an `allkeys-*` `maxmemory-policy` -- see `crate::policy` -- can still
/// evict it, silently resetting the quota to zero).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconcileOutcome {
    /// The live index already existed -- no reconciliation needed, no
    /// `SCAN` run. The overwhelming steady-state case.
    AlreadyPresent,
    /// The live index was missing; this call won the reconciliation lock,
    /// ran the bounded `SCAN`, and rebuilt the index from the live data
    /// keys (reporting the live-key count, capped at
    /// [`crate::limits::RECONCILE_SCAN_LIMIT`]).
    Reconciled(u64),
    /// The live index was missing, but another caller already holds the
    /// reconciliation lock -- the caller must fail closed (deny the
    /// write), never guess a count or proceed unreconciled.
    Locked,
}

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The Valkey primitives `crate::KvHost` needs, generic enough to fake.
/// Every method takes fully-formed keys (`crate::scope::KvScope` has
/// already built them) and returns a backend-level `String` error for the
/// "Valkey itself failed" case -- `crate::KvHost` is the layer that turns
/// that into the WIT `kv.error::backend` variant. `live_key` is always
/// [`KvScope::live_key`], the sorted-set live index described in the
/// module doc.
pub trait KvBackend: Send + Sync {
    /// Plain `GET` -- reads never touch the live index.
    fn get<'a>(&'a self, data_key: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>>;

    /// Atomically: trims lapsed members from the live index, admits a *new*
    /// key (one not already in the index) only if fewer than `max_keys` live
    /// keys remain, then `SET`s `value` with `ttl_seconds` (`0` = no
    /// expiry, already clamped by the caller) and refreshes the key's index
    /// score to match. A key already live in the index is always
    /// overwritten regardless of quota (overwriting never grows the app's
    /// key count).
    fn set_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>>;

    /// `DEL`s `data_key` and removes it from the live index in the same
    /// round trip. Returns whether the key existed in Valkey.
    fn delete<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>>;

    /// Atomically: admits a *new* key the same way [`Self::set_with_quota`]
    /// does, then `INCRBY`s `delta` and applies `ttl_seconds` (`EXPIRE` if
    /// nonzero, `PERSIST` if `0` -- `stage.wit`'s `kv.increment` doc: "0
    /// means no expiry", applied identically to `set` on every call, not
    /// just the first), refreshing the key's index score to match. Returns
    /// the post-increment value when admitted. A failing `INCRBY` (value is
    /// not an integer) leaves the live index untouched.
    fn increment_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
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

    /// Self-heal, low-severity fix (PR #425 security review): if
    /// `live_key` is missing, acquires `lock_key` (`SET NX PX
    /// lock_ttl_ms`) and, only if acquired, runs a bounded `SCAN` over
    /// `data_scan_pattern`, rebuilding the live index from the data keys
    /// that are actually live (each member scored from its remaining
    /// `PTTL`, capped at `scan_limit`) and deleting `legacy_count_key` --
    /// the pre-live-index `INCR`/`DECR` counter, whose phantom counts this
    /// migration repays; also covers the first write after an upgrade.
    /// If `live_key` already exists, returns
    /// [`ReconcileOutcome::AlreadyPresent`] immediately -- no lock attempt,
    /// no `SCAN`. If another caller already holds `lock_key`, returns
    /// [`ReconcileOutcome::Locked`] -- the caller must fail closed. Called
    /// by `crate::KvHost` before every quota-checked write
    /// (`set`/`increment`), lazily, on the write path where a wrong
    /// zero-count would actually matter (`crate::policy`'s doc).
    fn reconcile_count_if_missing<'a>(
        &'a self,
        live_key: &'a str,
        data_scan_pattern: &'a str,
        lock_key: &'a str,
        legacy_count_key: &'a str,
        lock_ttl_ms: u64,
        scan_limit: u64,
    ) -> BoxFuture<'a, Result<ReconcileOutcome, String>>;
}

/// `KEYS[1]` = data key, `KEYS[2]` = live index key.
/// `ARGV[1]` = value (raw bytes), `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `1` if quota-exceeded (rejected), `0` if admitted/written.
///
/// Trims lapsed members first, so the quota is checked against live keys
/// only; a key already in the index is an overwrite and never consumes a
/// slot. `SET` cannot fail, so the index update that follows it cannot be
/// left half-applied.
const SET_WITH_QUOTA_SCRIPT: &str = concat!(
    lua_prelude!(),
    r#"
trim(KEYS[2])
if not redis.call('ZSCORE', KEYS[2], KEYS[1]) then
  if live_count(KEYS[2]) >= tonumber(ARGV[3]) then
    return 1
  end
end
local ttl = tonumber(ARGV[2])
if ttl > 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
else
  redis.call('SET', KEYS[1], ARGV[1])
end
redis.call('ZADD', KEYS[2], expiry_score(ttl), KEYS[1])
return 0
"#
);

/// `KEYS[1]` = data key, `KEYS[2]` = live index key. Returns `1` if
/// `data_key` existed in Valkey, else `0`.
///
/// The index member is removed unconditionally (not only when `DEL` found a
/// key): a data key Valkey evicted or already lapsed leaves nothing to
/// `DEL`, but a stale member must still be released. A missing index key
/// stays missing (`ZREM` on it is a no-op) -- never a fabricated, wrongly
/// small index; recovery is left to
/// `crate::backend::KvBackend::reconcile_count_if_missing` (run by the
/// *next* `set`/`increment` on this app, whose `SCAN` already reflects this
/// delete).
const DELETE_SCRIPT: &str = r#"
local deleted = redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], KEYS[1])
return deleted
"#;

/// `KEYS[1]` = live index key, `KEYS[2]` = lock key, `KEYS[3]` = legacy
/// counter key.
/// `ARGV[1]` = data_scan_pattern, `ARGV[2]` = lock_ttl_ms, `ARGV[3]` = scan_limit.
/// Returns `{status, count}`: `status` `0` = already present (`count`
/// unused), `1` = reconciled (`count` is the live-key count, capped at
/// `scan_limit`), `2` = locked by another caller (`count` unused). `SCAN`
/// inside a Lua script is deterministic and safe -- the whole script runs
/// atomically, so no concurrent write can land between two `SCAN` cursor
/// steps. Each scanned key is scored from its remaining `PTTL` (`-1` = no
/// expiry; `-2` = lapsed since the scan returned it, skipped). `ZADD` is
/// idempotent, so a key `SCAN` returns twice is counted once -- the count
/// is read back from `ZCARD`, not tallied during the scan.
const RECONCILE_SCRIPT: &str = concat!(
    lua_prelude!(),
    r#"
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {0, 0}
end
local lock_ok = redis.call('SET', KEYS[2], '1', 'NX', 'PX', ARGV[2])
if not lock_ok then
  return {2, 0}
end
local cursor = '0'
local count = 0
local limit = tonumber(ARGV[3])
repeat
  local res = redis.call('SCAN', cursor, 'MATCH', ARGV[1], 'COUNT', 1000)
  cursor = res[1]
  for _, k in ipairs(res[2]) do
    local pttl = redis.call('PTTL', k)
    if pttl ~= -2 then
      local score = '+inf'
      if pttl >= 0 then score = fmt(now + pttl) end
      redis.call('ZADD', KEYS[1], score, k)
    end
  end
  count = redis.call('ZCARD', KEYS[1])
  if count >= limit then
    count = limit
    break
  end
until cursor == '0'
redis.call('ZADD', KEYS[1], '+inf', SENTINEL)
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[2])
return {1, count}
"#
);

/// `KEYS[1]` = data key, `KEYS[2]` = live index key.
/// `ARGV[1]` = delta, `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `{quota_exceeded, new_value}` -- `new_value` is `0` (unused)
/// when `quota_exceeded` is `1`.
///
/// `INCRBY` (the only step that can fail: non-integer value) runs before
/// any index mutation other than the idempotent trim, so a failure cannot
/// leave a phantom member behind.
const INCREMENT_WITH_QUOTA_SCRIPT: &str = concat!(
    lua_prelude!(),
    r#"
trim(KEYS[2])
if not redis.call('ZSCORE', KEYS[2], KEYS[1]) then
  if live_count(KEYS[2]) >= tonumber(ARGV[3]) then
    return {1, 0}
  end
end
local newval = redis.call('INCRBY', KEYS[1], ARGV[1])
local ttl = tonumber(ARGV[2])
if ttl > 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
else
  redis.call('PERSIST', KEYS[1])
end
redis.call('ZADD', KEYS[2], expiry_score(ttl), KEYS[1])
return {0, newval}
"#
);

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
        live_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let quota_exceeded: i64 = redis::Script::new(SET_WITH_QUOTA_SCRIPT)
                .key(data_key)
                .key(live_key)
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
        live_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let deleted: i64 = redis::Script::new(DELETE_SCRIPT)
                .key(data_key)
                .key(live_key)
                .invoke_async(&mut conn)
                .await
                .map_err(|e| e.to_string())?;
            Ok(deleted == 1)
        })
    }

    fn increment_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
        delta: i64,
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let (quota_exceeded, new_value): (i64, i64) =
                redis::Script::new(INCREMENT_WITH_QUOTA_SCRIPT)
                    .key(data_key)
                    .key(live_key)
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

    fn reconcile_count_if_missing<'a>(
        &'a self,
        live_key: &'a str,
        data_scan_pattern: &'a str,
        lock_key: &'a str,
        legacy_count_key: &'a str,
        lock_ttl_ms: u64,
        scan_limit: u64,
    ) -> BoxFuture<'a, Result<ReconcileOutcome, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let (status, count): (i64, u64) = redis::Script::new(RECONCILE_SCRIPT)
                .key(live_key)
                .key(lock_key)
                .key(legacy_count_key)
                .arg(data_scan_pattern)
                .arg(lock_ttl_ms)
                .arg(scan_limit)
                .invoke_async(&mut conn)
                .await
                .map_err(|e| e.to_string())?;
            Ok(match status {
                0 => ReconcileOutcome::AlreadyPresent,
                2 => ReconcileOutcome::Locked,
                _ => ReconcileOutcome::Reconciled(count),
            })
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
///
/// Data keys and the live index share one `Mutex` so every operation is
/// atomic, mirroring a Lua `EVAL` running to completion. Time is a manual
/// clock ([`FakeBackend::advance`]) rather than wall time, so TTL expiry is
/// deterministic: a key lapses once `now > expiry`, exactly Valkey's rule.
pub(crate) mod fake {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;
    use std::time::Duration;

    struct Entry {
        value: Vec<u8>,
        /// Absolute expiry in fake-clock milliseconds; `None` = no TTL.
        expires_at_ms: Option<u64>,
    }

    /// One live index: member -> expiry (`None` = `+inf`). The map existing
    /// at all models "the zset key exists" (initialised, sentinel present).
    type LiveIndex = HashMap<String, Option<u64>>;

    #[derive(Default)]
    struct State {
        now_ms: u64,
        data: HashMap<String, Entry>,
        live: HashMap<String, LiveIndex>,
    }

    fn lapsed(expires_at_ms: Option<u64>, now_ms: u64) -> bool {
        expires_at_ms.is_some_and(|at| now_ms > at)
    }

    impl State {
        /// `ZREMRANGEBYSCORE live -inf (now`.
        fn trim(&mut self, live_key: &str) {
            let now = self.now_ms;
            if let Some(index) = self.live.get_mut(live_key) {
                index.retain(|_, expires| !lapsed(*expires, now));
            }
        }

        fn live_count(&self, live_key: &str) -> u64 {
            self.live.get(live_key).map_or(0, |i| i.len() as u64)
        }

        fn is_indexed(&self, live_key: &str, data_key: &str) -> bool {
            self.live
                .get(live_key)
                .is_some_and(|i| i.contains_key(data_key))
        }

        /// The data key's value, `None` if absent or lapsed (Valkey would
        /// have reclaimed it).
        fn live_value(&self, data_key: &str) -> Option<&Entry> {
            self.data
                .get(data_key)
                .filter(|e| !lapsed(e.expires_at_ms, self.now_ms))
        }

        fn expiry_for(&self, ttl_seconds: u32) -> Option<u64> {
            (ttl_seconds > 0).then(|| self.now_ms + u64::from(ttl_seconds) * 1_000)
        }
    }

    #[derive(Default)]
    pub struct FakeBackend {
        state: Mutex<State>,
        rates: Mutex<HashMap<String, u64>>,
        /// Held reconciliation locks -- simulates `SET NX` (present = held).
        locks: Mutex<HashMap<String, ()>>,
    }

    impl FakeBackend {
        pub fn new() -> Self {
            Self::default()
        }

        /// Moves the fake clock forward -- keys whose TTL elapses lapse
        /// exactly as Valkey's own expiry would.
        pub fn advance(&self, by: Duration) {
            self.state.lock().unwrap().now_ms += by.as_millis() as u64;
        }

        /// Initialises `live_key`'s index and tops it up with permanent
        /// filler members until it holds `count` live members (never
        /// lowering an already-larger index) -- the unit-test shortcut for
        /// "this app is already at N keys" without writing N real keys.
        pub fn seed_count(&self, live_key: &str, count: u64) {
            let mut st = self.state.lock().unwrap();
            let now = st.now_ms;
            let index = st.live.entry(live_key.to_string()).or_default();
            index.retain(|_, expires| !lapsed(*expires, now));
            let mut filler = 0u64;
            while (index.len() as u64) < count {
                index.insert(format!("__seed_filler_{filler}"), None);
                filler += 1;
            }
        }

        /// Stores `data_key` directly -- bypassing the live index -- with an
        /// optional TTL in milliseconds. Simulates a key written before the
        /// index existed (a pre-upgrade deployment) or whose index was lost,
        /// i.e. exactly what `reconcile_count_if_missing` rebuilds from.
        pub fn seed_unindexed(&self, data_key: &str, value: &[u8], ttl_ms: Option<u64>) {
            let mut st = self.state.lock().unwrap();
            let expires_at_ms = ttl_ms.map(|ms| st.now_ms + ms);
            st.data.insert(
                data_key.to_string(),
                Entry {
                    value: value.to_vec(),
                    expires_at_ms,
                },
            );
        }

        /// Simulates Valkey evicting `live_key` under memory pressure
        /// (`crate::policy`'s doc) -- removes the index while leaving every
        /// data key untouched, the exact drift `reconcile_count_if_missing`
        /// self-heals.
        pub fn evict_count(&self, live_key: &str) {
            self.state.lock().unwrap().live.remove(live_key);
        }

        /// Whether `live_key`'s index currently exists (initialised).
        pub fn index_exists(&self, live_key: &str) -> bool {
            self.state.lock().unwrap().live.contains_key(live_key)
        }

        /// The number of *live* (not yet lapsed) keys `live_key`'s index
        /// accounts for -- what the quota is enforced against, evaluated
        /// non-destructively at the current fake time.
        pub fn live_count(&self, live_key: &str) -> u64 {
            let st = self.state.lock().unwrap();
            st.live.get(live_key).map_or(0, |index| {
                index
                    .values()
                    .filter(|expires| !lapsed(**expires, st.now_ms))
                    .count() as u64
            })
        }

        /// Simulates another caller already holding the reconciliation
        /// lock for `lock_key`.
        pub fn hold_lock(&self, lock_key: &str) {
            self.locks.lock().unwrap().insert(lock_key.to_string(), ());
        }
    }

    impl KvBackend for FakeBackend {
        fn get<'a>(&'a self, data_key: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
            let value = self
                .state
                .lock()
                .unwrap()
                .live_value(data_key)
                .map(|e| e.value.clone());
            Box::pin(async move { Ok(value) })
        }

        fn set_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            live_key: &'a str,
            value: &'a [u8],
            ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
            let mut st = self.state.lock().unwrap();
            st.trim(live_key);
            if !st.is_indexed(live_key, data_key) && st.live_count(live_key) >= max_keys {
                return Box::pin(async move { Ok(QuotaOutcome::QuotaExceeded) });
            }
            let expires_at_ms = st.expiry_for(ttl_seconds);
            st.data.insert(
                data_key.to_string(),
                Entry {
                    value: value.to_vec(),
                    expires_at_ms,
                },
            );
            st.live
                .entry(live_key.to_string())
                .or_default()
                .insert(data_key.to_string(), expires_at_ms);
            Box::pin(async move { Ok(QuotaOutcome::Admitted(())) })
        }

        fn delete<'a>(
            &'a self,
            data_key: &'a str,
            live_key: &'a str,
        ) -> BoxFuture<'a, Result<bool, String>> {
            let mut st = self.state.lock().unwrap();
            let existed = st.live_value(data_key).is_some();
            st.data.remove(data_key);
            // Mirrors `DELETE_SCRIPT`: the member is released
            // unconditionally, but a missing index is never fabricated.
            if let Some(index) = st.live.get_mut(live_key) {
                index.remove(data_key);
            }
            Box::pin(async move { Ok(existed) })
        }

        fn increment_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            live_key: &'a str,
            delta: i64,
            ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
            let mut st = self.state.lock().unwrap();
            st.trim(live_key);
            if !st.is_indexed(live_key, data_key) && st.live_count(live_key) >= max_keys {
                return Box::pin(async move { Ok(QuotaOutcome::QuotaExceeded) });
            }
            // `INCRBY` first: a non-integer value fails before any index
            // mutation, exactly like `INCREMENT_WITH_QUOTA_SCRIPT`.
            let current = match st.live_value(data_key) {
                None => 0,
                Some(entry) => match std::str::from_utf8(&entry.value)
                    .ok()
                    .and_then(|s| s.parse::<i64>().ok())
                {
                    Some(n) => n,
                    None => {
                        return Box::pin(async move {
                            Err("value is not an integer or out of range".to_string())
                        })
                    }
                },
            };
            let new_value = current + delta;
            let expires_at_ms = st.expiry_for(ttl_seconds);
            st.data.insert(
                data_key.to_string(),
                Entry {
                    value: new_value.to_string().into_bytes(),
                    expires_at_ms,
                },
            );
            st.live
                .entry(live_key.to_string())
                .or_default()
                .insert(data_key.to_string(), expires_at_ms);
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

        fn reconcile_count_if_missing<'a>(
            &'a self,
            live_key: &'a str,
            data_scan_pattern: &'a str,
            lock_key: &'a str,
            _legacy_count_key: &'a str,
            _lock_ttl_ms: u64,
            scan_limit: u64,
        ) -> BoxFuture<'a, Result<ReconcileOutcome, String>> {
            let mut st = self.state.lock().unwrap();
            if st.live.contains_key(live_key) {
                return Box::pin(async move { Ok(ReconcileOutcome::AlreadyPresent) });
            }
            let mut locks = self.locks.lock().unwrap();
            if locks.contains_key(lock_key) {
                return Box::pin(async move { Ok(ReconcileOutcome::Locked) });
            }
            locks.insert(lock_key.to_string(), ());

            // `data_scan_pattern` is always `{prefix}:data:*` in this
            // crate's own usage -- a trailing-`*` prefix match is all this
            // fake needs (it never sees arbitrary glob patterns). Lapsed
            // keys are skipped and each live key keeps its own expiry.
            let prefix = data_scan_pattern.trim_end_matches('*');
            let now = st.now_ms;
            let index: LiveIndex = st
                .data
                .iter()
                .filter(|(k, e)| k.starts_with(prefix) && !lapsed(e.expires_at_ms, now))
                .map(|(k, e)| (k.clone(), e.expires_at_ms))
                .collect();
            let count = (index.len() as u64).min(scan_limit);
            st.live.insert(live_key.to_string(), index);
            locks.remove(lock_key);
            Box::pin(async move { Ok(ReconcileOutcome::Reconciled(count)) })
        }
    }
}
