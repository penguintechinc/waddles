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
//! **Rolling-upgrade coexistence (two-phase).** Pods running the pre-index
//! code enforce the quota against the legacy `INCR`/`DECR` counter
//! ([`KvScope::legacy_count_key`], `:count`); pods running this code enforce
//! against `:live`. Both run against the same Valkey during a rolling deploy
//! (and again after a rollback), and each version must never *under*-enforce
//! while the other is alive:
//!
//! - **Phase 1 (this release) never deletes `:count`.** Reconciliation leaves
//!   it alone. Deleting it was a fail-open: a legacy pod that had already
//!   passed its own reconcile check recreated it from `0` mid-request and
//!   then under-counted for the rest of the rollout. Left in place it is an
//!   over-count-only upper bound -- a lapsed TTL key never decrements it (the
//!   old leak), so a legacy pod is at worst stricter than the truth.
//! - **Write-through while it exists.** `set`/`increment` `INCR` it for a data
//!   key that did not exist before the write and `delete` `DECR`s it for one
//!   that did (the legacy scripts' exact criteria), so a legacy pod's counter
//!   also includes every key *this* code creates -- without that a legacy pod
//!   is blind to them. The legacy ops are `pcall`-guarded (debris can never
//!   fail a write) and skipped when the counter is absent: a legacy pod's own
//!   reconcile then rebuilds it from a `SCAN`, which sees our keys.
//! - **Phase 2 (a later release, once no pre-index pod runs and a rollback to
//!   one is off the table)** drops the write-through and deletes the `:count`
//!   keys. Select exactly `{app_prefix}:count` per app -- never the glob
//!   `bundlekv:*:count`, which also matches the data key of a guest key named
//!   `count`. Nothing in this release removes it.
//!
//! **Residual, bounded to the mixed window:** a pre-index pod's writes never
//! reach `:live`, so keys a legacy pod creates *after* this code built `:live`
//! are not counted by it until `:live` is rebuilt (eviction) or they lapse.
//! Legacy pods themselves stay under the ceiling (their counter includes our
//! keys), so the overshoot is at most the keys legacy pods admit during the
//! roll -- and it cannot be closed from this side, since the old code cannot
//! be changed.
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

/// Lua helpers keeping the legacy `INCR`/`DECR` counter a valid upper bound
/// for pre-index pods during a rolling upgrade (see the module doc's
/// coexistence section). Both act only when the counter already exists --
/// creating it from a guess is exactly the under-count to avoid; a legacy
/// pod's own reconcile rebuilds a missing one from a `SCAN`. `pcall` so a
/// corrupt/foreign-typed `:count` can never fail (or half-apply) a write, and
/// `decr` never takes the counter to or below `0` while a key still lives.
macro_rules! lua_legacy_counter {
    () => {
        r#"
local function legacy_incr(key)
  if redis.call('EXISTS', key) == 1 then
    redis.pcall('INCR', key)
  end
end
local function legacy_decr(key)
  local v = redis.pcall('GET', key)
  if type(v) == 'string' and (tonumber(v) or 0) > 0 then
    redis.pcall('DECR', key)
  end
end
"#
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
    /// key count). `legacy_count_key` is written through for rolling-upgrade
    /// coexistence (module doc): `INCR`ed iff it exists and `data_key` did
    /// not exist before this write; never created, never read for admission.
    fn set_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
        legacy_count_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>>;

    /// `DEL`s `data_key` and removes it from the live index in the same
    /// round trip. Returns whether the key existed in Valkey. When it did,
    /// `legacy_count_key` is `DECR`ed iff it exists and is positive (module
    /// doc: the write-through that keeps legacy pods' counter an upper bound).
    fn delete<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
        legacy_count_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>>;

    /// Atomically: admits a *new* key the same way [`Self::set_with_quota`]
    /// does, then `INCRBY`s `delta` and applies `ttl_seconds` (`EXPIRE` if
    /// nonzero, `PERSIST` if `0` -- `stage.wit`'s `kv.increment` doc: "0
    /// means no expiry", applied identically to `set` on every call, not
    /// just the first), refreshing the key's index score to match. Returns
    /// the post-increment value when admitted. A failing `INCRBY` (value is
    /// not an integer) leaves the live index and `legacy_count_key`
    /// untouched; `legacy_count_key` is otherwise written through exactly as
    /// in [`Self::set_with_quota`].
    fn increment_with_quota<'a>(
        &'a self,
        data_key: &'a str,
        live_key: &'a str,
        legacy_count_key: &'a str,
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
    /// `PTTL`, capped at `scan_limit`); also covers the first write after an
    /// upgrade. The pre-live-index `INCR`/`DECR` counter is deliberately left
    /// untouched (module doc: deleting it was a mixed-version fail-open).
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
        lock_ttl_ms: u64,
        scan_limit: u64,
    ) -> BoxFuture<'a, Result<ReconcileOutcome, String>>;
}

