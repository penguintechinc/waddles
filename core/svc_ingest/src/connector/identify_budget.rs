//! Reusable, Valkey-backed gateway IDENTIFY budget (connector runtime spec
//! `2026-11-05-connector-runtime-inbound-manager.md` decision 5.4, row I) --
//! the host-module port of the legacy `ingest::discord_identify_budget`.
//!
//! # Guarantees (parity with the legacy module unless marked NEW)
//!
//! * **Fleet-wide, atomic.** One Lua script per [`IdentifyBudget::acquire`]
//!   checks the rolling daily counter, the per-bucket 5s window
//!   (`PTTL` / `SET .. PX`) and decrements the counter in ONE round trip, so
//!   N replicas racing for one bucket never over-grant.
//! * **`max_concurrency` buckets.** Rate key = `shard_id % max_concurrency`;
//!   one IDENTIFY per bucket per 5s window.
//! * **Rolling `session_start_limit`.** The daily counter mirrors Discord's
//!   `remaining`, expires at `reset_after`, and a spent counter is a hard
//!   stop ([`AcquireOutcome::Exhausted`]), not a warning. A low-headroom
//!   alert fires at [`DAILY_LIMIT_ALERT_FRACTION`].
//! * **No raw ids in keys.** Keys carry only a domain-separated SHA-256 of
//!   the bot token ([`BotIdentity`]) and the numeric bucket.
//! * **NEW: fail CLOSED.** A Valkey outage or stall is a typed retryable
//!   [`BudgetError::Store`]; the legacy module failed OPEN (identified
//!   unchecked). An unseeded counter is also refused until a real
//!   `session_start_limit` seeds it (the legacy treated absent as unlimited).
//! * **NEW: typed exhaustion.** [`BudgetError::Exhausted`] carries
//!   `retry_after`; the driver backs the connection off that long instead of
//!   hammering.
//! * **NEW: conservative reseed.** [`IdentifyBudget::record_session_start_limit`]
//!   keeps the LOWER of the stored and the freshly fetched `remaining`, so a
//!   stale fetch can never refund slots another replica already spent.
//!
//! [`DiscordIdentifyGate`] adapts a budget to the driver's
//! [`HandshakeGate`]: RESUME (session id present in the host session token)
//! skips the budget entirely; IDENTIFY acquires a slot, waiting with jitter up
//! to a bounded maximum.

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_host_http::guarded::{GuardError, GuardedClient};
use connection_resolver::ConnectionRef;
use prometheus::{IntCounterVec, Opts, Registry};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use super::handshake_gate::{GateCtx, GateError, GatePermit, HandshakeGate};
use super::secrets::{Destination, TransportSecretSource};
use super::webhook::{BoxFut, SecretError};

/// Discord's per-bucket IDENTIFY pacing window.
pub const IDENTIFY_WINDOW: Duration = Duration::from_millis(5000);

/// Alert once rolling daily headroom is at or below this fraction of total.
pub const DAILY_LIMIT_ALERT_FRACTION: f64 = 0.2;

/// Largest `max_concurrency` accepted from `/gateway/bot` or config (Discord
/// documents 1..=16 for most bots, large bots higher): a bound, not a guess.
pub const MAX_CONCURRENCY_CEILING: u32 = 4096;

/// Floor applied to a reset delay so a zero/garbage `reset_after` never
/// produces an instantly-expiring counter or a zero `retry_after`.
pub const MIN_RESET: Duration = Duration::from_secs(1);

/// Default Valkey call deadline; a stalled store is an outage (fail closed).
pub const DEFAULT_STORE_TIMEOUT: Duration = Duration::from_secs(2);

/// Default longest a connection waits inside the gate for a slot.
pub const DEFAULT_MAX_WAIT: Duration = Duration::from_secs(15);

/// Jitter ceiling added to every wait so replicas desynchronize.
const WAIT_JITTER: Duration = Duration::from_millis(250);

