//! Distributed Discord IDENTIFY budget + RESUME-first session persistence
//! (connector spec `2026-09-28-connector-bundles.md` §2.5, Gemini condition
//! 3, PASS-WITH-CONDITIONS §0 item 3): a Valkey-backed token bucket per bot
//! token enforces Discord's `max_concurrency` IDENTIFY buckets and 5s
//! pacing **fleet-wide** -- every svc-ingest pod shares one counter, never
//! a per-pod-local one, because shard ownership already moves between pods
//! on reassignment (dataplane-scale §3.1) -- plus rolling `session_start_limit`
//! tracking with a low-headroom alert, and a Valkey-backed
//! `session_id`/`seq`/`resume_gateway_url` store so a reconnect always
//! attempts `RESUME` (exempt from the IDENTIFY budget entirely) before
//! ever touching it.
//!
//! [`BudgetedResumingConnector`] is the integration point: it wraps any
//! [`super::discord::GatewayConnector`] and *is itself* one, so
//! `super::discord::run_loop` (and its ~20 existing tests) needs no
//! changes at all -- every reconnect it drives via `connector.connect()`
//! transparently gets RESUME-first ordering and budget-gated IDENTIFY.
//!
//! **Known upstream gap (tracked, not fixed here):** the pinned
//! `penguin-connector-discord` crate (external, `penguin-libs` git rev, see
//! `core/svc_ingest/Cargo.toml`) does not implement the `OP_RESUME` wire
//! frame yet, and its opcode-9 (`INVALID_SESSION`) handling collapses the
//! `d` resumable/non-resumable flag to always non-resumable -- see
//! [`penguin_connector_discord::gateway::DiscordError::SessionInvalidated`]'s
//! own doc comment. [`super::discord::GatewayConnector::resume`]'s default
//! impl therefore always reports the session non-resumable for the real
//! production connector today, and [`super::discord::GatewayChannel::session_snapshot`]'s
//! default returns `None` (nothing to persist yet). Every piece of
//! orchestration in this module -- the budget, the store, RESUME-first
//! ordering, jittered backoff -- is nonetheless fully implemented and
//! exercised end-to-end in this module's tests against a fake connector
//! that *does* support both, so wiring a real `penguin-libs` bump is a
//! two-method override, not a design change.

use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher};
use std::time::Duration;

use penguin_connector_discord::DiscordError;

use crate::ingest::discord::{GatewayChannel, GatewayConnector};
use crate::telemetry::ReceiverHealthMetrics;

/// A shard's persisted resumable session state -- Discord's `READY`
/// dispatch fields needed to `RESUME` instead of re-`IDENTIFY`ing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoredSession {
    /// Discord's opaque session id from `READY`.
    pub session_id: String,
    /// Last dispatch sequence number seen, sent back in `RESUME`.
    pub seq: Option<u64>,
    /// The per-session resume URL Discord's `READY` provides -- `RESUME`
    /// must reconnect to this URL, not the original gateway URL.
    pub resume_gateway_url: String,
}

/// Error from a [`SessionStore`] operation -- always retryable from the
/// caller's perspective (worst case: fall back to a fresh, budgeted
/// IDENTIFY), never a reason to crash the ingest loop.
#[derive(Debug, thiserror::Error)]
#[error("discord session store error: {0}")]
pub struct SessionStoreError(pub String);

/// Persists/loads one shard's [`StoredSession`], keyed by an opaque
/// `shard_key` (never the bot token). Implemented for Valkey
/// ([`RedisSessionStore`]); fakeable in tests.
#[allow(async_fn_in_trait)] // internal service trait, mirrors `super::discord::GatewayConnector`
pub trait SessionStore: Send + Sync {
    /// Loads the shard's stored session, if any and not TTL-expired.
    async fn load(&self, shard_key: &str) -> Result<Option<StoredSession>, SessionStoreError>;
    /// Persists `session` for `shard_key`, refreshing its TTL so a
    /// long-dead shard's session silently expires instead of being
    /// resumed against stale state.
    async fn save(
        &self,
        shard_key: &str,
        session: &StoredSession,
        ttl: Duration,
    ) -> Result<(), SessionStoreError>;
    /// Clears a shard's stored session (a non-resumable `INVALID_SESSION`
    /// or a `resume()` failure).
    async fn clear(&self, shard_key: &str) -> Result<(), SessionStoreError>;
}