/// `KEYS[1]` = data key, `KEYS[2]` = live index key, `KEYS[3]` = legacy
/// counter key.
/// `ARGV[1]` = value (raw bytes), `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `1` if quota-exceeded (rejected), `0` if admitted/written.
///
/// Trims lapsed members first, so the quota is checked against live keys
/// only; a key already in the index is an overwrite and never consumes a
/// slot. `SET` cannot fail, so the index update that follows it cannot be
/// left half-applied. The legacy counter is written through last (and only
/// for a data key that did not exist -- the legacy scripts' own criterion),
/// never consulted for admission.
const SET_WITH_QUOTA_SCRIPT: &str = concat!(
    lua_prelude!(),
    lua_legacy_counter!(),
    r#"
trim(KEYS[2])
local existed = redis.call('EXISTS', KEYS[1])
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
if existed == 0 then
  legacy_incr(KEYS[3])
end
return 0
"#
);

/// `KEYS[1]` = data key, `KEYS[2]` = live index key, `KEYS[3]` = legacy
/// counter key. Returns `1` if `data_key` existed in Valkey, else `0`.
///
/// The index member is removed unconditionally (not only when `DEL` found a
/// key): a data key Valkey evicted or already lapsed leaves nothing to
/// `DEL`, but a stale member must still be released. A missing index key
/// stays missing (`ZREM` on it is a no-op) -- never a fabricated, wrongly
/// small index; recovery is left to
/// `crate::backend::KvBackend::reconcile_count_if_missing` (run by the
/// *next* `set`/`increment` on this app, whose `SCAN` already reflects this
/// delete). The legacy counter is decremented only when `DEL` actually
/// removed a key (the legacy script's own criterion), via the guarded helper
/// so a missing counter is never created at `-1`.
const DELETE_SCRIPT: &str = concat!(
    lua_legacy_counter!(),
    r#"
local deleted = redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], KEYS[1])
if deleted == 1 then
  legacy_decr(KEYS[3])
end
return deleted
"#
);

/// `KEYS[1]` = live index key, `KEYS[2]` = lock key.
/// `ARGV[1]` = data_scan_pattern, `ARGV[2]` = lock_ttl_ms, `ARGV[3]` = scan_limit.
/// Returns `{status, count}`: `status` `0` = already present (`count`
/// unused), `1` = reconciled (`count` is the live-key count, capped at
/// `scan_limit`), `2` = locked by another caller (`count` unused). `SCAN`
/// inside a Lua script is deterministic and safe -- the whole script runs
/// atomically, so no concurrent write can land between two `SCAN` cursor
/// steps. Each scanned key is scored from its remaining `PTTL` (`-1` = no
/// expiry; `-2` = lapsed since the scan returned it, skipped). `ZADD` is
/// idempotent, so a key `SCAN` returns twice is counted once -- the count
/// is read back from `ZCARD`, not tallied during the scan. Never touches the
/// legacy counter -- deleting it here was the mixed-version fail-open (module
/// doc, rolling-upgrade coexistence).
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
redis.call('DEL', KEYS[2])
return {1, count}
"#
);