/// Why a budget operation did not yield a slot.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum BudgetError {
    /// The Valkey store failed or stalled: retryable, never "identify anyway".
    #[error("identify budget store unavailable ({0})")]
    Store(&'static str),
    /// The budget is spent; the connection must wait `retry_after`.
    #[error("identify budget exhausted; retry after {retry_after:?}")]
    Exhausted {
        /// Earliest sensible retry (window remainder or daily reset).
        retry_after: Duration,
    },
    /// No usable `session_start_limit` (fetch failed and no host-config
    /// fallback is configured): fail loud, retryable.
    #[error("session_start_limit unavailable ({0})")]
    LimitUnavailable(&'static str),
    /// The bot credential is missing/rejected: terminal (park).
    #[error("bot credential rejected ({0})")]
    Credential(&'static str),
    /// Host configuration is invalid (refused at construction).
    #[error("invalid identify budget config ({0})")]
    Config(&'static str),
}

/// Opaque per-bot key component: a domain-separated SHA-256 of the token.
/// The raw token never reaches Valkey, logs or labels.
#[derive(Clone, PartialEq, Eq)]
pub struct BotIdentity(String);

impl BotIdentity {
    /// Hashes the bot token.
    #[must_use]
    pub fn from_token(token: &str) -> Self {
        let mut h = Sha256::new();
        h.update(b"waddles.identify-budget.v1\0");
        h.update(token.as_bytes());
        Self(format!("{:x}", h.finalize()))
    }

    /// The hex digest (safe to embed in a key).
    #[must_use]
    pub fn as_hex(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for BotIdentity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // First 8 hex chars only: enough to correlate, never the full digest.
        write!(f, "BotIdentity({}..)", &self.0[..8])
    }
}

/// Discord's `session_start_limit` (+ `max_concurrency`), validated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionStartLimit {
    /// Total IDENTIFYs per window (1000 by default).
    pub total: u32,
    /// IDENTIFYs left in the window.
    pub remaining: u32,
    /// Time until the window resets.
    pub reset_after: Duration,
    /// Concurrent IDENTIFY buckets.
    pub max_concurrency: u32,
}

impl SessionStartLimit {
    /// Validates a limit; refuses nonsense rather than guessing.
    ///
    /// # Errors
    /// [`BudgetError::Config`] when `max_concurrency` is 0 or over
    /// [`MAX_CONCURRENCY_CEILING`], or `remaining > total`.
    pub fn new(
        total: u32,
        remaining: u32,
        reset_after: Duration,
        max_concurrency: u32,
    ) -> Result<Self, BudgetError> {
        if max_concurrency == 0 || max_concurrency > MAX_CONCURRENCY_CEILING {
            return Err(BudgetError::Config("max_concurrency out of range"));
        }
        if remaining > total {
            return Err(BudgetError::Config("remaining exceeds total"));
        }
        Ok(Self {
            total,
            remaining,
            reset_after: reset_after.max(MIN_RESET),
            max_concurrency,
        })
    }

    /// Parses a `GET /gateway/bot` response body (`reset_after` is
    /// MILLISECONDS on the wire).
    ///
    /// # Errors
    /// [`BudgetError::LimitUnavailable`] on malformed JSON/missing fields,
    /// [`BudgetError::Config`] on out-of-range values.
    pub fn from_gateway_bot(body: &[u8]) -> Result<Self, BudgetError> {
        #[derive(Deserialize)]
        struct Wire {
            session_start_limit: Inner,
        }
        #[derive(Deserialize)]
        struct Inner {
            total: u32,
            remaining: u32,
            reset_after: u64,
            max_concurrency: u32,
        }
        let w: Wire = serde_json::from_slice(body)
            .map_err(|_| BudgetError::LimitUnavailable("malformed gateway/bot response"))?;
        let i = w.session_start_limit;
        Self::new(
            i.total,
            i.remaining,
            Duration::from_millis(i.reset_after),
            i.max_concurrency,
        )
    }

    /// True when headroom is at/below [`DAILY_LIMIT_ALERT_FRACTION`].
    #[must_use]
    pub fn is_low_headroom(total: u32, remaining: u32) -> bool {
        total != 0 && (f64::from(remaining) / f64::from(total)) <= DAILY_LIMIT_ALERT_FRACTION
    }
}

/// Outcome of one [`IdentifyBudget::acquire`] attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AcquireOutcome {
    /// Slot granted fleet-wide: IDENTIFY now.
    Granted,
    /// This bucket's window is spent by another connection: wait this long.
    Wait(Duration),
    /// The rolling daily counter is spent until `retry_after`.
    Exhausted {
        /// Time until the daily counter resets.
        retry_after: Duration,
    },
    /// No `session_start_limit` has been recorded for this bot yet: the
    /// caller must fetch one and [`IdentifyBudget::record_session_start_limit`].
    Unseeded,
}

/// The distributed IDENTIFY budget. Implemented for Valkey
/// ([`RedisIdentifyBudget`]); fakeable in tests.
pub trait IdentifyBudget: Send + Sync {
    /// Attempts ONE slot reservation for `shard_id`'s bucket. Never sleeps.
    fn acquire(
        &self,
        identity: &BotIdentity,
        shard_id: u32,
    ) -> impl std::future::Future<Output = Result<AcquireOutcome, BudgetError>> + Send;

    /// Records a fresh `session_start_limit` (seeds an unseeded counter,
    /// otherwise keeps the lower `remaining`). Returns `true` when headroom
    /// is low enough to alert.
    fn record_session_start_limit(
        &self,
        identity: &BotIdentity,
        limit: &SessionStartLimit,
    ) -> impl std::future::Future<Output = Result<bool, BudgetError>> + Send;
}

/// Atomic acquire. KEYS[1] = bucket window key, KEYS[2] = limit hash;
/// ARGV[1] = window ms. Same check order as the legacy script (daily
/// exhaustion, then the window, then decrement) -- a `wait` never touches the
/// counter.
const ACQUIRE_SCRIPT: &str = r"
local rem = redis.call('HGET', KEYS[2], 'remaining')
if not rem then
  return {'unseeded', 0}
end
if tonumber(rem) <= 0 then
  return {'exhausted', redis.call('PTTL', KEYS[2])}
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl and ttl > 0 then
  return {'wait', ttl}
end
redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
redis.call('HINCRBY', KEYS[2], 'remaining', -1)
return {'granted', 0}
";

/// Atomic seed/lower. KEYS[1] = limit hash; ARGV = total, remaining,
/// max_concurrency, reset ms. Returns the stored `remaining`.
const RECORD_SCRIPT: &str = r"
local cur = redis.call('HGET', KEYS[1], 'remaining')
local rem = tonumber(ARGV[2])
if cur and tonumber(cur) < rem then
  rem = tonumber(cur)
end
redis.call('HSET', KEYS[1], 'total', ARGV[1], 'remaining', rem, 'max_concurrency', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return rem
";

/// Valkey-backed [`IdentifyBudget`].
///
/// Keys: `waddles:connector:identify-budget:{scope}:{identity_hex}:limit`
/// (hash: total/remaining/max_concurrency, TTL = reset) and
/// `...:{identity_hex}:b{bucket}` (the 5s window).
pub struct RedisIdentifyBudget {
    conn: redis::aio::MultiplexedConnection,
    scope: &'static str,
    window: Duration,
    store_timeout: Duration,
    acquire: redis::Script,
    record: redis::Script,
}

impl RedisIdentifyBudget {
    /// Wraps a connected Valkey connection. `scope` namespaces the platform
    /// (`"discord"`), a host constant.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection, scope: &'static str) -> Self {
        Self {
            conn,
            scope,
            window: IDENTIFY_WINDOW,
            store_timeout: DEFAULT_STORE_TIMEOUT,
            acquire: redis::Script::new(ACQUIRE_SCRIPT),
            record: redis::Script::new(RECORD_SCRIPT),
        }
    }

    /// Overrides the per-bucket window (tests use a short one; production
    /// keeps Discord's 5s).
    #[must_use]
    pub fn with_window(mut self, window: Duration) -> Self {
        self.window = window;
        self
    }

    /// Overrides the Valkey call deadline.
    #[must_use]
    pub fn with_store_timeout(mut self, timeout: Duration) -> Self {
        self.store_timeout = timeout;
        self
    }

    fn limit_key(&self, id: &BotIdentity) -> String {
        format!(
            "waddles:connector:identify-budget:{}:{}:limit",
            self.scope,
            id.as_hex()
        )
    }

    fn bucket_key(&self, id: &BotIdentity, bucket: u32) -> String {
        format!(
            "waddles:connector:identify-budget:{}:{}:b{bucket}",
            self.scope,
            id.as_hex()
        )
    }

    /// Runs `fut` under the store deadline, mapping every failure to a
    /// class-only [`BudgetError::Store`] (the redis error text can embed the
    /// address; never surfaced).
    async fn guarded<T>(
        &self,
        fut: impl std::future::Future<Output = redis::RedisResult<T>>,
    ) -> Result<T, BudgetError> {
        match tokio::time::timeout(self.store_timeout, fut).await {
            Err(_) => Err(BudgetError::Store("timeout")),
            Ok(Err(e)) if e.is_timeout() => Err(BudgetError::Store("timeout")),
            Ok(Err(e)) if e.is_connection_refusal() || e.is_connection_dropped() => {
                Err(BudgetError::Store("connection"))
            }
            Ok(Err(_)) => Err(BudgetError::Store("command")),
            Ok(Ok(v)) => Ok(v),
        }
    }
}

impl IdentifyBudget for RedisIdentifyBudget {
    async fn acquire(
        &self,
        identity: &BotIdentity,
        shard_id: u32,
    ) -> Result<AcquireOutcome, BudgetError> {
        let mut conn = self.conn.clone();
        let limit_key = self.limit_key(identity);
        let mc: Option<u32> = self
            .guarded(async {
                redis::cmd("HGET")
                    .arg(&limit_key)
                    .arg("max_concurrency")
                    .query_async(&mut conn)
                    .await
            })
            .await?;
        let Some(mc) = mc.filter(|m| *m >= 1) else {
            return Ok(AcquireOutcome::Unseeded);
        };
        let bucket = shard_id % mc;
        let window_ms = millis(self.window);
        let mut inv = self.acquire.prepare_invoke();
        inv.key(self.bucket_key(identity, bucket))
            .key(&limit_key)
            .arg(window_ms);
        let (status, ms): (String, i64) = self.guarded(inv.invoke_async(&mut conn)).await?;
        let ms = ms.max(0).unsigned_abs();
        match status.as_str() {
            "granted" => Ok(AcquireOutcome::Granted),
            "wait" => Ok(AcquireOutcome::Wait(Duration::from_millis(ms))),
            "exhausted" => Ok(AcquireOutcome::Exhausted {
                retry_after: Duration::from_millis(ms).max(MIN_RESET),
            }),
            "unseeded" => Ok(AcquireOutcome::Unseeded),
            _ => Err(BudgetError::Store("unexpected script reply")),
        }
    }

    async fn record_session_start_limit(
        &self,
        identity: &BotIdentity,
        limit: &SessionStartLimit,
    ) -> Result<bool, BudgetError> {
        let mut conn = self.conn.clone();
        let reset_ms = millis(limit.reset_after);
        let mut inv = self.record.prepare_invoke();
        inv.key(self.limit_key(identity))
            .arg(limit.total)
            .arg(limit.remaining)
            .arg(limit.max_concurrency)
            .arg(reset_ms);
        let stored: i64 = self.guarded(inv.invoke_async(&mut conn)).await?;
        let stored = u32::try_from(stored.max(0)).unwrap_or(u32::MAX);
        Ok(SessionStartLimit::is_low_headroom(limit.total, stored))
    }
}

/// `connector_identify_budget_total{outcome}` -- bounded `outcome` labels:
/// `granted`, `waited`, `exhausted`, `store_error`, `limit_unavailable`,
/// `limit_fallback`, `low_headroom`, `resume_skipped`, `resume_mismatch`, `credential_rejected`.
#[derive(Clone)]
pub struct IdentifyBudgetMetrics {
    total: IntCounterVec,
}

impl IdentifyBudgetMetrics {
    /// Registers the counter on `registry` (once per registry).
    ///
    /// # Errors
    /// A duplicate registration is a typed error.
    pub fn register(registry: &Registry) -> Result<Self, prometheus::Error> {
        let total = IntCounterVec::new(
            Opts::new(
                "connector_identify_budget_total",
                "Gateway IDENTIFY budget decisions by outcome",
            ),
            &["outcome"],
        )?;
        registry.register(Box::new(total.clone()))?;
        Ok(Self { total })
    }

    /// Counts one decision. `outcome` MUST be one of the documented statics.
    pub fn inc(&self, outcome: &'static str) {
        self.total.with_label_values(&[outcome]).inc();
    }
}

/// Where the gate gets the bot identity and the session-start limit. The
/// production impl is [`GatewayBotSource`]; tests substitute fakes.
pub trait GatewayInfoSource: Send + Sync {
    /// The bot's key identity (resolves the token fresh; hashes; drops it).
    fn identity(&self) -> BoxFut<'_, Result<BotIdentity, BudgetError>>;
    /// A fresh `session_start_limit`.
    fn session_start_limit(&self) -> BoxFut<'_, Result<SessionStartLimit, BudgetError>>;
}

/// Production [`GatewayInfoSource`]: `GET /gateway/bot` through the SSRF
/// [`GuardedClient`], the bot token resolved per use through the host
/// secret source (never held, never logged), with an optional explicit
/// host-config fallback used -- loudly, counted -- only when the fetch fails.
pub struct GatewayBotSource<S: TransportSecretSource> {
    client: GuardedClient,
    secrets: Arc<S>,
    connection: ConnectionRef,
    api_base: String,
    fallback: Option<SessionStartLimit>,
    metrics: Option<IdentifyBudgetMetrics>,
}

/// Production Discord REST base.
pub const DISCORD_API_BASE: &str = "https://discord.com/api/v10";

impl<S: TransportSecretSource> GatewayBotSource<S> {
    /// Builds a source for one bot connection.
    #[must_use]
    pub fn new(
        client: GuardedClient,
        secrets: Arc<S>,
        connection: ConnectionRef,
        api_base: impl Into<String>,
    ) -> Self {
        Self {
            client,
            secrets,
            connection,
            api_base: api_base.into(),
            fallback: None,
            metrics: None,
        }
    }

    /// Host-config limit used ONLY when `GET /gateway/bot` fails (counted as
    /// `limit_fallback` and logged at ERROR). Without it a failed fetch is a
    /// retryable [`BudgetError::LimitUnavailable`].
    #[must_use]
    pub fn with_config_fallback(mut self, limit: SessionStartLimit) -> Self {
        self.fallback = Some(limit);
        self
    }

    /// Attaches the metrics sink for the fallback counter.
    #[must_use]
    pub fn with_metrics(mut self, metrics: IdentifyBudgetMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    fn gateway_bot_url(&self) -> String {
        format!("{}/gateway/bot", self.api_base.trim_end_matches('/'))
    }

    /// Resolves the bot token for the approved destination. The returned
    /// `ResolvedAuth` zeroizes on drop and has a redacted `Debug`.
    async fn token(&self) -> Result<super::secrets::ResolvedAuth, BudgetError> {
        let url = reqwest::Url::parse(&self.gateway_bot_url())
            .map_err(|_| BudgetError::Config("invalid api base url"))?;
        let dest = Destination::from_url(&url)
            .ok_or(BudgetError::Config("api base url has no host/port"))?;
        self.secrets
            .resolve(self.connection, dest)
            .await
            .map_err(|e| match e {
                SecretError::Missing => BudgetError::Credential("secret missing"),
                SecretError::Unavailable => BudgetError::Store("secret backend"),
            })
    }

    async fn fetch(&self) -> Result<SessionStartLimit, BudgetError> {
        let auth = self.token().await?;
        let header = zeroize::Zeroizing::new(format!("Bot {}", auth.secret.expose()));
        let resp = self
            .client
            .fetch(
                reqwest::Method::GET,
                &self.gateway_bot_url(),
                vec![
                    ("authorization".to_string(), (*header).clone()),
                    ("accept".to_string(), "application/json".to_string()),
                ],
                None,
            )
            .await
            .map_err(|e: GuardError| BudgetError::LimitUnavailable(e.class()))?;
        match resp.status {
            200..=299 => SessionStartLimit::from_gateway_bot(&resp.body),
            401 | 403 => Err(BudgetError::Credential("gateway/bot rejected the token")),
            429 => Err(BudgetError::LimitUnavailable("gateway/bot rate limited")),
            _ => Err(BudgetError::LimitUnavailable("gateway/bot http status")),
        }
    }
}

impl<S: TransportSecretSource + 'static> GatewayInfoSource for GatewayBotSource<S> {
    fn identity(&self) -> BoxFut<'_, Result<BotIdentity, BudgetError>> {
        Box::pin(async move {
            let auth = self.token().await?;
            Ok(BotIdentity::from_token(auth.secret.expose()))
        })
    }

    fn session_start_limit(&self) -> BoxFut<'_, Result<SessionStartLimit, BudgetError>> {
        Box::pin(async move {
            match self.fetch().await {
                Ok(l) => Ok(l),
                // A dead credential is never papered over by config.
                Err(e @ (BudgetError::Credential(_) | BudgetError::Config(_))) => Err(e),
                Err(e) => match self.fallback {
                    Some(l) => {
                        tracing::error!(error = %e, "gateway/bot fetch failed; using the host-config session_start_limit");
                        if let Some(m) = &self.metrics {
                            m.inc("limit_fallback");
                        }
                        Ok(l)
                    }
                    None => Err(e),
                },
            }
        })
    }
}

