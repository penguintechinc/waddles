//! Analytics producer: one metadata-only entry per ingested platform event on the Valkey stream
//! `waddles:analytics` (wire contract: `docs/analytics-foundation.md`, "Ingest -> analytics stream").
//!
//! Ingest only PRODUCES here; `svc_analytics` consumes. The entry carries numeric internal ids,
//! the member's tokenized UUID and small int codes -- never a handle, username, message text or
//! any free text. The tenant/community ids are the ones the publish funnel already resolved. The
//! member token is taken from `payload["user_uuid"]` when an upstream stage stamped it.
//!
//! INTERIM: until the planned PII-tokenizer service bundle stamps `user_uuid` once per message,
//! the WORKER (never the ingest hot path) tokenizes the platform's raw user id (`user_id` /
//! `author_id`) through the EXISTING hub-backed `IdentityTokenizer` (`MintEphemeralPseudonyms`,
//! tenant-scoped by the request's `tenant_id`), behind a bounded TTL cache keyed by a SHA-256 of
//! (tenant, platform, id) so no raw id is ever a stored key. Replace by dropping this step once
//! the bundle's stamp exists (the stamp already takes precedence).
//!
//! When any field is unavailable the event is SKIPPED and counted with a reason -- never guessed
//! or defaulted. No raw id or handle is ever logged, put on the stream or used as a label.
//!
//! Safety properties (the producer must never harm ingest):
//! * Flag `waddles.analytics-stream` (env baseline `FLAG_WADDLES_ANALYTICS_STREAM`), default OFF.
//!   OFF costs one `OnceLock` read and one relaxed atomic load per event; nothing is queued.
//! * The hot path only `try_send`s onto a bounded queue; a worker (bounded concurrency) tokenizes
//!   and does the `XADD`, each under a hard timeout. A full queue, a timeout or a Valkey error
//!   drops the event, bumps a counter and emits a rate-limited WARN. A publish failure is never
//!   an ingest failure.
//! * `XADD ... MAXLEN ~ <cap>` bounds the stream (`ANALYTICS_STREAM_MAXLEN`).

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use penguin_spine::PlatformEvent;
use prometheus::{Histogram, HistogramOpts, IntCounter, IntCounterVec, Opts, Registry};
use sha2::{Digest, Sha256};
use tokio::sync::{mpsc, Semaphore};

use crate::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};

const ENV_TOKEN_TIMEOUT_MS: &str = "ANALYTICS_TOKENIZE_TIMEOUT_MS";
const ENV_TOKEN_CONCURRENCY: &str = "ANALYTICS_TOKENIZE_CONCURRENCY";
const ENV_TOKEN_CACHE: &str = "ANALYTICS_TOKEN_CACHE_CAPACITY";
const DEFAULT_TOKEN_TIMEOUT_MS: u64 = 500;
const DEFAULT_TOKEN_CONCURRENCY: u64 = 8;
const DEFAULT_TOKEN_CACHE: u64 = 10_000;
const TTL_FOUND: Duration = Duration::from_secs(600);
const TTL_MISSING: Duration = Duration::from_secs(30);
const TTL_DOWN: Duration = Duration::from_secs(5);

/// The analytics stream key.
pub const STREAM: &str = "waddles:analytics";
/// PostHog flag key (default OFF).
pub const FLAG_KEY: &str = "waddles.analytics-stream";
/// Docker ENV baseline for [`FLAG_KEY`] (`FLAG_` + key uppercased, `.`/`-` -> `_`).
pub const ENV_BASELINE: &str = "FLAG_WADDLES_ANALYTICS_STREAM";
/// Wire schema version stamped in field `v`.
pub const WIRE_VERSION: u8 = 1;

const ENV_MAXLEN: &str = "ANALYTICS_STREAM_MAXLEN";
const ENV_TIMEOUT_MS: &str = "ANALYTICS_PUBLISH_TIMEOUT_MS";
const ENV_QUEUE: &str = "ANALYTICS_QUEUE_CAPACITY";
const DEFAULT_MAXLEN: usize = 500_000;
const MIN_MAXLEN: usize = 1_000;
const DEFAULT_TIMEOUT_MS: u64 = 250;
const DEFAULT_QUEUE: usize = 4_096;
const FLAG_REFRESH: Duration = Duration::from_secs(15);
const WARN_INTERVAL_MS: u64 = 30_000;

/// Event kind codes (`kind_code` on the wire).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    /// A chat message.
    Message = 1,
    /// A command invocation.
    Command = 2,
    /// A member joined.
    Join = 3,
    /// A member left.
    Leave = 4,
    /// Presence started.
    PresenceStart = 5,
    /// Presence ended.
    PresenceEnd = 6,
}

/// Maps an ingest event type onto an analytics kind; `None` for event types analytics ignores.
#[must_use]
pub fn kind_for_event_type(event_type: &str) -> Option<Kind> {
    match event_type {
        "chat.message" | "message" => Some(Kind::Message),
        "interaction.command" | "chat.command" => Some(Kind::Command),
        "member.join" | "user.join" => Some(Kind::Join),
        "member.leave" | "user.leave" => Some(Kind::Leave),
        "presence.start" => Some(Kind::PresenceStart),
        "presence.end" => Some(Kind::PresenceEnd),
        _ => None,
    }
}

/// Platform code (1 waddles_native, 2 discord, 3 twitch, 4 youtube, 5 other). Provenance uses the
/// same enum: an event from Discord/Twitch/YouTube is platform-derived data, so later gates can
/// exclude codes 2..=5 and keep first-party (1) only.
#[must_use]
pub fn platform_code(platform: &str) -> u8 {
    match platform {
        "waddles" | "native" | "waddles_native" => 1,
        "discord" => 2,
        "twitch" => 3,
        "youtube" => 4,
        _ => 5,
    }
}

/// Why an event was not published (bounded metric label).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    /// Event type analytics does not track.
    UnmappedKind,
    /// No resolved numeric tenant/community (v2/static path or unresolved).
    NoScope,
    /// Tenant-wide (`community_id == 0`): analytics is community-grained.
    TenantWide,
    /// No tokenized member UUID on the event.
    NoUserToken,
    /// `occurred_at` unparsable.
    BadTimestamp,
    /// The hub-backed tokenizer failed, timed out or was unreachable.
    TokenizerUnavailable,
}

impl SkipReason {
    /// The metric label.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::UnmappedKind => "unmapped_kind",
            Self::NoScope => "no_scope",
            Self::TenantWide => "tenant_wide",
            Self::NoUserToken => "no_user_token",
            Self::BadTimestamp => "bad_timestamp",
            Self::TokenizerUnavailable => "tokenizer_unavailable",
        }
    }
}