/// Valkey-backed [`SessionStore`]: a `HASH` per shard under
/// `waddles:discord:session:{shard_key}`, TTL-refreshed on every save.
pub struct RedisSessionStore {
    conn: redis::aio::MultiplexedConnection,
}

impl RedisSessionStore {
    /// Wraps an already-connected Valkey multiplexed connection (cheap to
    /// clone per call, matching `RedisReplayGuard`/`RedisRevocationSink`'s
    /// own pattern in `crate::ingest::twitch_eventsub`).
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self { conn }
    }

    fn key(shard_key: &str) -> String {
        format!("waddles:discord:session:{shard_key}")
    }
}

impl SessionStore for RedisSessionStore {
    async fn load(&self, shard_key: &str) -> Result<Option<StoredSession>, SessionStoreError> {
        let mut conn = self.conn.clone();
        let fields: HashMap<String, String> =
            redis::AsyncCommands::hgetall(&mut conn, Self::key(shard_key))
                .await
                .map_err(|e| SessionStoreError(e.to_string()))?;
        if fields.is_empty() {
            return Ok(None);
        }
        let session_id = fields.get("session_id").cloned().unwrap_or_default();
        let resume_gateway_url = fields
            .get("resume_gateway_url")
            .cloned()
            .unwrap_or_default();
        if session_id.is_empty() || resume_gateway_url.is_empty() {
            return Ok(None);
        }
        let seq = fields.get("seq").and_then(|s| s.parse::<u64>().ok());
        Ok(Some(StoredSession {
            session_id,
            seq,
            resume_gateway_url,
        }))
    }

    async fn save(
        &self,
        shard_key: &str,
        session: &StoredSession,
        ttl: Duration,
    ) -> Result<(), SessionStoreError> {
        let mut conn = self.conn.clone();
        let key = Self::key(shard_key);
        let mut items: Vec<(&str, String)> = vec![
            ("session_id", session.session_id.clone()),
            ("resume_gateway_url", session.resume_gateway_url.clone()),
        ];
        if let Some(seq) = session.seq {
            items.push(("seq", seq.to_string()));
        }
        redis::AsyncCommands::hset_multiple::<_, _, _, ()>(&mut conn, &key, &items)
            .await
            .map_err(|e| SessionStoreError(e.to_string()))?;
        redis::AsyncCommands::expire::<_, ()>(&mut conn, &key, ttl.as_secs().max(1) as i64)
            .await
            .map_err(|e| SessionStoreError(e.to_string()))?;
        Ok(())
    }

    async fn clear(&self, shard_key: &str) -> Result<(), SessionStoreError> {
        let mut conn = self.conn.clone();
        redis::AsyncCommands::del::<_, ()>(&mut conn, Self::key(shard_key))
            .await
            .map_err(|e| SessionStoreError(e.to_string()))?;
        Ok(())
    }
}

/// Error from an [`IdentifyBudget`] operation.
#[derive(Debug, thiserror::Error)]
#[error("discord identify budget error: {0}")]
pub struct IdentifyBudgetError(pub String);

/// Outcome of an [`IdentifyBudget::acquire`] call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquireOutcome {
    /// Slot granted fleet-wide -- IDENTIFY now.
    Granted,
    /// This bucket's 5s window is already spent by another pod; wait this
    /// long (jitter it, don't just sleep exactly this) then retry.
    Wait(Duration),
    /// The bot token's rolling 24h `session_start_limit` is exhausted --
    /// Discord resets the whole token's sessions on breach, so this is a
    /// hard stop, not a soft warning, until headroom returns.
    DailyLimitExhausted,
}

/// Alert once the rolling 24h `session_start_limit` headroom drops to or
/// below this fraction of the token's total (the same alert-before-breach
/// posture as the Twitch cost-budget alert, connections-credentials §3.2).
pub const DAILY_LIMIT_ALERT_FRACTION: f64 = 0.2;