/// Saturating whole milliseconds of `d`.
fn millis(d: Duration) -> u64 {
    d.as_secs()
        .saturating_mul(1000)
        .saturating_add(u64::from(d.subsec_millis()))
}

/// Zero-dependency jitter in `[0, max)`.
fn jitter(max: Duration) -> Duration {
    let max_nanos = max
        .as_secs()
        .saturating_mul(1_000_000_000)
        .saturating_add(u64::from(max.subsec_nanos()));
    if max_nanos == 0 {
        return Duration::ZERO;
    }
    let draw = RandomState::new().build_hasher().finish();
    Duration::from_nanos(draw % max_nanos)
}

/// True when the host session token names a resumable `session_id` (the
/// handshake will be a RESUME, which Discord exempts from the IDENTIFY
/// budget). A missing or non-JSON token is NOT resumable: the conservative
/// answer consumes budget rather than skipping it.
#[must_use]
pub fn session_token_is_resumable(token: Option<&str>) -> bool {
    let Some(t) = token else { return false };
    match serde_json::from_str::<serde_json::Value>(t) {
        Ok(v) => v
            .get("session_id")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|s| !s.is_empty()),
        Err(_) => false,
    }
}

/// Host-anchored prefix of a Discord RESUME frame (`op` 6, token first). The
/// budget exemption applies ONLY to a handshake starting with exactly this.
pub const RESUME_PREFIX: &[u8] = br#"{"op":6,"d":{"token":""#;