/// One wire entry. Exactly these eight fields, nothing else.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnalyticsEvent {
    /// Numeric tenant id.
    pub tenant_id: i64,
    /// Numeric community id (never a slug/UCID).
    pub community_id: i64,
    /// Member UUID token (lowercase hyphenated).
    pub user_uuid: String,
    /// Platform code 1..=5.
    pub platform_code: u8,
    /// Kind code 1..=6.
    pub kind_code: u8,
    /// Epoch milliseconds.
    pub ts_ms: u64,
    /// Provenance code 1..=5.
    pub provenance_code: u8,
}

impl AnalyticsEvent {
    /// The ordered XADD field/value pairs (the complete wire shape).
    #[must_use]
    pub fn fields(&self) -> [(&'static str, String); 8] {
        [
            ("v", WIRE_VERSION.to_string()),
            ("tenant_id", self.tenant_id.to_string()),
            ("community_id", self.community_id.to_string()),
            ("user_uuid", self.user_uuid.clone()),
            ("platform_code", self.platform_code.to_string()),
            ("kind_code", self.kind_code.to_string()),
            ("ts_ms", self.ts_ms.to_string()),
            ("provenance_code", self.provenance_code.to_string()),
        ]
    }
}

/// Where the member token comes from for one pending event.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UserRef {
    /// A token an upstream stage already stamped on the payload (`user_uuid`): used as-is.
    Token(String),
    /// INTERIM: the platform's raw user id, tokenized by the worker through the hub-backed
    /// tokenizer. In-process only; it is never put on the stream, in a log or in a cache key.
    Raw {
        /// Platform slug the tokenizer scopes the id by.
        platform: &'static str,
        /// The raw platform user id.
        id: String,
    },
}

/// An event with everything but (possibly) the member token resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pending {
    /// Numeric tenant id.
    pub tenant_id: i64,
    /// Numeric community id.
    pub community_id: i64,
    /// Member token source.
    pub user: UserRef,
    /// Platform code.
    pub platform_code: u8,
    /// Kind code.
    pub kind_code: u8,
    /// Epoch ms.
    pub ts_ms: u64,
    /// Provenance code.
    pub provenance_code: u8,
}

impl Pending {
    /// Completes the wire event with a resolved member token.
    #[must_use]
    pub fn finish(&self, user_uuid: String) -> AnalyticsEvent {
        AnalyticsEvent {
            tenant_id: self.tenant_id,
            community_id: self.community_id,
            user_uuid,
            platform_code: self.platform_code,
            kind_code: self.kind_code,
            ts_ms: self.ts_ms,
            provenance_code: self.provenance_code,
        }
    }
}

/// The static platform slug the tokenizer understands, or `None`.
fn tokenizer_platform(platform: &str) -> Option<&'static str> {
    match platform {
        "discord" => Some("discord"),
        "twitch" => Some("twitch"),
        "youtube" => Some("youtube"),
        _ => None,
    }
}

/// The raw platform user id from the payload (`user_id`, then `author_id`), if well-formed.
fn raw_user_id(event: &PlatformEvent) -> Option<String> {
    ["user_id", "author_id"].iter().find_map(|k| {
        let v = event.payload.get(*k)?.as_str()?;
        let ok = !v.is_empty()
            && v.len() <= 64
            && v.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_');
        ok.then(|| v.to_string())
    })
}

/// Builds the pending wire event from an already-published ingest event plus the scope the
/// publish funnel resolved (`(tenant_id, community_id)`). Skips -- never defaults -- when
/// anything is missing. A payload `user_uuid` wins over tokenizing the raw id.
///
/// # Errors
/// The [`SkipReason`] for the first missing/invalid input.
pub fn build_event(
    event: &PlatformEvent,
    scope: Option<(i64, i32)>,
) -> Result<Pending, SkipReason> {
    let kind = kind_for_event_type(&event.event_type).ok_or(SkipReason::UnmappedKind)?;
    let (tenant_id, community_id) = scope.ok_or(SkipReason::NoScope)?;
    if tenant_id < 1 {
        return Err(SkipReason::NoScope);
    }
    if community_id < 1 {
        return Err(SkipReason::TenantWide);
    }
    let stamped = event
        .payload
        .get("user_uuid")
        .and_then(serde_json::Value::as_str)
        .and_then(|s| uuid::Uuid::parse_str(s).ok())
        .filter(|u| !u.is_nil());
    let user = match stamped {
        Some(u) => UserRef::Token(u.hyphenated().to_string()),
        None => match (tokenizer_platform(&event.platform), raw_user_id(event)) {
            (Some(platform), Some(id)) => UserRef::Raw { platform, id },
            _ => return Err(SkipReason::NoUserToken),
        },
    };
    let ts_ms = parse_rfc3339_millis_z(&event.occurred_at).ok_or(SkipReason::BadTimestamp)?;
    let code = platform_code(&event.platform);
    Ok(Pending {
        tenant_id,
        community_id: i64::from(community_id),
        user,
        platform_code: code,
        kind_code: kind as u8,
        ts_ms,
        provenance_code: code,
    })
}

/// Parses `YYYY-MM-DDTHH:MM:SS[.fff]Z` to epoch ms (std only; the spine validates this shape).
fn parse_rfc3339_millis_z(s: &str) -> Option<u64> {
    let b = s.as_bytes();
    if b.len() < 20 || !s.ends_with('Z') || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' {
        return None;
    }
    let num = |r: std::ops::Range<usize>| s.get(r)?.parse::<i64>().ok();
    let (y, mo, d) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (h, mi, sec) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let frac = &s[19..s.len() - 1];
    let ms = if frac.is_empty() {
        0
    } else {
        let digits = frac.strip_prefix('.')?;
        if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        format!("{:0<3}", &digits[..digits.len().min(3)])
            .parse::<i64>()
            .ok()?
    };
    // Days-from-civil (Howard Hinnant).
    let y = if mo <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (mo + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    let total = ((days * 24 + h) * 60 + mi) * 60 + sec;
    u64::try_from(total * 1000 + ms).ok()
}

/// Runtime settings (all env, bounded, defaulted).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Settings {
    /// Approximate stream cap (`MAXLEN ~`).
    pub maxlen: usize,
    /// Hard per-XADD timeout.
    pub timeout: Duration,
    /// Hot-path queue capacity.
    pub queue: usize,
    /// Hard per-tokenize timeout.
    pub tokenize_timeout: Duration,
    /// Max concurrent in-flight tokenize+XADD tasks.
    pub concurrency: usize,
    /// Token cache capacity (entries).
    pub cache_capacity: usize,
}