/// Enforces the distributed IDENTIFY budget for a bot token. Implemented
/// for Valkey ([`RedisIdentifyBudget`]); fakeable in tests.
#[allow(async_fn_in_trait)]
pub trait IdentifyBudget: Send + Sync {
    /// Attempts to reserve one IDENTIFY slot for `bucket` (the caller
    /// computes `shard_id % max_concurrency`, dataplane-scale §3.1) under
    /// `token_hash` -- a SHA-256 hex digest of the bot token, *never* the
    /// token itself (see [`hash_bot_token`]).
    async fn acquire(
        &self,
        token_hash: &str,
        bucket: u32,
        max_concurrency: u32,
    ) -> Result<AcquireOutcome, IdentifyBudgetError>;

    /// Records the latest `/gateway/bot` `session_start_limit` snapshot so
    /// [`Self::acquire`] can enforce the rolling daily ceiling. Returns
    /// `true` when `remaining` is at or below [`DAILY_LIMIT_ALERT_FRACTION`]
    /// of `total`, so the caller can page.
    async fn record_session_start_limit(
        &self,
        token_hash: &str,
        total: u32,
        remaining: u32,
        reset_after: Duration,
    ) -> Result<bool, IdentifyBudgetError>;
}

/// `EVAL`-ed atomically so check-and-increment races across every pod are
/// impossible: rejects outright if the rolling daily counter has hit zero,
/// otherwise enforces the bucket's 5s window (`PTTL`/`SET ... PX`) and only
/// then decrements the daily counter -- a `Wait` outcome never touches the
/// daily counter at all.
const IDENTIFY_BUDGET_ACQUIRE_SCRIPT: &str = r"
local daily_remaining = tonumber(redis.call('GET', KEYS[2]) or '-1')
if daily_remaining == 0 then
  return {'exhausted', 0}
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl and ttl > 0 then
  return {'wait', ttl}
end
redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
if daily_remaining > 0 then
  redis.call('DECR', KEYS[2])
end
return {'granted', 0}
";

/// Discord's per-bucket IDENTIFY pacing window (§2.5).
const IDENTIFY_WINDOW_MS: i64 = 5000;
const IDENTIFY_WINDOW: Duration = Duration::from_millis(5000);

/// Valkey-backed [`IdentifyBudget`]: `waddles:discord:identify-budget:{token_hash}:{bucket}`
/// (5s window) and `...{token_hash}:daily` (rolling `session_start_limit`
/// mirror), both touched atomically by one Lua script per `acquire` call.
pub struct RedisIdentifyBudget {
    conn: redis::aio::MultiplexedConnection,
    script: redis::Script,
}

impl RedisIdentifyBudget {
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self {
            conn,
            script: redis::Script::new(IDENTIFY_BUDGET_ACQUIRE_SCRIPT),
        }
    }

    fn bucket_key(token_hash: &str, bucket: u32) -> String {
        format!("waddles:discord:identify-budget:{token_hash}:{bucket}")
    }

    fn daily_key(token_hash: &str) -> String {
        format!("waddles:discord:identify-budget:{token_hash}:daily")
    }
}

impl IdentifyBudget for RedisIdentifyBudget {
    async fn acquire(
        &self,
        token_hash: &str,
        bucket: u32,
        _max_concurrency: u32,
    ) -> Result<AcquireOutcome, IdentifyBudgetError> {
        let mut conn = self.conn.clone();
        let (status, wait_ms): (String, i64) = self
            .script
            .key(Self::bucket_key(token_hash, bucket))
            .key(Self::daily_key(token_hash))
            .arg(IDENTIFY_WINDOW_MS)
            .invoke_async(&mut conn)
            .await
            .map_err(|e| IdentifyBudgetError(e.to_string()))?;
        Ok(match status.as_str() {
            "granted" => AcquireOutcome::Granted,
            "exhausted" => AcquireOutcome::DailyLimitExhausted,
            _ => AcquireOutcome::Wait(Duration::from_millis(
                u64::try_from(wait_ms.max(0)).unwrap_or(0),
            )),
        })
    }

    async fn record_session_start_limit(
        &self,
        token_hash: &str,
        total: u32,
        remaining: u32,
        reset_after: Duration,
    ) -> Result<bool, IdentifyBudgetError> {
        let mut conn = self.conn.clone();
        redis::AsyncCommands::set_ex::<_, _, ()>(
            &mut conn,
            Self::daily_key(token_hash),
            remaining,
            reset_after.as_secs().max(1),
        )
        .await
        .map_err(|e| IdentifyBudgetError(e.to_string()))?;
        Ok(is_low_headroom(total, remaining))
    }
}