/// The Discord [`HandshakeGate`]: RESUME skips the budget; IDENTIFY acquires
/// a slot (seeding the counter from `/gateway/bot` when needed), waiting with
/// jitter up to `max_wait`.
pub struct DiscordIdentifyGate<B: IdentifyBudget, L: GatewayInfoSource> {
    budget: Arc<B>,
    source: Arc<L>,
    shard_id: u32,
    max_wait: Duration,
    metrics: IdentifyBudgetMetrics,
}

impl<B: IdentifyBudget, L: GatewayInfoSource> DiscordIdentifyGate<B, L> {
    /// Builds a gate for one shard of one bot.
    #[must_use]
    pub fn new(
        budget: Arc<B>,
        source: Arc<L>,
        shard_id: u32,
        metrics: IdentifyBudgetMetrics,
    ) -> Self {
        Self {
            budget,
            source,
            shard_id,
            max_wait: DEFAULT_MAX_WAIT,
            metrics,
        }
    }

    /// Overrides the longest wait for a slot.
    #[must_use]
    pub fn with_max_wait(mut self, max_wait: Duration) -> Self {
        self.max_wait = max_wait;
        self
    }

    fn map_err(&self, e: &BudgetError) -> GateError {
        match e {
            BudgetError::Exhausted { retry_after } => {
                self.metrics.inc("exhausted");
                GateError::Exhausted {
                    retry_after: *retry_after,
                }
            }
            BudgetError::Store(_) => {
                self.metrics.inc("store_error");
                GateError::Retryable {
                    reason: "budget store unavailable",
                }
            }
            BudgetError::LimitUnavailable(_) => {
                self.metrics.inc("limit_unavailable");
                GateError::Retryable {
                    reason: "session_start_limit unavailable",
                }
            }
            BudgetError::Credential(_) => {
                self.metrics.inc("credential_rejected");
                GateError::Fatal {
                    reason: "bot credential rejected",
                }
            }
            BudgetError::Config(_) => {
                self.metrics.inc("limit_unavailable");
                GateError::Fatal {
                    reason: "invalid identify budget config",
                }
            }
        }
    }