impl Settings {
    /// Reads settings via `lookup`; an unparsable or out-of-range value falls back to the default
    /// with a WARN (never a startup failure for an optional producer).
    pub fn from_lookup(lookup: &dyn Fn(&str) -> Option<String>) -> Self {
        let get = |key: &str, default: u64, min: u64| -> u64 {
            match lookup(key) {
                None => default,
                Some(raw) => match raw.trim().parse::<u64>() {
                    Ok(v) if v >= min => v,
                    _ => {
                        tracing::warn!(key, "invalid analytics setting; using default");
                        default
                    }
                },
            }
        };
        let size = |key: &str, default: u64, min: u64| {
            usize::try_from(get(key, default, min)).unwrap_or(default as usize)
        };
        Self {
            maxlen: size(ENV_MAXLEN, DEFAULT_MAXLEN as u64, MIN_MAXLEN as u64),
            timeout: Duration::from_millis(get(ENV_TIMEOUT_MS, DEFAULT_TIMEOUT_MS, 1)),
            queue: size(ENV_QUEUE, DEFAULT_QUEUE as u64, 1),
            tokenize_timeout: Duration::from_millis(get(
                ENV_TOKEN_TIMEOUT_MS,
                DEFAULT_TOKEN_TIMEOUT_MS,
                1,
            )),
            concurrency: size(ENV_TOKEN_CONCURRENCY, DEFAULT_TOKEN_CONCURRENCY, 1).min(256),
            cache_capacity: size(ENV_TOKEN_CACHE, DEFAULT_TOKEN_CACHE, 1),
        }
    }
}

/// Prometheus instruments (bounded labels only).
#[derive(Clone)]
pub struct AnalyticsMetrics {
    /// `svc_ingest_analytics_published_total`.
    pub published: IntCounter,
    /// `svc_ingest_analytics_skipped_total{reason}`.
    pub skipped: IntCounterVec,
    /// `svc_ingest_analytics_failed_total{reason}` (`queue_full`/`timeout`/`error`/`connect`).
    pub failed: IntCounterVec,
    /// `svc_ingest_analytics_publish_duration_seconds`.
    pub latency: Histogram,
}

/// Registers the analytics metrics on `registry` (once per registry).
///
/// # Panics
/// On duplicate registration (a startup programming error, like the sibling registrations).
pub fn register_metrics(registry: &Registry) -> AnalyticsMetrics {
    let published = IntCounter::new(
        "svc_ingest_analytics_published_total",
        "Analytics stream entries XADDed",
    )
    .expect("valid metric");
    let skipped = IntCounterVec::new(
        Opts::new(
            "svc_ingest_analytics_skipped_total",
            "Events not published to the analytics stream, by reason",
        ),
        &["reason"],
    )
    .expect("valid metric");
    let failed = IntCounterVec::new(
        Opts::new(
            "svc_ingest_analytics_failed_total",
            "Analytics publishes dropped after a failure, by reason",
        ),
        &["reason"],
    )
    .expect("valid metric");
    let latency = Histogram::with_opts(
        HistogramOpts::new(
            "svc_ingest_analytics_publish_duration_seconds",
            "Analytics XADD latency in seconds",
        )
        .buckets(vec![
            0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
        ]),
    )
    .expect("valid metric");
    registry
        .register(Box::new(published.clone()))
        .expect("register analytics published");
    registry
        .register(Box::new(skipped.clone()))
        .expect("register analytics skipped");
    registry
        .register(Box::new(failed.clone()))
        .expect("register analytics failed");
    registry
        .register(Box::new(latency.clone()))
        .expect("register analytics latency");
    AnalyticsMetrics {
        published,
        skipped,
        failed,
        latency,
    }
}

/// Allows at most one WARN per interval.
#[derive(Debug)]
pub struct WarnLimiter {
    last_ms: AtomicU64,
    interval_ms: u64,
}

impl WarnLimiter {
    /// A limiter that lets the first call through, then one per `interval_ms`.
    #[must_use]
    pub fn new(interval_ms: u64) -> Self {
        Self {
            last_ms: AtomicU64::new(0),
            interval_ms,
        }
    }

    /// Whether a WARN may be emitted at `now_ms` (monotonic or epoch; must be >= 1).
    pub fn allow(&self, now_ms: u64) -> bool {
        let last = self.last_ms.load(Ordering::Relaxed);
        if last != 0 && now_ms.saturating_sub(last) < self.interval_ms {
            return false;
        }
        self.last_ms
            .compare_exchange(last, now_ms, Ordering::Relaxed, Ordering::Relaxed)
            .is_ok()
    }
}

/// Why a tokenize attempt produced no token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TokenizeFail {
    /// The identity does not exist.
    NotFound,
    /// Unreachable, denied, rate limited, malformed or timed out.
    Unavailable,
}

/// Turns a platform user id into the member UUID token (INTERIM resolution; see module doc).
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait UserTokenizer: Send + Sync + 'static {
    /// The token for `raw_id` on `platform`, minted under `tenant_id`.
    fn tokenize(
        &self,
        tenant_id: i64,
        community_id: i64,
        platform: &'static str,
        raw_id: &str,
    ) -> impl std::future::Future<Output = Result<String, TokenizeFail>> + Send;
}

/// The production tokenizer: the existing hub-backed [`IdentityTokenizer`] (tenant-scoped by the
/// mint request's `tenant_id`), connected lazily on first use so a down hub never blocks boot.
pub struct HubUserTokenizer {
    cli: crate::config::CliConfig,
    cell: tokio::sync::OnceCell<Arc<dyn IdentityTokenizer>>,
}

impl HubUserTokenizer {
    /// Wraps the process config (no I/O).
    #[must_use]
    pub fn new(cli: crate::config::CliConfig) -> Self {
        Self {
            cli,
            cell: tokio::sync::OnceCell::new(),
        }
    }
}

impl UserTokenizer for HubUserTokenizer {
    async fn tokenize(
        &self,
        tenant_id: i64,
        community_id: i64,
        platform: &'static str,
        raw_id: &str,
    ) -> Result<String, TokenizeFail> {
        let inner = self
            .cell
            .get_or_try_init(|| async {
                crate::connector_runtime::boot::connect_tokenizer(&self.cli, &|k| {
                    std::env::var(k).ok()
                })
                .await
            })
            .await
            .map_err(|_| TokenizeFail::Unavailable)?;
        // The hub tokenizer keys on tenant + platform only; `connection_id` is unused by it.
        let scope = CallScope {
            platform,
            community_id,
            tenant_id,
            connection_id: 0,
        };
        inner.tokenize(&scope, raw_id).await.map_err(|e| match e {
            TokenizeError::NotFound => TokenizeFail::NotFound,
            _ => TokenizeFail::Unavailable,
        })
    }
}