/// `KEYS[1]` = data key, `KEYS[2]` = live index key, `KEYS[3]` = legacy
/// counter key.
/// `ARGV[1]` = delta, `ARGV[2]` = ttl_seconds, `ARGV[3]` = max_keys.
/// Returns `{quota_exceeded, new_value}` -- `new_value` is `0` (unused)
/// when `quota_exceeded` is `1`.
///
/// `INCRBY` (the only step that can fail: non-integer value) runs before
/// any index or legacy-counter mutation other than the idempotent trim, so a
/// failure cannot leave a phantom member (or a skewed legacy counter) behind.
const INCREMENT_WITH_QUOTA_SCRIPT: &str = concat!(
    lua_prelude!(),
    lua_legacy_counter!(),
    r#"
trim(KEYS[2])
local existed = redis.call('EXISTS', KEYS[1])
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
if existed == 0 then
  legacy_incr(KEYS[3])
end
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
        legacy_count_key: &'a str,
        value: &'a [u8],
        ttl_seconds: u32,
        max_keys: u64,
    ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let quota_exceeded: i64 = redis::Script::new(SET_WITH_QUOTA_SCRIPT)
                .key(data_key)
                .key(live_key)
                .key(legacy_count_key)
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
        legacy_count_key: &'a str,
    ) -> BoxFuture<'a, Result<bool, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let deleted: i64 = redis::Script::new(DELETE_SCRIPT)
                .key(data_key)
                .key(live_key)
                .key(legacy_count_key)
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
        legacy_count_key: &'a str,
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
                    .key(legacy_count_key)
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
        lock_ttl_ms: u64,
        scan_limit: u64,
    ) -> BoxFuture<'a, Result<ReconcileOutcome, String>> {
        let mut conn = self.clone();
        Box::pin(async move {
            let (status, count): (i64, u64) = redis::Script::new(RECONCILE_SCRIPT)
                .key(live_key)
                .key(lock_key)
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
        /// Legacy `INCR`/`DECR` counters (`:count` keys) -- present only if
        /// something created them (a pre-index pod's reconcile, or a test
        /// seeding one), exactly like the real key.
        legacy: HashMap<String, i64>,
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

        /// `legacy_incr` in the Lua prelude: only if the counter exists.
        fn legacy_incr(&mut self, legacy_key: &str) {
            if let Some(v) = self.legacy.get_mut(legacy_key) {
                *v += 1;
            }
        }

        /// `legacy_decr` in the Lua prelude: only if it exists and is > 0.
        fn legacy_decr(&mut self, legacy_key: &str) {
            if let Some(v) = self.legacy.get_mut(legacy_key) {
                if *v > 0 {
                    *v -= 1;
                }
            }
        }

        /// Number of non-lapsed data keys under `prefix` -- the ground truth a
        /// quota counter is supposed to track.
        fn true_live_keys(&self, prefix: &str) -> u64 {
            self.data
                .iter()
                .filter(|(k, e)| k.starts_with(prefix) && !lapsed(e.expires_at_ms, self.now_ms))
                .count() as u64
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

        /// Plants a legacy `INCR`/`DECR` counter at `legacy_key` -- the state
        /// a pre-index deployment leaves behind (and keeps using while its
        /// pods run).
        pub fn seed_legacy(&self, legacy_key: &str, value: i64) {
            self.state
                .lock()
                .unwrap()
                .legacy
                .insert(legacy_key.to_string(), value);
        }

        /// The legacy counter's current value, `None` if it does not exist.
        pub fn legacy_value(&self, legacy_key: &str) -> Option<i64> {
            self.state.lock().unwrap().legacy.get(legacy_key).copied()
        }

        /// Ground truth: how many non-lapsed data keys exist under
        /// `data_scan_pattern` (`{prefix}:data:*`), independent of any counter
        /// or index.
        pub fn true_live_keys(&self, data_scan_pattern: &str) -> u64 {
            self.state
                .lock()
                .unwrap()
                .true_live_keys(data_scan_pattern.trim_end_matches('*'))
        }

        /// A pre-index pod's `ensure_count_reconciled` step (the legacy
        /// `RECONCILE_SCRIPT`): a no-op while the counter exists, otherwise
        /// `SET legacy_key <live data-key count, capped at scan_limit>`.
        /// Split from [`Self::legacy_set`] because the real legacy host made
        /// two round trips -- tests interleave a new pod between them.
        pub fn legacy_reconcile(&self, legacy_key: &str, data_scan_pattern: &str, scan_limit: u64) {
            let mut st = self.state.lock().unwrap();
            if st.legacy.contains_key(legacy_key) {
                return;
            }
            let count = st
                .true_live_keys(data_scan_pattern.trim_end_matches('*'))
                .min(scan_limit);
            st.legacy.insert(legacy_key.to_string(), count as i64);
        }

        /// The legacy `SET_WITH_QUOTA_SCRIPT`, verbatim semantics: a *new*
        /// data key is admitted only while `GET count or 0` is below
        /// `max_keys` (a missing counter reads as `0`, and is created by the
        /// `INCR`), overwrites are exempt. Returns whether it was admitted.
        pub fn legacy_set(
            &self,
            data_key: &str,
            legacy_key: &str,
            value: &[u8],
            ttl_seconds: u32,
            max_keys: u64,
        ) -> bool {
            let mut st = self.state.lock().unwrap();
            if st.live_value(data_key).is_none() {
                let count = st.legacy.get(legacy_key).copied().unwrap_or(0);
                if count >= max_keys as i64 {
                    return false;
                }
                *st.legacy.entry(legacy_key.to_string()).or_insert(0) += 1;
            }
            let expires_at_ms = st.expiry_for(ttl_seconds);
            st.data.insert(
                data_key.to_string(),
                Entry {
                    value: value.to_vec(),
                    expires_at_ms,
                },
            );
            true
        }

        /// The legacy `DELETE_SCRIPT`: `DEL`, then `DECR` only if a key was
        /// removed and the counter exists (no floor -- the legacy script had
        /// none). Returns whether the key existed.
        pub fn legacy_delete(&self, data_key: &str, legacy_key: &str) -> bool {
            let mut st = self.state.lock().unwrap();
            let existed = st.live_value(data_key).is_some();
            st.data.remove(data_key);
            if existed {
                if let Some(v) = st.legacy.get_mut(legacy_key) {
                    *v -= 1;
                }
            }
            existed
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
            legacy_count_key: &'a str,
            value: &'a [u8],
            ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
            let mut st = self.state.lock().unwrap();
            st.trim(live_key);
            let existed = st.live_value(data_key).is_some();
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
            if !existed {
                st.legacy_incr(legacy_count_key);
            }
            Box::pin(async move { Ok(QuotaOutcome::Admitted(())) })
        }

        fn delete<'a>(
            &'a self,
            data_key: &'a str,
            live_key: &'a str,
            legacy_count_key: &'a str,
        ) -> BoxFuture<'a, Result<bool, String>> {
            let mut st = self.state.lock().unwrap();
            let existed = st.live_value(data_key).is_some();
            st.data.remove(data_key);
            // Mirrors `DELETE_SCRIPT`: the member is released
            // unconditionally, but a missing index is never fabricated.
            if let Some(index) = st.live.get_mut(live_key) {
                index.remove(data_key);
            }
            if existed {
                st.legacy_decr(legacy_count_key);
            }
            Box::pin(async move { Ok(existed) })
        }

        fn increment_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            live_key: &'a str,
            legacy_count_key: &'a str,
            delta: i64,
            ttl_seconds: u32,
            max_keys: u64,
        ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
            let mut st = self.state.lock().unwrap();
            st.trim(live_key);
            let existed = st.live_value(data_key).is_some();
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
            if !existed {
                st.legacy_incr(legacy_count_key);
            }
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

#[cfg(test)]
/// Mixed-version rollout simulation: pods running the pre-index code
/// (legacy `INCR`/`DECR` counter, [`fake::FakeBackend::legacy_set`] & co.,
/// which replicate the legacy Lua scripts) interleaved with pods running
/// this code against one shared backend. The invariant under test is the
/// module doc's coexistence rule: while the legacy counter exists it is never
/// *below* the true live-key count (it may only over-count), so a legacy pod
/// can never be more permissive than the truth. `tests/valkey_integration.rs`
/// repeats this against the verbatim legacy Lua on a real Valkey.
mod mixed_version_tests {
    use super::fake::FakeBackend;
    use super::*;
    use crate::limits;
    use crate::scope::KvScope;
    use std::time::Duration;

    /// Small ceiling so the quota is reachable in a handful of ops; both
    /// versions take `max_keys` as a parameter, so this exercises the real
    /// admission paths unchanged.
    const MAX: u64 = 12;
    const SCAN_LIMIT: u64 = MAX * 2;

    fn scope() -> KvScope {
        KvScope::new("acme", Some("main".to_string()), "waddles.bot.a")
    }

    /// A new (post-fix) pod's write: reconcile-if-missing, then the quota
    /// script -- the exact two calls `KvHost::set` makes.
    async fn new_set(b: &FakeBackend, s: &KvScope, key: &str, ttl: u32) -> bool {
        b.reconcile_count_if_missing(
            &s.live_key(),
            &s.data_scan_pattern(),
            &s.reconcile_lock_key(),
            limits::RECONCILE_LOCK_TTL_MS,
            SCAN_LIMIT,
        )
        .await
        .unwrap();
        matches!(
            b.set_with_quota(
                &s.data_key(key),
                &s.live_key(),
                &s.legacy_count_key(),
                b"v",
                ttl,
                MAX,
            )
            .await
            .unwrap(),
            QuotaOutcome::Admitted(())
        )
    }

    async fn new_delete(b: &FakeBackend, s: &KvScope, key: &str) {
        b.delete(&s.data_key(key), &s.live_key(), &s.legacy_count_key())
            .await
            .unwrap();
    }

    fn old_reconcile(b: &FakeBackend, s: &KvScope) {
        b.legacy_reconcile(&s.legacy_count_key(), &s.data_scan_pattern(), SCAN_LIMIT);
    }

    fn old_set_script(b: &FakeBackend, s: &KvScope, key: &str, ttl: u32) -> bool {
        b.legacy_set(&s.data_key(key), &s.legacy_count_key(), b"v", ttl, MAX)
    }

    fn old_set(b: &FakeBackend, s: &KvScope, key: &str, ttl: u32) -> bool {
        old_reconcile(b, s);
        old_set_script(b, s, key, ttl)
    }

    /// `legacy >= truth` whenever the legacy counter exists.
    fn assert_never_undercounts(b: &FakeBackend, s: &KvScope, ctx: &str) {
        if let Some(legacy) = b.legacy_value(&s.legacy_count_key()) {
            let truth = b.true_live_keys(&s.data_scan_pattern());
            assert!(
                legacy >= truth as i64,
                "{ctx}: legacy counter {legacy} fell below the true live-key count {truth}"
            );
        }
    }

    #[tokio::test]
    async fn a_new_pods_first_write_leaves_the_legacy_counter_in_place() {
        let b = FakeBackend::new();
        let s = scope();
        for i in 0..3 {
            assert!(old_set(&b, &s, &format!("pre.{i}"), 0));
        }
        assert_eq!(b.legacy_value(&s.legacy_count_key()), Some(3));

        // First new-pod write builds `:live` (reconcile) -- it must not
        // delete the counter, and writes its own new key through.
        assert!(new_set(&b, &s, "new.0", 0).await);
        assert!(b.index_exists(&s.live_key()));
        assert_eq!(b.legacy_value(&s.legacy_count_key()), Some(4));
        // The reconcile seeded `:live` from the keyspace, not from `:count`.
        assert_eq!(b.live_count(&s.live_key()), 4);
    }

    /// The reported bypass, replayed exactly: a legacy pod passes its own
    /// reconcile check (counter exists), a new pod then does its first
    /// write/reconcile, and the legacy pod's quota script runs. Deleting the
    /// counter in that gap made the script read `0` and recreate it from
    /// there; the legacy pod then under-counted for the rest of the rollout.
    #[tokio::test]
    async fn a_legacy_pod_cannot_recreate_the_counter_from_zero_mid_request() {
        let b = FakeBackend::new();
        let s = scope();
        for i in 0..4 {
            assert!(old_set(&b, &s, &format!("pre.{i}"), 0));
        }

        old_reconcile(&b, &s); // legacy pod: counter present, proceed
        for i in 0..(MAX - 4) {
            // a new pod fills the app to the ceiling in the gap
            assert!(new_set(&b, &s, &format!("new.{i}"), 0).await);
        }
        assert_eq!(b.true_live_keys(&s.data_scan_pattern()), MAX);

        assert!(
            !old_set_script(&b, &s, "one-too-many", 0),
            "the legacy pod must see the keys the new pod created and reject"
        );
        assert_eq!(b.true_live_keys(&s.data_scan_pattern()), MAX);
        assert_never_undercounts(&b, &s, "after the gap write");
    }

    /// Upgrade from a counter pinned at the ceiling by phantom (lapsed TTL)
    /// keys: the new pod is not blocked by it (it enforces against `:live`),
    /// and the pinned counter stays -- legacy pods keep seeing a full app,
    /// the safe direction -- while still tracking the new key.
    #[tokio::test]
    async fn phantom_pinned_legacy_counter_does_not_block_a_new_pod_and_is_kept() {
        let b = FakeBackend::new();
        let s = scope();
        b.seed_legacy(&s.legacy_count_key(), MAX as i64);
        b.seed_unindexed(&s.data_key("perm"), b"v", None);
        b.seed_unindexed(&s.data_key("ttl"), b"v", Some(60_000));

        assert!(new_set(&b, &s, "first-after-upgrade", 0).await);
        assert_eq!(b.legacy_value(&s.legacy_count_key()), Some(MAX as i64 + 1));
        assert!(!old_set(&b, &s, "legacy-blocked", 0));
        assert_never_undercounts(&b, &s, "after upgrade");
    }

    #[tokio::test]
    async fn a_legacy_pod_sees_new_pod_keys_and_new_pod_deletes_free_slots_for_it() {
        let b = FakeBackend::new();
        let s = scope();
        assert!(old_set(&b, &s, "pre", 0));
        for i in 0..(MAX - 1) {
            assert!(new_set(&b, &s, &format!("new.{i}"), 0).await);
        }
        assert!(!old_set(&b, &s, "blocked", 0), "full: legacy pod rejects");

        new_delete(&b, &s, "new.0").await;
        new_delete(&b, &s, "new.0").await; // double delete: no double DECR
        new_delete(&b, &s, "never-existed").await;
        assert!(old_set(&b, &s, "fits-now", 0), "the freed slot is usable");
        assert!(!old_set(&b, &s, "blocked-again", 0));
        assert_never_undercounts(&b, &s, "after deletes");
    }

    #[tokio::test]
    async fn an_absent_legacy_counter_is_never_created_by_a_new_pod() {
        let b = FakeBackend::new();
        let s = scope();
        for i in 0..3 {
            assert!(new_set(&b, &s, &format!("k.{i}"), 0).await);
        }
        new_delete(&b, &s, "k.0").await;
        assert_eq!(
            b.legacy_value(&s.legacy_count_key()),
            None,
            "a guessed counter is exactly the under-count to avoid"
        );

        // A legacy pod arriving later rebuilds it from the keyspace (SCAN),
        // which already includes the new pod's keys.
        assert!(old_set(&b, &s, "legacy.0", 0));
        assert_eq!(b.legacy_value(&s.legacy_count_key()), Some(3));
    }

    #[tokio::test]
    async fn the_legacy_counter_only_ever_over_counts_lapsed_ttl_keys() {
        let b = FakeBackend::new();
        let s = scope();
        for i in 0..3 {
            assert!(old_set(&b, &s, &format!("old.cooldown.{i}"), 5));
            assert!(new_set(&b, &s, &format!("new.cooldown.{i}"), 5).await);
        }
        b.advance(Duration::from_secs(6)); // every key lapsed; nothing decrements
        assert_eq!(b.true_live_keys(&s.data_scan_pattern()), 0);
        let legacy = b.legacy_value(&s.legacy_count_key()).unwrap();
        assert_eq!(legacy, 6, "lapsed keys stay counted: stricter, never laxer");
        // The new pod is unaffected (its own index trims), the legacy pod
        // stays at the safe-direction overcount.
        assert!(new_set(&b, &s, "fresh", 0).await);
        assert_never_undercounts(&b, &s, "after lapse");
    }

    /// Tiny deterministic PRNG (xorshift64*) -- the property below needs
    /// reproducible interleavings, not a `rand` dependency.
    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 >> 12;
            self.0 ^= self.0 << 25;
            self.0 ^= self.0 >> 27;
            self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    /// Random interleavings of legacy-pod and new-pod set/delete/increment,
    /// TTL lapse, and a new pod landing *between* a legacy pod's reconcile
    /// and its quota script: after every single op the legacy counter (when
    /// it exists) is never below the true live-key count, and once it exists
    /// it never disappears.
    #[tokio::test]
    async fn legacy_counter_never_drops_below_the_true_live_count_under_random_interleaving() {
        for seed in 1..=24u64 {
            let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
            let b = FakeBackend::new();
            let s = scope();
            // Half the runs start from a pre-rollout deployment that already
            // has a legacy counter; half from none (fresh app mid-rollout).
            if seed % 2 == 0 {
                for i in 0..5 {
                    assert!(old_set(
                        &b,
                        &s,
                        &format!("pre.{i}"),
                        if i % 2 == 0 { 0 } else { 8 }
                    ));
                }
            }
            let mut existed_once = b.legacy_value(&s.legacy_count_key()).is_some();

            for step in 0..400u32 {
                let key = format!("k.{}", rng.below(30));
                let ttl = if rng.below(3) == 0 { 6 } else { 0 };
                let ctx = format!("seed {seed} step {step}");
                match rng.below(8) {
                    0 | 1 => {
                        old_set(&b, &s, &key, ttl);
                    }
                    2 => {
                        // a new pod lands between the legacy reconcile and
                        // the legacy quota script (the reported TOCTOU)
                        old_reconcile(&b, &s);
                        let racer = format!("race.{}", rng.below(30));
                        new_set(&b, &s, &racer, ttl).await;
                        old_set_script(&b, &s, &key, ttl);
                    }
                    3 => {
                        b.legacy_delete(&s.data_key(&key), &s.legacy_count_key());
                    }
                    4 | 5 => {
                        new_set(&b, &s, &key, ttl).await;
                    }
                    6 => new_delete(&b, &s, &key).await,
                    _ => b.advance(Duration::from_secs(1 + rng.below(3))),
                }
                assert_never_undercounts(&b, &s, &ctx);
                let now_exists = b.legacy_value(&s.legacy_count_key()).is_some();
                assert!(
                    now_exists || !existed_once,
                    "{ctx}: the legacy counter disappeared mid-rollout"
                );
                existed_once |= now_exists;
            }
        }
    }
}