    /// Acquires one slot or returns the typed reason it could not.
    ///
    /// # Errors
    /// [`BudgetError`] -- the caller maps it to a [`GateError`].
    pub async fn acquire_slot(&self) -> Result<Duration, BudgetError> {
        let identity = self.source.identity().await?;
        let started = Instant::now();
        let mut seeded = false;
        loop {
            match self.budget.acquire(&identity, self.shard_id).await? {
                AcquireOutcome::Granted => {
                    self.metrics.inc("granted");
                    return Ok(started.elapsed());
                }
                AcquireOutcome::Wait(d) => {
                    let sleep = d.min(self.max_wait) + jitter(WAIT_JITTER);
                    if started.elapsed() + sleep > self.max_wait {
                        return Err(BudgetError::Exhausted { retry_after: d });
                    }
                    self.metrics.inc("waited");
                    tokio::time::sleep(sleep).await;
                }
                AcquireOutcome::Exhausted { retry_after } => {
                    return Err(BudgetError::Exhausted { retry_after });
                }
                AcquireOutcome::Unseeded => {
                    if seeded {
                        return Err(BudgetError::LimitUnavailable("seed did not take effect"));
                    }
                    let limit = self.source.session_start_limit().await?;
                    let low = self
                        .budget
                        .record_session_start_limit(&identity, &limit)
                        .await?;
                    if low {
                        self.metrics.inc("low_headroom");
                        tracing::error!(
                            platform = "discord",
                            remaining = limit.remaining,
                            total = limit.total,
                            "discord IDENTIFY daily session-start budget running low"
                        );
                    }
                    seeded = true;
                }
            }
        }
    }
}