/// A cached tokenize outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
enum CacheVal {
    Token(String),
    Missing,
    Down,
}

type CacheKey = [u8; 32];

/// SHA-256 over (tenant, platform, id): the only form in which a raw id is ever a cache key.
fn cache_key(tenant_id: i64, platform: &str, raw_id: &str) -> CacheKey {
    let mut h = Sha256::new();
    h.update(tenant_id.to_le_bytes());
    h.update(platform.as_bytes());
    h.update([0]);
    h.update(raw_id.as_bytes());
    h.finalize().into()
}

type CacheInner = (HashMap<CacheKey, (CacheVal, Instant)>, VecDeque<CacheKey>);

/// Bounded TTL cache (insertion-order eviction at capacity).
pub struct TokenCache {
    cap: usize,
    inner: Mutex<CacheInner>,
}

impl TokenCache {
    /// A cache holding at most `cap` entries.
    #[must_use]
    pub fn new(cap: usize) -> Self {
        Self {
            cap: cap.max(1),
            inner: Mutex::new((HashMap::new(), VecDeque::new())),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, CacheInner> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn get(&self, key: &CacheKey, now: Instant) -> Option<CacheVal> {
        let mut g = self.lock();
        match g.0.get(key) {
            Some((v, exp)) if *exp > now => Some(v.clone()),
            Some(_) => {
                g.0.remove(key);
                g.1.retain(|k| k != key);
                None
            }
            None => None,
        }
    }

    fn put(&self, key: CacheKey, val: CacheVal, ttl: Duration, now: Instant) {
        let mut g = self.lock();
        while g.0.len() >= self.cap && !g.0.contains_key(&key) {
            match g.1.pop_front() {
                Some(old) => {
                    g.0.remove(&old);
                }
                None => break,
            }
        }
        if g.0.insert(key, (val, now + ttl)).is_none() {
            g.1.push_back(key);
        }
    }

    /// Current entry count.
    #[must_use]
    pub fn len(&self) -> usize {
        self.lock().0.len()
    }

    /// Whether the cache is empty.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Where entries go. A generic bound (not `dyn`), like [`crate::publish::EventAppender`].
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see publish.rs::EventAppender's doc
pub trait AnalyticsSink: Send + Sync + 'static {
    /// `XADD <stream> MAXLEN ~ <maxlen> * <fields...>`.
    fn xadd(
        &self,
        stream: &str,
        maxlen: usize,
        fields: &[(&'static str, String)],
    ) -> impl std::future::Future<Output = Result<(), String>> + Send;
}

/// Valkey sink; the connection is opened lazily on first use and re-attempted after a failure.
pub struct ValkeySink {
    client: redis::Client,
    conn: tokio::sync::OnceCell<redis::aio::ConnectionManager>,
    response_timeout: Duration,
    /// Stream-name override; `None` (production) uses the name the caller passes ([`STREAM`]).
    stream_override: Option<String>,
}

impl ValkeySink {
    /// Wraps `client` (no I/O).
    #[must_use]
    pub fn new(client: redis::Client, response_timeout: Duration) -> Self {
        Self {
            client,
            conn: tokio::sync::OnceCell::new(),
            response_timeout,
            stream_override: None,
        }
    }

    /// Redirects every `XADD` to `stream` instead of [`STREAM`], so integration tests sharing one
    /// Valkey can each use a private key. Production never calls this.
    #[must_use]
    pub fn with_stream(mut self, stream: impl Into<String>) -> Self {
        self.stream_override = Some(stream.into());
        self
    }
}

impl AnalyticsSink for ValkeySink {
    async fn xadd(
        &self,
        stream: &str,
        maxlen: usize,
        fields: &[(&'static str, String)],
    ) -> Result<(), String> {
        let mut conn = self
            .conn
            .get_or_try_init(|| async {
                let cfg = redis::aio::ConnectionManagerConfig::new()
                    .set_response_timeout(Some(self.response_timeout));
                self.client.get_connection_manager_with_config(cfg).await
            })
            .await
            .map_err(|e| format!("connect: {e}"))?
            .clone();
        let stream = self.stream_override.as_deref().unwrap_or(stream);
        let mut cmd = redis::cmd("XADD");
        cmd.arg(stream).arg("MAXLEN").arg("~").arg(maxlen).arg("*");
        for (k, v) in fields {
            cmd.arg(*k).arg(v.as_str());
        }
        cmd.query_async::<String>(&mut conn)
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }
}

/// The hot-path handle: flag gate + bounded queue. Cheap to call; never blocks, never errors.
pub struct AnalyticsPublisher {
    gate: Arc<AtomicBool>,
    tx: mpsc::Sender<Pending>,
    metrics: AnalyticsMetrics,
    warn: Arc<WarnLimiter>,
    epoch: Instant,
}

impl AnalyticsPublisher {
    /// Builds the publisher and the queue's receiving half for [`spawn_worker`].
    #[must_use]
    pub fn new(
        gate: Arc<AtomicBool>,
        queue: usize,
        metrics: AnalyticsMetrics,
    ) -> (Self, mpsc::Receiver<Pending>, Arc<WarnLimiter>) {
        let (tx, rx) = mpsc::channel(queue.max(1));
        let warn = Arc::new(WarnLimiter::new(WARN_INTERVAL_MS));
        (
            Self {
                gate,
                tx,
                metrics,
                warn: Arc::clone(&warn),
                epoch: Instant::now(),
            },
            rx,
            warn,
        )
    }

    /// Observes one successfully published ingest event. Flag OFF: returns immediately (no
    /// allocation, no queue). Otherwise builds the pending event (skip + count on any gap) and
    /// `try_send`s it; a full queue drops it.
    pub fn observe(&self, event: &PlatformEvent, scope: Option<(i64, i32)>) {
        if !self.gate.load(Ordering::Relaxed) {
            return;
        }
        match build_event(event, scope) {
            Err(reason) => {
                self.metrics
                    .skipped
                    .with_label_values(&[reason.label()])
                    .inc();
                tracing::debug!(reason = reason.label(), "analytics event skipped");
            }
            Ok(ev) => {
                if self.tx.try_send(ev).is_err() {
                    self.metrics.failed.with_label_values(&["queue_full"]).inc();
                    let now = u64::try_from(self.epoch.elapsed().as_millis()).unwrap_or(0) + 1;
                    if self.warn.allow(now) {
                        tracing::warn!("analytics queue full or closed; dropping events");
                    }
                }
            }
        }
    }
}

/// Resolves a pending event's member token: stamped token as-is, else cache, else the tokenizer
/// under a hard timeout. Never logs the raw id.
async fn resolve_user<T: UserTokenizer>(
    p: &Pending,
    tokenizer: &T,
    cache: &TokenCache,
    timeout: Duration,
) -> Result<String, SkipReason> {
    let (platform, id) = match &p.user {
        UserRef::Token(t) => return Ok(t.clone()),
        UserRef::Raw { platform, id } => (*platform, id.as_str()),
    };
    let key = cache_key(p.tenant_id, platform, id);
    match cache.get(&key, Instant::now()) {
        Some(CacheVal::Token(t)) => return Ok(t),
        Some(CacheVal::Missing) => return Err(SkipReason::NoUserToken),
        Some(CacheVal::Down) => return Err(SkipReason::TokenizerUnavailable),
        None => {}
    }
    let outcome = tokio::time::timeout(
        timeout,
        tokenizer.tokenize(p.tenant_id, p.community_id, platform, id),
    )
    .await;
    let now = Instant::now();
    match outcome {
        Ok(Ok(t)) if uuid::Uuid::parse_str(&t).is_ok_and(|u| !u.is_nil()) => {
            cache.put(key, CacheVal::Token(t.clone()), TTL_FOUND, now);
            Ok(t)
        }
        Ok(Err(TokenizeFail::NotFound)) => {
            cache.put(key, CacheVal::Missing, TTL_MISSING, now);
            Err(SkipReason::NoUserToken)
        }
        _ => {
            cache.put(key, CacheVal::Down, TTL_DOWN, now);
            Err(SkipReason::TokenizerUnavailable)
        }
    }
}

/// The shared collaborators of the worker tasks.
struct Shared<S, T> {
    sink: S,
    tokenizer: T,
    cache: TokenCache,
    settings: Settings,
    metrics: AnalyticsMetrics,
    warn: Arc<WarnLimiter>,
    epoch: Instant,
}

/// Tokenizes (if needed) then XADDs one event; every failure is counted, never propagated.
async fn process<S: AnalyticsSink, T: UserTokenizer>(p: Pending, sh: &Shared<S, T>) {
    let user = match resolve_user(&p, &sh.tokenizer, &sh.cache, sh.settings.tokenize_timeout).await
    {
        Ok(u) => u,
        Err(reason) => {
            sh.metrics
                .skipped
                .with_label_values(&[reason.label()])
                .inc();
            tracing::debug!(reason = reason.label(), "analytics event skipped");
            return;
        }
    };
    let fields = p.finish(user).fields();
    let start = Instant::now();
    let outcome = tokio::time::timeout(
        sh.settings.timeout,
        sh.sink.xadd(STREAM, sh.settings.maxlen, &fields),
    )
    .await;
    let reason = match outcome {
        Ok(Ok(())) => {
            sh.metrics.published.inc();
            sh.metrics.latency.observe(start.elapsed().as_secs_f64());
            return;
        }
        Ok(Err(e)) if e.starts_with("connect") => "connect",
        Ok(Err(_)) => "error",
        Err(_) => "timeout",
    };
    sh.metrics.failed.with_label_values(&[reason]).inc();
    let now = u64::try_from(sh.epoch.elapsed().as_millis()).unwrap_or(0) + 1;
    if sh.warn.allow(now) {
        tracing::warn!(reason, "analytics publish failed; event dropped");
    }
}

/// Spawns the worker draining the queue with bounded concurrency; it ends (after in-flight
/// tasks finish) when every sender is dropped.
pub fn spawn_worker<S: AnalyticsSink, T: UserTokenizer>(
    sink: S,
    tokenizer: T,
    mut rx: mpsc::Receiver<Pending>,
    settings: Settings,
    metrics: AnalyticsMetrics,
    warn: Arc<WarnLimiter>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let shared = Arc::new(Shared {
            sink,
            tokenizer,
            cache: TokenCache::new(settings.cache_capacity),
            settings,
            metrics,
            warn,
            epoch: Instant::now(),
        });
        let sem = Arc::new(Semaphore::new(settings.concurrency));
        while let Some(p) = rx.recv().await {
            let Ok(permit) = Arc::clone(&sem).acquire_owned().await else {
                break;
            };
            let sh = Arc::clone(&shared);
            tokio::spawn(async move {
                process(p, &sh).await;
                drop(permit);
            });
        }
        let all = u32::try_from(settings.concurrency).unwrap_or(u32::MAX);
        let _ = sem.acquire_many(all).await;
    })
}

static GLOBAL: OnceLock<AnalyticsPublisher> = OnceLock::new();

/// The process-wide publisher, if [`boot`] installed one.
#[must_use]
pub fn get() -> Option<&'static AnalyticsPublisher> {
    GLOBAL.get()
}

/// Whether the env baseline turns the producer ON.
fn env_baseline_on() -> bool {
    matches!(
        std::env::var(ENV_BASELINE).ok().as_deref().map(str::trim),
        Some("1" | "true" | "TRUE" | "True" | "yes" | "on")
    )
}

/// PostHog value if seen, else the ENV baseline -- never the license bypass domain (a data-collecting
/// producer must be switched on deliberately, default OFF), same rule as the canary.
pub async fn flag_wanted(
    client: Option<&Arc<penguin_licensing::LicenseClient>>,
    env_on: bool,
) -> bool {
    match client {
        Some(c) => crate::license::posthog_then_env(
            crate::license::posthog_flag_value(c, FLAG_KEY).await,
            env_on,
        ),
        None => env_on,
    }
}

/// Installs the process-wide publisher: spawns the flag refresher and the worker. Returns
/// `false` (loudly) when Valkey is not configured, in which case the producer stays uninstalled
/// and inert.
pub fn boot(
    cli: &crate::config::CliConfig,
    metrics: AnalyticsMetrics,
    license: Option<Arc<penguin_licensing::LicenseClient>>,
) -> bool {
    let cfg = match penguin_spine::SpineConfig::from_env() {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "analytics producer not installed: spine/valkey config missing");
            return false;
        }
    };
    let client = match crate::outbound::build_redis_client(&cfg) {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "analytics producer not installed: valkey client");
            return false;
        }
    };
    let settings = Settings::from_lookup(&|k| std::env::var(k).ok());
    let gate = Arc::new(AtomicBool::new(false));
    let (publisher, rx, warn) =
        AnalyticsPublisher::new(Arc::clone(&gate), settings.queue, metrics.clone());
    if GLOBAL.set(publisher).is_err() {
        return false;
    }
    let sink = ValkeySink::new(client, settings.timeout.max(Duration::from_millis(50)));
    spawn_worker(
        sink,
        HubUserTokenizer::new(cli.clone()),
        rx,
        settings,
        metrics,
        warn,
    );
    tokio::spawn(async move {
        loop {
            let on = flag_wanted(license.as_ref(), env_baseline_on()).await;
            if gate.swap(on, Ordering::Relaxed) != on {
                tracing::info!(
                    enabled = on,
                    flag = FLAG_KEY,
                    "analytics stream flag changed"
                );
            }
            tokio::time::sleep(FLAG_REFRESH).await;
        }
    });
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    const U: &str = "0b6f3f9e-8f7e-4c75-9b1f-0d6a4a2f1c11";
    const U2: &str = "5d1c2a44-3b0e-4f8e-8a5a-2c9d7e6b1a22";

    fn event(event_type: &str, platform: &str, user: Option<&str>) -> PlatformEvent {
        let mut payload = serde_json::Map::new();
        payload.insert("text".into(), "hello secret".into());
        if let Some(u) = user {
            payload.insert("user_uuid".into(), u.into());
        }
        PlatformEvent {
            platform: platform.into(),
            event_type: event_type.into(),
            actor: Some("some_handle".into()),
            payload,
            occurred_at: "2026-11-18T12:34:56.789Z".into(),
            source: None,
        }
    }

    fn with_raw(mut e: PlatformEvent, id: &str) -> PlatformEvent {
        e.payload.insert("user_id".into(), id.into());
        e
    }

    fn metrics() -> AnalyticsMetrics {
        register_metrics(&Registry::new())
    }

    fn settings() -> Settings {
        Settings {
            maxlen: 1234,
            timeout: Duration::from_millis(30),
            queue: 8,
            tokenize_timeout: Duration::from_millis(30),
            concurrency: 2,
            cache_capacity: 4,
        }
    }

    #[test]
    fn wire_has_exact_field_set_and_no_pii() {
        let p = build_event(&event("chat.message", "discord", Some(U)), Some((7, 42))).unwrap();
        let fields = p.finish(U.into()).fields();
        let keys: Vec<_> = fields.iter().map(|(k, _)| *k).collect();
        assert_eq!(
            keys,
            [
                "v",
                "tenant_id",
                "community_id",
                "user_uuid",
                "platform_code",
                "kind_code",
                "ts_ms",
                "provenance_code"
            ]
        );
        let get = |k: &str| fields.iter().find(|(n, _)| *n == k).unwrap().1.clone();
        assert_eq!(get("v"), "1");
        assert_eq!(get("tenant_id"), "7");
        assert_eq!(get("community_id"), "42");
        assert_eq!(get("user_uuid"), U);
        assert_eq!(get("platform_code"), "2");
        assert_eq!(get("provenance_code"), "2");
        assert_eq!(get("kind_code"), "1");
        assert_eq!(get("ts_ms"), "1795005296789");
        let all: String = fields.iter().map(|(k, v)| format!("{k}={v};")).collect();
        for banned in [
            "some_handle",
            "hello secret",
            "actor",
            "text",
            "username",
            "handle",
        ] {
            assert!(!all.contains(banned), "{banned} leaked into the wire shape");
        }
    }

    #[test]
    fn kinds_and_platforms_map_to_codes() {
        for (t, k) in [
            ("chat.message", 1),
            ("message", 1),
            ("interaction.command", 2),
            ("member.join", 3),
            ("member.leave", 4),
            ("presence.start", 5),
            ("presence.end", 6),
        ] {
            assert_eq!(kind_for_event_type(t).unwrap() as u8, k, "{t}");
        }
        assert!(kind_for_event_type("stream.online").is_none());
        for (p, c) in [
            ("waddles", 1),
            ("discord", 2),
            ("twitch", 3),
            ("youtube", 4),
            ("kick", 5),
        ] {
            assert_eq!(platform_code(p), c, "{p}");
        }
    }

    #[test]
    fn skips_never_fabricate() {
        let e = event("chat.message", "twitch", Some(U));
        assert_eq!(
            build_event(&event("stream.online", "twitch", Some(U)), Some((1, 1))),
            Err(SkipReason::UnmappedKind)
        );
        assert_eq!(build_event(&e, None), Err(SkipReason::NoScope));
        assert_eq!(build_event(&e, Some((0, 1))), Err(SkipReason::NoScope));
        assert_eq!(build_event(&e, Some((1, 0))), Err(SkipReason::TenantWide));
        assert_eq!(
            build_event(&event("chat.message", "twitch", None), Some((1, 1))),
            Err(SkipReason::NoUserToken)
        );
        assert_eq!(
            build_event(
                &with_raw(event("chat.message", "kick", None), "42"),
                Some((1, 1))
            ),
            Err(SkipReason::NoUserToken)
        );
        assert_eq!(
            build_event(
                &with_raw(event("chat.message", "twitch", None), "a b"),
                Some((1, 1))
            ),
            Err(SkipReason::NoUserToken)
        );
        let mut nil = event(
            "chat.message",
            "twitch",
            Some("00000000-0000-0000-0000-000000000000"),
        );
        assert_eq!(
            build_event(&nil, Some((1, 1))),
            Err(SkipReason::NoUserToken)
        );
        nil.payload.insert("user_uuid".into(), "not-a-uuid".into());
        assert_eq!(
            build_event(&nil, Some((1, 1))),
            Err(SkipReason::NoUserToken)
        );
        let mut ts = event("chat.message", "twitch", Some(U));
        ts.occurred_at = "yesterday".into();
        assert_eq!(
            build_event(&ts, Some((1, 1))),
            Err(SkipReason::BadTimestamp)
        );
        for r in [
            SkipReason::UnmappedKind,
            SkipReason::NoScope,
            SkipReason::TenantWide,
            SkipReason::NoUserToken,
            SkipReason::BadTimestamp,
            SkipReason::TokenizerUnavailable,
        ] {
            assert!(!r.label().is_empty());
        }
    }

    #[test]
    fn stamped_token_wins_over_raw_id() {
        let e = with_raw(event("chat.message", "twitch", Some(U)), "999");
        let p = build_event(&e, Some((1, 1))).unwrap();
        assert_eq!(p.user, UserRef::Token(U.into()));
        let r = with_raw(event("chat.message", "twitch", None), "999");
        assert_eq!(
            build_event(&r, Some((1, 1))).unwrap().user,
            UserRef::Raw {
                platform: "twitch",
                id: "999".into()
            }
        );
        let mut a = event("chat.message", "discord", None);
        a.payload.insert("author_id".into(), "123456789".into());
        assert!(matches!(
            build_event(&a, Some((1, 1))).unwrap().user,
            UserRef::Raw {
                platform: "discord",
                ..
            }
        ));
    }

    #[test]
    fn timestamp_parser_edges() {
        assert_eq!(parse_rfc3339_millis_z("1970-01-01T00:00:00.000Z"), Some(0));
        assert_eq!(parse_rfc3339_millis_z("1970-01-01T00:00:01Z"), Some(1000));
        assert_eq!(
            parse_rfc3339_millis_z("2000-03-01T00:00:00.5Z"),
            Some(951_868_800_500)
        );
        assert_eq!(
            parse_rfc3339_millis_z("2024-02-29T23:59:59.999Z"),
            Some(1_709_251_199_999)
        );
        assert_eq!(parse_rfc3339_millis_z("1969-12-31T23:59:59.000Z"), None);
        assert_eq!(parse_rfc3339_millis_z("2026-13-01T00:00:00.000Z"), None);
        assert_eq!(parse_rfc3339_millis_z("2026-11-18T12:34:56.xyzZ"), None);
        assert_eq!(parse_rfc3339_millis_z("2026-11-18T12:34:56+00:00"), None);
        assert_eq!(parse_rfc3339_millis_z("2026-11-18T12:34:56.Z"), None);
    }

    #[test]
    fn settings_defaults_overrides_and_invalid() {
        let none = Settings::from_lookup(&|_| None);
        assert_eq!(none.maxlen, 500_000);
        assert_eq!(none.timeout, Duration::from_millis(250));
        assert_eq!(none.queue, 4096);
        assert_eq!(none.tokenize_timeout, Duration::from_millis(500));
        assert_eq!(none.concurrency, 8);
        assert_eq!(none.cache_capacity, 10_000);
        let set = Settings::from_lookup(&|k| match k {
            "ANALYTICS_STREAM_MAXLEN" => Some("2000".into()),
            "ANALYTICS_PUBLISH_TIMEOUT_MS" => Some("40".into()),
            "ANALYTICS_QUEUE_CAPACITY" => Some("8".into()),
            "ANALYTICS_TOKENIZE_TIMEOUT_MS" => Some("90".into()),
            "ANALYTICS_TOKENIZE_CONCURRENCY" => Some("3".into()),
            "ANALYTICS_TOKEN_CACHE_CAPACITY" => Some("77".into()),
            _ => None,
        });
        assert_eq!(set.maxlen, 2000);
        assert_eq!(set.timeout, Duration::from_millis(40));
        assert_eq!(set.queue, 8);
        assert_eq!(set.tokenize_timeout, Duration::from_millis(90));
        assert_eq!(set.concurrency, 3);
        assert_eq!(set.cache_capacity, 77);
        let bad = Settings::from_lookup(&|k| (k == "ANALYTICS_STREAM_MAXLEN").then(|| "5".into()));
        assert_eq!(bad.maxlen, DEFAULT_MAXLEN);
        let junk = Settings::from_lookup(&|_| Some("abc".into()));
        assert_eq!(junk.queue, DEFAULT_QUEUE);
    }

    #[test]
    fn warn_limiter_rate_limits() {
        let l = WarnLimiter::new(100);
        assert!(l.allow(1));
        assert!(!l.allow(50));
        assert!(l.allow(150));
        assert!(!l.allow(200));
    }

    #[test]
    fn cache_key_is_tenant_and_platform_scoped_and_hides_the_id() {
        let a = cache_key(1, "twitch", "999");
        assert_ne!(a, cache_key(2, "twitch", "999"), "tenant must scope");
        assert_ne!(a, cache_key(1, "discord", "999"), "platform must scope");
        assert_ne!(a, cache_key(1, "twitch", "9999"));
        assert_eq!(a, cache_key(1, "twitch", "999"));
        assert!(!a.windows(3).any(|w| w == b"999"));
    }

    #[test]
    fn cache_is_bounded_and_expires() {
        let c = TokenCache::new(2);
        assert!(c.is_empty());
        let now = Instant::now();
        let k = |i: i64| cache_key(i, "twitch", "1");
        let ttl = Duration::from_secs(10);
        c.put(k(1), CacheVal::Missing, ttl, now);
        c.put(k(2), CacheVal::Down, ttl, now);
        c.put(k(2), CacheVal::Missing, ttl, now);
        c.put(k(3), CacheVal::Token(U.into()), ttl, now);
        assert_eq!(c.len(), 2);
        assert_eq!(c.get(&k(1), now), None, "oldest evicted");
        assert_eq!(c.get(&k(3), now), Some(CacheVal::Token(U.into())));
        assert_eq!(c.get(&k(3), now + Duration::from_secs(11)), None, "expired");
        assert_eq!(c.len(), 1);
    }

    #[test]
    fn flag_off_is_a_total_noop() {
        let m = metrics();
        let (p, mut rx, _w) =
            AnalyticsPublisher::new(Arc::new(AtomicBool::new(false)), 4, m.clone());
        p.observe(&event("chat.message", "discord", Some(U)), Some((1, 1)));
        p.observe(&event("chat.message", "discord", None), Some((1, 1)));
        assert!(rx.try_recv().is_err());
        assert_eq!(m.skipped.with_label_values(&["no_user_token"]).get(), 0);
        assert_eq!(m.published.get(), 0);
    }

    #[test]
    fn flag_on_queues_and_counts_skips() {
        let m = metrics();
        let (p, mut rx, _w) =
            AnalyticsPublisher::new(Arc::new(AtomicBool::new(true)), 4, m.clone());
        p.observe(&event("chat.message", "discord", Some(U)), Some((1, 2)));
        p.observe(&event("chat.message", "discord", None), Some((1, 2)));
        assert_eq!(rx.try_recv().unwrap().community_id, 2);
        assert!(rx.try_recv().is_err());
        assert_eq!(m.skipped.with_label_values(&["no_user_token"]).get(), 1);
    }

    #[test]
    fn full_queue_drops_without_blocking() {
        let m = metrics();
        let (p, _rx, _w) = AnalyticsPublisher::new(Arc::new(AtomicBool::new(true)), 1, m.clone());
        for _ in 0..3 {
            p.observe(&event("chat.message", "discord", Some(U)), Some((1, 2)));
        }
        assert_eq!(m.failed.with_label_values(&["queue_full"]).get(), 2);
    }

    type Call = (String, usize, Vec<(&'static str, String)>);

    #[derive(Default)]
    struct FakeSink {
        calls: Mutex<Vec<Call>>,
        mode: u8, // 0 ok, 1 error, 2 hang, 3 connect error
    }
    impl AnalyticsSink for Arc<FakeSink> {
        async fn xadd(
            &self,
            stream: &str,
            maxlen: usize,
            fields: &[(&'static str, String)],
        ) -> Result<(), String> {
            match self.mode {
                1 => return Err("boom".into()),
                2 => tokio::time::sleep(Duration::from_secs(60)).await,
                3 => return Err("connect: refused".into()),
                _ => {}
            }
            self.calls
                .lock()
                .unwrap()
                .push((stream.into(), maxlen, fields.to_vec()));
            Ok(())
        }
    }

    struct FakeTok {
        calls: Arc<AtomicUsize>,
        seen: Arc<Mutex<Vec<(i64, &'static str)>>>,
        mode: u8, // 0 ok, 1 not found, 2 unavailable, 3 hang, 4 bad token
    }
    impl UserTokenizer for FakeTok {
        async fn tokenize(
            &self,
            tenant_id: i64,
            _community_id: i64,
            platform: &'static str,
            _raw_id: &str,
        ) -> Result<String, TokenizeFail> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.seen.lock().unwrap().push((tenant_id, platform));
            match self.mode {
                1 => Err(TokenizeFail::NotFound),
                2 => Err(TokenizeFail::Unavailable),
                3 => {
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    Err(TokenizeFail::Unavailable)
                }
                4 => Ok("not-a-uuid".into()),
                _ => Ok(U2.into()),
            }
        }
    }

    struct Run {
        sink: Arc<FakeSink>,
        m: AnalyticsMetrics,
        tok_calls: usize,
        seen: Vec<(i64, &'static str)>,
    }

    async fn run(sink_mode: u8, tok_mode: u8, events: Vec<PlatformEvent>) -> Run {
        let m = metrics();
        let sink = Arc::new(FakeSink {
            mode: sink_mode,
            ..FakeSink::default()
        });
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let tok = FakeTok {
            calls: Arc::clone(&calls),
            seen: Arc::clone(&seen),
            mode: tok_mode,
        };
        let (p, rx, w) = AnalyticsPublisher::new(Arc::new(AtomicBool::new(true)), 8, m.clone());
        let h = spawn_worker(Arc::clone(&sink), tok, rx, settings(), m.clone(), w);
        for e in &events {
            p.observe(e, Some((3, 9)));
        }
        drop(p);
        h.await.unwrap();
        let seen = seen.lock().unwrap().clone();
        Run {
            sink,
            m,
            tok_calls: calls.load(Ordering::SeqCst),
            seen,
        }
    }

    #[tokio::test]
    async fn worker_xadds_to_stream_with_maxlen() {
        let r = run(0, 0, vec![event("chat.message", "twitch", Some(U))]).await;
        let calls = r.sink.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "waddles:analytics");
        assert_eq!(calls[0].1, 1234);
        assert_eq!(calls[0].2.len(), 8);
        assert_eq!(r.m.published.get(), 1);
        assert_eq!(r.m.latency.get_sample_count(), 1);
        assert_eq!(r.tok_calls, 0, "a stamped token is never re-resolved");
    }

    #[tokio::test]
    async fn raw_id_is_tokenized_and_never_reaches_the_stream() {
        let ev = with_raw(event("chat.message", "twitch", None), "999");
        let r = run(0, 0, vec![ev.clone(), ev]).await;
        let calls = r.sink.calls.lock().unwrap();
        assert_eq!(calls.len(), 2);
        for c in calls.iter() {
            let all: String = c.2.iter().map(|(k, v)| format!("{k}={v};")).collect();
            assert!(all.contains(U2) && !all.contains("=999"));
        }
        assert!(r.tok_calls >= 1 && r.tok_calls <= 2);
        assert_eq!(r.seen[0], (3, "twitch"), "minted under the event's tenant");
    }

    #[tokio::test]
    async fn tokenizer_failures_skip_with_reason_and_never_publish() {
        for (mode, reason) in [
            (1, "no_user_token"),
            (2, "tokenizer_unavailable"),
            (3, "tokenizer_unavailable"),
            (4, "tokenizer_unavailable"),
        ] {
            let ev = with_raw(event("chat.message", "discord", None), "55");
            let r = run(0, mode, vec![ev]).await;
            assert!(r.sink.calls.lock().unwrap().is_empty());
            assert_eq!(
                r.m.skipped.with_label_values(&[reason]).get(),
                1,
                "{reason}"
            );
            assert_eq!(r.m.published.get(), 0);
        }
    }

    #[tokio::test]
    async fn cached_outcomes_absorb_repeats() {
        // Sequential (concurrency 1) so the second lookup sees the first's cache entry.
        for (mode, reason) in [(1, "no_user_token"), (2, "tokenizer_unavailable")] {
            let m = metrics();
            let calls = Arc::new(AtomicUsize::new(0));
            let tok = FakeTok {
                calls: Arc::clone(&calls),
                seen: Arc::new(Mutex::new(Vec::new())),
                mode,
            };
            let cache = TokenCache::new(4);
            let p = build_event(
                &with_raw(event("chat.message", "twitch", None), "7"),
                Some((1, 1)),
            )
            .unwrap();
            for _ in 0..3 {
                let r = resolve_user(&p, &tok, &cache, Duration::from_millis(30)).await;
                assert_eq!(r.unwrap_err().label(), reason);
            }
            assert_eq!(calls.load(Ordering::SeqCst), 1, "{reason}: cached");
            drop(m);
        }
        let tok = FakeTok {
            calls: Arc::new(AtomicUsize::new(0)),
            seen: Arc::new(Mutex::new(Vec::new())),
            mode: 0,
        };
        let cache = TokenCache::new(4);
        let p = build_event(
            &with_raw(event("chat.message", "twitch", None), "8"),
            Some((1, 1)),
        )
        .unwrap();
        for _ in 0..3 {
            assert_eq!(
                resolve_user(&p, &tok, &cache, Duration::from_millis(30)).await,
                Ok(U2.to_string())
            );
        }
        assert_eq!(tok.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn sink_failures_are_isolated_and_counted() {
        for (mode, reason) in [(1, "error"), (2, "timeout"), (3, "connect")] {
            let r = run(mode, 0, vec![event("chat.message", "twitch", Some(U))]).await;
            assert!(r.sink.calls.lock().unwrap().is_empty());
            assert_eq!(r.m.failed.with_label_values(&[reason]).get(), 1, "{reason}");
            assert_eq!(r.m.published.get(), 0);
        }
    }

    #[tokio::test]
    async fn flag_wanted_defaults_off_without_client() {
        assert!(!flag_wanted(None, false).await);
        assert!(flag_wanted(None, true).await);
    }
}