fn is_low_headroom(total: u32, remaining: u32) -> bool {
    if total == 0 {
        return false;
    }
    (f64::from(remaining) / f64::from(total)) <= DAILY_LIMIT_ALERT_FRACTION
}

/// SHA-256 hex digest of the bot token -- the only thing ever used as a
/// Valkey key component; the raw token itself never reaches Valkey (or
/// this module's own logs).
#[must_use]
pub fn hash_bot_token(token: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Zero-dependency jitter: `RandomState`'s per-instance OS-seeded hasher
/// gives an effectively random `u64` without pulling in the `rand` crate
/// for one call site. Bounded to `[0, max)`; `max == 0` yields zero.
fn jitter(max: Duration) -> Duration {
    let max_nanos = u64::try_from(max.as_nanos()).unwrap_or(u64::MAX);
    if max_nanos == 0 {
        return Duration::ZERO;
    }
    let draw = std::collections::hash_map::RandomState::new()
        .build_hasher()
        .finish();
    Duration::from_nanos(draw % max_nanos)
}

/// Records a `/gateway/bot` `session_start_limit` snapshot and alerts (a
/// log line and a [`ReceiverHealthMetrics`] counter) when headroom has
/// dropped to [`DAILY_LIMIT_ALERT_FRACTION`] or below -- call this each
/// time `svc_ingest`'s REST client fetches a fresh snapshot.
pub async fn record_and_alert_session_start_limit<IB, M>(
    budget: &IB,
    metrics: &M,
    token_hash: &str,
    total: u32,
    remaining: u32,
    reset_after: Duration,
) -> Result<(), IdentifyBudgetError>
where
    IB: IdentifyBudget,
    M: ReceiverHealthMetrics,
{
    let low = budget
        .record_session_start_limit(token_hash, total, remaining, reset_after)
        .await?;
    if low {
        tracing::error!(
            platform = "discord",
            token_hash,
            remaining,
            total,
            "discord IDENTIFY daily session-start budget running low"
        );
        metrics.receiver_reconnect("discord", "identify_budget_low");
    }
    Ok(())
}

/// Bounded wait applied while polling [`IdentifyBudget::acquire`] for a
/// `Wait` outcome -- capped so a stale/huge `PTTL` reading can never stall
/// a reconnect for longer than one IDENTIFY window plus jitter.
fn capped_wait(d: Duration) -> Duration {
    d.min(IDENTIFY_WINDOW) + jitter(Duration::from_millis(250))
}

/// Wraps a [`GatewayConnector`] with RESUME-first ordering and
/// budget-gated IDENTIFY (spec §2.5) -- itself a [`GatewayConnector`], so
/// `super::discord::run_loop` drives it exactly like the bare connector it
/// wraps and needs no changes.
pub struct BudgetedResumingConnector<C, SS, IB, M> {
    inner: C,
    session_store: SS,
    identify_budget: IB,
    metrics: M,
    token_hash: String,
    shard_key: String,
    shard_id: u32,
    max_concurrency: u32,
    session_ttl: Duration,
}

impl<C, SS, IB, M> BudgetedResumingConnector<C, SS, IB, M> {
    /// `token_hash` should come from [`hash_bot_token`]; `shard_key`
    /// identifies this shard's stored session (e.g. `dg-{shard_id}`).
    #[must_use]
    #[allow(clippy::too_many_arguments)] // every field is independently configured; see `super::discord::run_loop`'s own precedent
    pub fn new(
        inner: C,
        session_store: SS,
        identify_budget: IB,
        metrics: M,
        token_hash: String,
        shard_key: String,
        shard_id: u32,
        max_concurrency: u32,
        session_ttl: Duration,
    ) -> Self {
        Self {
            inner,
            session_store,
            identify_budget,
            metrics,
            token_hash,
            shard_key,
            shard_id,
            max_concurrency: max_concurrency.max(1),
            session_ttl,
        }
    }

    async fn persist_snapshot(&self, channel: &C::Channel)
    where
        C: GatewayConnector,
        SS: SessionStore,
    {
        if let Some(session) = channel.session_snapshot() {
            if let Err(err) = self
                .session_store
                .save(&self.shard_key, &session, self.session_ttl)
                .await
            {
                tracing::warn!(platform = "discord", shard_key = %self.shard_key, error = %err, "failed to persist discord session for future RESUME");
            }
        }
    }
}

impl<C, SS, IB, M> GatewayConnector for BudgetedResumingConnector<C, SS, IB, M>
where
    C: GatewayConnector,
    SS: SessionStore,
    IB: IdentifyBudget,
    M: ReceiverHealthMetrics + Send + Sync,
{
    type Channel = C::Channel;

    async fn resume(&self, session: &StoredSession) -> Result<Self::Channel, DiscordError> {
        self.inner.resume(session).await
    }

    async fn connect(&self) -> Result<Self::Channel, DiscordError> {
        // RESUME-first (mandatory ordering, §2.5): a reconnect always
        // tries RESUME before ever consulting the IDENTIFY budget --
        // Discord exempts RESUME from the IDENTIFY rate limit entirely,
        // so a healthy resume path never touches the budget below.
        match self.session_store.load(&self.shard_key).await {
            Ok(Some(session)) => match self.inner.resume(&session).await {
                Ok(channel) => {
                    self.metrics.receiver_reconnect("discord", "resume_success");
                    self.persist_snapshot(&channel).await;
                    return Ok(channel);
                }
                Err(DiscordError::ResumeRequested) => {
                    // Opcode 7 during the resume attempt itself: still
                    // possibly resumable, but don't hot-loop -- fall
                    // through to a budgeted fresh IDENTIFY this cycle;
                    // the stored session (left intact) gets another
                    // RESUME attempt on the next reconnect.
                    self.metrics
                        .receiver_reconnect("discord", "resume_transient_retry");
                }
                Err(err) => {
                    // Opcode 9 (`INVALID_SESSION`), non-resumable: clear
                    // the stale session and fall back to a fresh,
                    // budgeted IDENTIFY.
                    tracing::warn!(platform = "discord", shard_key = %self.shard_key, error = %err, "RESUME failed (session not resumable), falling back to IDENTIFY");
                    self.metrics
                        .receiver_reconnect("discord", "resume_fallback_identify");
                    if let Err(clear_err) = self.session_store.clear(&self.shard_key).await {
                        tracing::warn!(platform = "discord", error = %clear_err, "failed to clear stale discord session");
                    }
                }
            },
            Ok(None) => {}
            Err(err) => {
                tracing::warn!(platform = "discord", error = %err, "session store load failed, proceeding straight to budgeted IDENTIFY");
            }
        }

        // Budget-gated fresh IDENTIFY: only reached when RESUME was
        // unavailable or failed above.
        let bucket = self.shard_id % self.max_concurrency;
        loop {
            match self
                .identify_budget
                .acquire(&self.token_hash, bucket, self.max_concurrency)
                .await
            {
                Ok(AcquireOutcome::Granted) => {
                    let channel = self.inner.connect().await?;
                    self.metrics.receiver_reconnect("discord", "identify_ok");
                    self.persist_snapshot(&channel).await;
                    return Ok(channel);
                }
                Ok(AcquireOutcome::Wait(d)) => {
                    self.metrics
                        .receiver_reconnect("discord", "identify_budget_wait");
                    tokio::time::sleep(capped_wait(d)).await;
                }
                Ok(AcquireOutcome::DailyLimitExhausted) => {
                    self.metrics
                        .receiver_reconnect("discord", "identify_budget_exhausted");
                    tracing::error!(platform = "discord", token_hash = %self.token_hash, "IDENTIFY daily session-start budget exhausted, backing off");
                    tokio::time::sleep(Duration::from_secs(30) + jitter(Duration::from_secs(5)))
                        .await;
                }
                Err(err) => {
                    // Budget store unreachable: fail open to a direct
                    // connect rather than blocking ingest indefinitely on
                    // a Valkey outage -- matches `RedisReplayGuard`'s own
                    // "dedup store outage fails open" precedent
                    // (`ingest::twitch_eventsub`).
                    tracing::warn!(platform = "discord", error = %err, "identify budget store error, failing open to a direct connect");
                    return self.inner.connect().await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap as StdHashMap;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::{Arc, Mutex};

    /// Simulates the Lua script's atomic fleet-wide state with a plain
    /// `Mutex` -- multiple `InMemoryBudget` handles cloned from the same
    /// `Arc` model "every svc-ingest pod racing to IDENTIFY at once"
    /// against one shared Valkey.
    #[derive(Default)]
    struct SharedBudgetState {
        /// bucket key -> instant the 5s window expires.
        windows: StdHashMap<String, tokio::time::Instant>,
        daily_remaining: Option<i64>,
        daily_total: u32,
    }

    #[derive(Clone)]
    struct InMemoryBudget {
        state: Arc<Mutex<SharedBudgetState>>,
    }

    impl InMemoryBudget {
        fn new() -> Self {
            Self {
                state: Arc::new(Mutex::new(SharedBudgetState::default())),
            }
        }
    }

    impl IdentifyBudget for InMemoryBudget {
        async fn acquire(
            &self,
            token_hash: &str,
            bucket: u32,
            _max_concurrency: u32,
        ) -> Result<AcquireOutcome, IdentifyBudgetError> {
            let mut state = self.state.lock().unwrap();
            if state.daily_remaining == Some(0) {
                return Ok(AcquireOutcome::DailyLimitExhausted);
            }
            let key = format!("{token_hash}:{bucket}");
            let now = tokio::time::Instant::now();
            if let Some(expiry) = state.windows.get(&key) {
                if *expiry > now {
                    return Ok(AcquireOutcome::Wait(*expiry - now));
                }
            }
            state.windows.insert(key, now + IDENTIFY_WINDOW);
            if let Some(remaining) = state.daily_remaining {
                state.daily_remaining = Some(remaining - 1);
            }
            Ok(AcquireOutcome::Granted)
        }

        async fn record_session_start_limit(
            &self,
            _token_hash: &str,
            total: u32,
            remaining: u32,
            _reset_after: Duration,
        ) -> Result<bool, IdentifyBudgetError> {
            let mut state = self.state.lock().unwrap();
            state.daily_remaining = Some(i64::from(remaining));
            state.daily_total = total;
            Ok(is_low_headroom(total, remaining))
        }
    }

    #[derive(Clone, Default)]
    struct InMemorySessionStore {
        sessions: Arc<Mutex<StdHashMap<String, StoredSession>>>,
    }

    impl SessionStore for InMemorySessionStore {
        async fn load(&self, shard_key: &str) -> Result<Option<StoredSession>, SessionStoreError> {
            Ok(self.sessions.lock().unwrap().get(shard_key).cloned())
        }

        async fn save(
            &self,
            shard_key: &str,
            session: &StoredSession,
            _ttl: Duration,
        ) -> Result<(), SessionStoreError> {
            self.sessions
                .lock()
                .unwrap()
                .insert(shard_key.to_string(), session.clone());
            Ok(())
        }

        async fn clear(&self, shard_key: &str) -> Result<(), SessionStoreError> {
            self.sessions.lock().unwrap().remove(shard_key);
            Ok(())
        }
    }

    #[derive(Default)]
    struct RecordingMetrics {
        events: Mutex<Vec<(String, String)>>,
    }

    impl ReceiverHealthMetrics for RecordingMetrics {
        fn receiver_reconnect(&self, platform: &str, reason: &str) {
            self.events
                .lock()
                .unwrap()
                .push((platform.to_string(), reason.to_string()));
        }
    }

    /// A fake connector whose `connect()`/`resume()` are independently
    /// scriptable and whose channel carries a settable session snapshot --
    /// lets these tests exercise RESUME-first ordering and opcode-9
    /// fallback without a live gateway.
    struct FakeChannel {
        snapshot: Option<StoredSession>,
    }

    impl GatewayChannel for FakeChannel {
        async fn next_chat_message(
            &mut self,
        ) -> Result<Option<penguin_connector_discord::gateway::ChatMessage>, DiscordError> {
            Ok(None)
        }

        fn session_snapshot(&self) -> Option<StoredSession> {
            self.snapshot.clone()
        }
    }

    struct FakeConnector {
        resume_calls: AtomicU32,
        connect_calls: AtomicU32,
        /// If set, `resume()` returns this error instead of succeeding.
        resume_error: Option<fn() -> DiscordError>,
        next_session: StoredSession,
    }

    impl FakeConnector {
        fn new(resume_error: Option<fn() -> DiscordError>) -> Self {
            Self {
                resume_calls: AtomicU32::new(0),
                connect_calls: AtomicU32::new(0),
                resume_error,
                next_session: StoredSession {
                    session_id: "sess-1".to_string(),
                    seq: Some(42),
                    resume_gateway_url: "wss://resume.example".to_string(),
                },
            }
        }
    }

    impl GatewayConnector for FakeConnector {
        type Channel = FakeChannel;

        async fn connect(&self) -> Result<Self::Channel, DiscordError> {
            self.connect_calls.fetch_add(1, Ordering::SeqCst);
            Ok(FakeChannel {
                snapshot: Some(self.next_session.clone()),
            })
        }

        async fn resume(&self, _session: &StoredSession) -> Result<Self::Channel, DiscordError> {
            self.resume_calls.fetch_add(1, Ordering::SeqCst);
            if let Some(err_fn) = self.resume_error {
                return Err(err_fn());
            }
            Ok(FakeChannel {
                snapshot: Some(self.next_session.clone()),
            })
        }
    }

    fn shard_key() -> String {
        "dg-0".to_string()
    }

    #[tokio::test]
    async fn resume_is_used_when_a_valid_session_is_stored() {
        let sessions = InMemorySessionStore::default();
        sessions
            .save(
                &shard_key(),
                &StoredSession {
                    session_id: "existing".to_string(),
                    seq: Some(7),
                    resume_gateway_url: "wss://resume.example".to_string(),
                },
                Duration::from_secs(600),
            )
            .await
            .unwrap();

        let inner = FakeConnector::new(None);
        let adapter = BudgetedResumingConnector::new(
            inner,
            sessions,
            InMemoryBudget::new(),
            RecordingMetrics::default(),
            "tokhash".to_string(),
            shard_key(),
            0,
            1,
            Duration::from_secs(600),
        );

        let _channel = adapter.connect().await.expect("connect should succeed");
        assert_eq!(adapter.inner.resume_calls.load(Ordering::SeqCst), 1);
        assert_eq!(adapter.inner.connect_calls.load(Ordering::SeqCst), 0);
        assert!(adapter
            .metrics
            .events
            .lock()
            .unwrap()
            .contains(&("discord".to_string(), "resume_success".to_string())));
    }

    #[tokio::test]
    async fn non_resumable_op9_clears_session_and_falls_back_to_identify() {
        let sessions = InMemorySessionStore::default();
        sessions
            .save(
                &shard_key(),
                &StoredSession {
                    session_id: "stale".to_string(),
                    seq: Some(1),
                    resume_gateway_url: "wss://resume.example".to_string(),
                },
                Duration::from_secs(600),
            )
            .await
            .unwrap();

        let inner = FakeConnector::new(Some(|| DiscordError::SessionInvalidated));
        let budget = InMemoryBudget::new();
        let adapter = BudgetedResumingConnector::new(
            inner,
            sessions.clone(),
            budget,
            RecordingMetrics::default(),
            "tokhash".to_string(),
            shard_key(),
            0,
            1,
            Duration::from_secs(600),
        );

        adapter.connect().await.expect("falls back to IDENTIFY");
        assert_eq!(adapter.inner.resume_calls.load(Ordering::SeqCst), 1);
        assert_eq!(adapter.inner.connect_calls.load(Ordering::SeqCst), 1);
        // The stale session is gone -- the fresh IDENTIFY's own
        // `session_snapshot` (a *new* session id) was persisted in its
        // place, not left empty.
        assert_eq!(
            sessions
                .load(&shard_key())
                .await
                .unwrap()
                .unwrap()
                .session_id,
            "sess-1"
        );
        assert!(adapter.metrics.events.lock().unwrap().contains(&(
            "discord".to_string(),
            "resume_fallback_identify".to_string()
        )));
    }

    #[tokio::test]
    async fn no_stored_session_goes_straight_to_budgeted_identify() {
        let inner = FakeConnector::new(None);
        let adapter = BudgetedResumingConnector::new(
            inner,
            InMemorySessionStore::default(),
            InMemoryBudget::new(),
            RecordingMetrics::default(),
            "tokhash".to_string(),
            shard_key(),
            0,
            1,
            Duration::from_secs(600),
        );

        adapter.connect().await.expect("connect should succeed");
        assert_eq!(adapter.inner.resume_calls.load(Ordering::SeqCst), 0);
        assert_eq!(adapter.inner.connect_calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test(start_paused = true)]
    async fn concurrent_reconnects_across_simulated_pods_are_paced_at_5s_per_bucket() {
        // Two "pods" (independent adapters) share one `InMemoryBudget` and
        // race to IDENTIFY for the *same* bucket at the same virtual
        // instant -- exactly the shard-reassignment race §2.5 exists to
        // prevent a per-pod-local counter from missing.
        let shared_budget = InMemoryBudget::new();
        let start = tokio::time::Instant::now();

        let mut handles = Vec::new();
        for _ in 0..2 {
            let inner = FakeConnector::new(None);
            let adapter = BudgetedResumingConnector::new(
                inner,
                InMemorySessionStore::default(),
                shared_budget.clone(),
                RecordingMetrics::default(),
                "tokhash".to_string(),
                shard_key(),
                0, // same shard_id -> same bucket for both simulated pods
                1,
                Duration::from_secs(600),
            );
            handles.push(tokio::spawn(async move {
                adapter.connect().await.unwrap();
                tokio::time::Instant::now()
            }));
        }

        let mut completion_times = Vec::new();
        for h in handles {
            completion_times.push(h.await.unwrap());
        }
        completion_times.sort();

        let gap = completion_times[1] - completion_times[0];
        assert!(
            gap >= Duration::from_secs(5),
            "second pod's IDENTIFY for the same bucket must be paced at least 5s after the first, got {gap:?}"
        );
        // Sanity: this all happened within one bounded virtual window, not
        // an unbounded hang.
        assert!(tokio::time::Instant::now() - start < Duration::from_secs(30));
    }

    #[tokio::test]
    async fn daily_budget_exhaustion_emits_the_alert_metric_and_backs_off() {
        let budget = InMemoryBudget::new();
        budget
            .record_session_start_limit("tokhash", 1000, 0, Duration::from_secs(86400))
            .await
            .unwrap();

        let inner = FakeConnector::new(None);
        let adapter = BudgetedResumingConnector::new(
            inner,
            InMemorySessionStore::default(),
            budget,
            RecordingMetrics::default(),
            "tokhash".to_string(),
            shard_key(),
            0,
            1,
            Duration::from_secs(600),
        );

        let result = tokio::time::timeout(Duration::from_millis(50), adapter.connect()).await;
        assert!(
            result.is_err(),
            "connect() should still be backing off (30s) against an exhausted daily budget, not returning early"
        );
        assert!(adapter.metrics.events.lock().unwrap().contains(&(
            "discord".to_string(),
            "identify_budget_exhausted".to_string()
        )));
        assert_eq!(adapter.inner.connect_calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn record_and_alert_fires_only_when_headroom_is_low() {
        let budget = InMemoryBudget::new();
        let metrics = RecordingMetrics::default();

        record_and_alert_session_start_limit(
            &budget,
            &metrics,
            "tokhash",
            1000,
            900,
            Duration::from_secs(86400),
        )
        .await
        .unwrap();
        assert!(metrics.events.lock().unwrap().is_empty());

        record_and_alert_session_start_limit(
            &budget,
            &metrics,
            "tokhash",
            1000,
            150,
            Duration::from_secs(86400),
        )
        .await
        .unwrap();
        assert!(metrics
            .events
            .lock()
            .unwrap()
            .contains(&("discord".to_string(), "identify_budget_low".to_string())));
    }

    #[test]
    fn hash_bot_token_never_returns_the_raw_token() {
        let digest = hash_bot_token("super-secret-bot-token");
        assert_ne!(digest, "super-secret-bot-token");
        assert_eq!(digest.len(), 64); // SHA-256 hex
    }
}