impl<B, L> HandshakeGate for DiscordIdentifyGate<B, L>
where
    B: IdentifyBudget,
    L: GatewayInfoSource,
{
    fn acquire<'a>(&'a self, ctx: &'a GateCtx<'a>) -> BoxFut<'a, Result<GatePermit, GateError>> {
        Box::pin(async move {
            // The decision is bound to the bytes that will be sent: only a frame
            // that starts with the host-anchored RESUME prefix is budget-exempt.
            // A guest-rendered IDENTIFY (or anything else) takes a slot even when
            // the host session looks resumable -- fail closed.
            let is_resume = ctx.handshake.starts_with(RESUME_PREFIX);
            if is_resume && session_token_is_resumable(ctx.session_token) {
                self.metrics.inc("resume_skipped");
                return Ok(GatePermit::not_required());
            }
            if session_token_is_resumable(ctx.session_token) {
                self.metrics.inc("resume_mismatch");
                tracing::warn!(platform = %ctx.platform,
                    "resumable session but the rendered handshake is not a RESUME; taking an IDENTIFY slot");
            }
            match self.acquire_slot().await {
                Ok(waited) => Ok(GatePermit {
                    consumed: true,
                    waited,
                }),
                Err(e) => {
                    let ge = self.map_err(&e);
                    tracing::warn!(platform = %ctx.platform, kind = ge.kind(), error = %e,
                        "identify budget refused the handshake");
                    Err(ge)
                }
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_is_hash_only_and_domain_separated() {
        let a = BotIdentity::from_token("tok-secret-123");
        assert_eq!(a.as_hex().len(), 64);
        assert!(!a.as_hex().contains("tok-secret"));
        assert!(!format!("{a:?}").contains(a.as_hex()));
        let plain = format!("{:x}", Sha256::digest(b"tok-secret-123"));
        assert_ne!(a.as_hex(), plain, "domain separation prefix applied");
        assert_eq!(a, BotIdentity::from_token("tok-secret-123"));
        assert_ne!(a, BotIdentity::from_token("other"));
    }

    #[test]
    fn limit_validation_and_gateway_parse() {
        let body = br#"{"url":"wss://x","shards":1,"session_start_limit":
            {"total":1000,"remaining":997,"reset_after":86400000,"max_concurrency":16}}"#;
        let l = SessionStartLimit::from_gateway_bot(body).unwrap();
        assert_eq!((l.total, l.remaining, l.max_concurrency), (1000, 997, 16));
        assert_eq!(l.reset_after, Duration::from_millis(86_400_000));
        for bad in [&b"not json"[..], br#"{"session_start_limit":{"total":1}}"#] {
            assert!(matches!(
                SessionStartLimit::from_gateway_bot(bad),
                Err(BudgetError::LimitUnavailable(_))
            ));
        }
        for (t, r, mc) in [(10, 5, 0), (10, 5, 5000), (10, 11, 1)] {
            assert!(matches!(
                SessionStartLimit::new(t, r, Duration::from_secs(1), mc),
                Err(BudgetError::Config(_))
            ));
        }
        assert_eq!(
            SessionStartLimit::new(10, 5, Duration::ZERO, 1)
                .unwrap()
                .reset_after,
            MIN_RESET
        );
    }

    #[test]
    fn low_headroom_threshold() {
        assert!(SessionStartLimit::is_low_headroom(1000, 200));
        assert!(!SessionStartLimit::is_low_headroom(1000, 201));
        assert!(!SessionStartLimit::is_low_headroom(0, 0));
    }

    #[test]
    fn resumable_token_detection() {
        assert!(session_token_is_resumable(Some(r#"{"session_id":"abc"}"#)));
        for t in [
            None,
            Some(""),
            Some("not json"),
            Some("{}"),
            Some(r#"{"session_id":""}"#),
            Some(r#"{"session_id":5}"#),
            Some(r#"{"seq":3}"#),
        ] {
            assert!(!session_token_is_resumable(t), "{t:?}");
        }
    }

    use std::collections::VecDeque;
    use std::sync::Mutex;

    struct FakeBudget {
        script: Mutex<VecDeque<Result<AcquireOutcome, BudgetError>>>,
        recorded: Mutex<Vec<SessionStartLimit>>,
        record_low: bool,
        acquires: Mutex<u32>,
    }

    impl FakeBudget {
        fn new(script: Vec<Result<AcquireOutcome, BudgetError>>, record_low: bool) -> Arc<Self> {
            Arc::new(Self {
                script: Mutex::new(script.into()),
                recorded: Mutex::default(),
                record_low,
                acquires: Mutex::default(),
            })
        }
    }

    impl IdentifyBudget for FakeBudget {
        async fn acquire(&self, _i: &BotIdentity, _s: u32) -> Result<AcquireOutcome, BudgetError> {
            *self.acquires.lock().unwrap() += 1;
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Err(BudgetError::Store("script exhausted")))
        }
        async fn record_session_start_limit(
            &self,
            _i: &BotIdentity,
            l: &SessionStartLimit,
        ) -> Result<bool, BudgetError> {
            self.recorded.lock().unwrap().push(*l);
            Ok(self.record_low)
        }
    }

    struct FakeSource {
        limit: Result<SessionStartLimit, BudgetError>,
    }

    impl GatewayInfoSource for FakeSource {
        fn identity(&self) -> BoxFut<'_, Result<BotIdentity, BudgetError>> {
            Box::pin(async { Ok(BotIdentity::from_token("t")) })
        }
        fn session_start_limit(&self) -> BoxFut<'_, Result<SessionStartLimit, BudgetError>> {
            let l = self.limit.clone();
            Box::pin(async move { l })
        }
    }

    fn gate(
        script: Vec<Result<AcquireOutcome, BudgetError>>,
        low: bool,
        limit: Result<SessionStartLimit, BudgetError>,
    ) -> (
        DiscordIdentifyGate<FakeBudget, FakeSource>,
        Arc<FakeBudget>,
        Registry,
    ) {
        let reg = Registry::new();
        let m = IdentifyBudgetMetrics::register(&reg).unwrap();
        let b = FakeBudget::new(script, low);
        let g = DiscordIdentifyGate::new(Arc::clone(&b), Arc::new(FakeSource { limit }), 3, m)
            .with_max_wait(Duration::from_millis(400));
        (g, b, reg)
    }

    fn lim() -> Result<SessionStartLimit, BudgetError> {
        Ok(SessionStartLimit::new(1000, 1000, Duration::from_secs(60), 2).unwrap())
    }

    fn count(reg: &Registry, outcome: &str) -> u64 {
        reg.gather()
            .iter()
            .flat_map(|f| f.get_metric().iter())
            .find(|m| m.get_label().iter().any(|l| l.value() == outcome))
            .map_or(0, |m| m.get_counter().value() as u64)
    }

    const RESUME_FRAME: &[u8] =
        br#"{"op":6,"d":{"token":"{{discord-bot}}","session_id":"s","seq":1}}"#;
    const IDENTIFY_FRAME: &[u8] = br#"{"op":2,"d":{"token":"{{discord-bot}}","intents":1}}"#;

    /// A session token present means the host expects a RESUME; the frame the
    /// helper pairs with it is the matching RESUME, otherwise an IDENTIFY.
    fn cx(token: Option<&str>) -> GateCtx<'_> {
        cx_with(
            token,
            if token.is_some() {
                RESUME_FRAME
            } else {
                IDENTIFY_FRAME
            },
        )
    }

    fn cx_with<'a>(token: Option<&'a str>, handshake: &'a [u8]) -> GateCtx<'a> {
        GateCtx {
            platform: "discord",
            source_id: "s",
            connection_id: "c",
            session_token: token,
            handshake,
        }
    }

    #[tokio::test]
    async fn resume_never_touches_the_budget() {
        let (g, b, reg) = gate(vec![], false, lim());
        let p = g.acquire(&cx(Some(r#"{"session_id":"x"}"#))).await.unwrap();
        assert!(!p.consumed);
        assert_eq!(*b.acquires.lock().unwrap(), 0);
        assert_eq!(count(&reg, "resume_skipped"), 1);
    }

    /// Security regression: the exemption is bound to the bytes SENT. A guest
    /// that renders an IDENTIFY (op 2) while the host session looks resumable
    /// must still take a slot; a RESUME (op 6) must not; an anything-else frame
    /// or a RESUME with no resumable session is not exempt either.
    #[tokio::test]
    async fn the_budget_decision_follows_the_rendered_frame_not_the_session_token() {
        let tok = Some(r#"{"session_id":"x"}"#);
        // guest renders op 2 although a session exists -> slot acquired
        let (g, b, reg) = gate(vec![Ok(AcquireOutcome::Granted)], false, lim());
        let p = g.acquire(&cx_with(tok, IDENTIFY_FRAME)).await.unwrap();
        assert!(
            p.consumed,
            "an IDENTIFY consumes a slot even with a session"
        );
        assert_eq!(*b.acquires.lock().unwrap(), 1);
        assert_eq!(count(&reg, "resume_mismatch"), 1);
        assert_eq!(count(&reg, "resume_skipped"), 0);
        // op 6 + resumable session -> no slot
        let (g, b, reg) = gate(vec![], false, lim());
        let p = g.acquire(&cx_with(tok, RESUME_FRAME)).await.unwrap();
        assert!(!p.consumed);
        assert_eq!(*b.acquires.lock().unwrap(), 0);
        assert_eq!(count(&reg, "resume_skipped"), 1);
        // an unrecognised frame (not the anchored prefix) is never exempt
        for odd in [
            &b""[..],
            br#" {"op":6,"d":{"token":"x"}}"#,
            br#"{"d":{"token":"x"},"op":6}"#,
        ] {
            let (g, b, _) = gate(vec![Ok(AcquireOutcome::Granted)], false, lim());
            assert!(g.acquire(&cx_with(tok, odd)).await.unwrap().consumed);
            assert_eq!(*b.acquires.lock().unwrap(), 1);
        }
        // exhausted budget refuses the guest IDENTIFY: nothing may be sent
        let (g, _, _) = gate(
            vec![Ok(AcquireOutcome::Exhausted {
                retry_after: Duration::from_secs(9),
            })],
            false,
            lim(),
        );
        assert!(matches!(
            g.acquire(&cx_with(tok, IDENTIFY_FRAME)).await,
            Err(GateError::Exhausted { .. })
        ));
    }

    #[tokio::test]
    async fn identify_seeds_once_then_grants() {
        let (g, b, reg) = gate(
            vec![Ok(AcquireOutcome::Unseeded), Ok(AcquireOutcome::Granted)],
            true,
            lim(),
        );
        assert!(g.acquire(&cx(None)).await.unwrap().consumed);
        assert_eq!(b.recorded.lock().unwrap().len(), 1);
        assert_eq!(count(&reg, "low_headroom"), 1);
        assert_eq!(count(&reg, "granted"), 1);
    }

    #[tokio::test]
    async fn a_seed_that_does_not_take_is_refused_not_looped() {
        let (g, b, _) = gate(
            vec![Ok(AcquireOutcome::Unseeded), Ok(AcquireOutcome::Unseeded)],
            false,
            lim(),
        );
        assert_eq!(
            g.acquire(&cx(None)).await.unwrap_err(),
            GateError::Retryable {
                reason: "session_start_limit unavailable"
            }
        );
        assert_eq!(*b.acquires.lock().unwrap(), 2);
    }

    #[tokio::test]
    async fn a_short_wait_is_slept_with_jitter_then_granted() {
        let (g, _, reg) = gate(
            vec![
                Ok(AcquireOutcome::Wait(Duration::from_millis(20))),
                Ok(AcquireOutcome::Granted),
            ],
            false,
            lim(),
        );
        let p = g.acquire(&cx(None)).await.unwrap();
        assert!(p.waited >= Duration::from_millis(20));
        assert_eq!(count(&reg, "waited"), 1);
    }

    #[tokio::test]
    async fn a_wait_beyond_max_wait_is_exhaustion_not_a_sleep() {
        let (g, _, reg) = gate(
            vec![Ok(AcquireOutcome::Wait(Duration::from_secs(5)))],
            false,
            lim(),
        );
        match g.acquire(&cx(None)).await.unwrap_err() {
            GateError::Exhausted { retry_after } => assert_eq!(retry_after, Duration::from_secs(5)),
            other => panic!("{other:?}"),
        }
        assert_eq!(count(&reg, "exhausted"), 1);
    }

    #[tokio::test]
    async fn every_budget_error_maps_to_a_typed_gate_refusal() {
        let cases: Vec<(BudgetError, GateError, &str)> = vec![
            (
                BudgetError::Exhausted {
                    retry_after: Duration::from_secs(9),
                },
                GateError::Exhausted {
                    retry_after: Duration::from_secs(9),
                },
                "exhausted",
            ),
            (
                BudgetError::Store("x"),
                GateError::Retryable {
                    reason: "budget store unavailable",
                },
                "store_error",
            ),
            (
                BudgetError::LimitUnavailable("x"),
                GateError::Retryable {
                    reason: "session_start_limit unavailable",
                },
                "limit_unavailable",
            ),
            (
                BudgetError::Credential("x"),
                GateError::Fatal {
                    reason: "bot credential rejected",
                },
                "credential_rejected",
            ),
            (
                BudgetError::Config("x"),
                GateError::Fatal {
                    reason: "invalid identify budget config",
                },
                "limit_unavailable",
            ),
        ];
        for (be, expect, outcome) in cases {
            let (g, _, reg) = gate(vec![Err(be)], false, lim());
            assert_eq!(g.acquire(&cx(None)).await.unwrap_err(), expect);
            assert_eq!(count(&reg, outcome), 1, "{outcome}");
        }
    }

    #[tokio::test]
    async fn a_failed_limit_fetch_propagates_and_exhausted_outcome_is_typed() {
        let (g, _, _) = gate(
            vec![Ok(AcquireOutcome::Unseeded)],
            false,
            Err(BudgetError::LimitUnavailable("fetch")),
        );
        assert!(matches!(
            g.acquire(&cx(None)).await,
            Err(GateError::Retryable { .. })
        ));
        let (g, _, _) = gate(
            vec![Ok(AcquireOutcome::Exhausted {
                retry_after: Duration::from_secs(3),
            })],
            false,
            lim(),
        );
        assert_eq!(
            g.acquire(&cx(None)).await.unwrap_err(),
            GateError::Exhausted {
                retry_after: Duration::from_secs(3)
            }
        );
    }

    #[test]
    fn jitter_is_bounded() {
        assert_eq!(jitter(Duration::ZERO), Duration::ZERO);
        for _ in 0..200 {
            assert!(jitter(Duration::from_millis(250)) < Duration::from_millis(250));
        }
    }
}
